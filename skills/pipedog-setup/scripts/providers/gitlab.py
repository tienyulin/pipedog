"""GitLab CI provider (REST API v4). See docs/DESIGN.md section 5."""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote, urlsplit

import extractor
import grouping
import model
from providers import http

_PER_PAGE = 100
_MAX_PAGES = 5

_STATUS_MAP: dict[str, model.Status] = {
    "failed": "failed",
    "running": "running",
    "created": "pending",
    "pending": "pending",
    "preparing": "pending",
    "waiting_for_resource": "pending",
    "scheduled": "pending",
    "waiting_for_callback": "pending",
    "success": "passed",
    "canceled": "skipped",
    "canceling": "skipped",
    "skipped": "skipped",
    "manual": "skipped",
}
_DONE: tuple[str, ...] = ("passed", "failed", "skipped")
# HTTP statuses that will not change on retry (cache the "log unavailable" result).
_PERMANENT = (403, 404, 410)


def map_status(native: str | None) -> model.Status:
    """Map a GitLab status onto the normalized status (unknown -> pending)."""
    return _STATUS_MAP.get(native or "", "pending")


def _project_path(web_url: str | None) -> str | None:
    """``https://host/group/sub/repo/-/jobs/1`` -> ``group/sub/repo``."""
    if not web_url:
        return None
    path = urlsplit(web_url).path
    if "/-/" not in path:
        return None
    return path.split("/-/", 1)[0].strip("/") or None


def _strip_prefixes(trace: str, web_url: str | None) -> list[str]:
    """Work out the CI build-dir prefixes (``/builds/...``) that appear in a trace.

    Runners check out at ``/builds/<namespace>/<project>/`` or, with newer runners,
    ``/builds/<token>/<n>/<namespace>/<project>/``; the exact prefix is taken from the
    trace itself so both layouts work.
    """
    prefixes: list[str] = []
    path = _project_path(web_url)
    if path:
        for target in (path, path.rsplit("/", 1)[-1]):
            m = re.search(rf"(/builds/(?:[^/\s]+/)*?{re.escape(target)}/)", trace)
            if m:
                prefixes.append(m.group(1))
                break
        prefixes.append(f"/builds/{path}/")
    # Fallback: the checkout dir is announced when the repository is initialised.
    m = re.search(r"in (/builds/\S+?)/\.git/", trace)
    if m:
        prefixes.append(m.group(1) + "/")
    return list(dict.fromkeys(prefixes))


def _min_job_id(job: model.Job) -> tuple[int, Any]:
    """Return sorting key based on minimum job ID among leaves."""
    numeric_ids: list[int] = []
    str_ids: list[str] = []
    for leaf in job.leaves():
        if leaf.job_id is not None:
            try:
                numeric_ids.append(int(leaf.job_id))
            except (ValueError, TypeError):
                str_ids.append(str(leaf.job_id))
    if numeric_ids:
        return (0, min(numeric_ids))
    if str_ids:
        return (1, min(str_ids))
    if job.job_id is not None:
        try:
            return (0, int(job.job_id))
        except (ValueError, TypeError):
            return (1, str(job.job_id))
    return (2, 0)


class Client:
    """GitLab client. Instances are reused across polls and keep caches."""

    def __init__(self, base_url: str, token: str | None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api = f"{self.base_url}/api/v4"
        self._token = token or None
        self._headers: dict[str, str] = {"PRIVATE-TOKEN": self._token} if self._token else {}
        self._traces: dict[Any, model.ErrorInfo] = {}                 # job_id -> ErrorInfo
        self._downstream: dict[tuple[str, Any], list[model.Job]] = {}  # finished downstream jobs

    # ------------------------------------------------------------------ #
    # public
    # ------------------------------------------------------------------ #

    def fetch(self, project: dict, branch: str | None, sha: str | None = None) -> model.Pipeline:
        """Fetch the latest pipeline of ``project``. Never raises."""
        ref = str(project.get("id", ""))
        pipe = model.Pipeline(
            project=project.get("name") or ref,
            short=project.get("short") or (project.get("name") or ref),
            provider="gitlab",
            project_ref=ref,
            local_path=project.get("local_path"),
            branch=branch,
            fetched_at=time.time(),
        )
        try:
            self._fill(pipe, ref, branch, sha)
        except Exception as e:  # noqa: BLE001 - contract: fetch never raises
            msg = e.message if isinstance(e, http.HttpError) else f"{type(e).__name__}: {e}"
            if self._token:
                msg = msg.replace(self._token, "***")
            pipe.fetch_error = msg
            pipe.status = None
            pipe.jobs = []
        return pipe

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _get(self, path: str) -> Any:
        return http.get_json(f"{self.api}{path}", self._headers)

    @staticmethod
    def _pid(project_ref: Any) -> str:
        s = str(project_ref)
        return s if "%" in s else quote(s, safe="")

    def _get_paged(self, path: str) -> list[dict]:
        """GET a list endpoint, following pages while a page comes back full."""
        items: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            url = path if page == 1 else f"{path}&page={page}"
            batch = self._get(url)
            if not isinstance(batch, list):
                raise http.HttpError("unexpected response")
            items.extend(batch)
            if len(batch) < _PER_PAGE:
                break
        return items

    def _fill(self, pipe: model.Pipeline, ref: str, branch: str | None, sha: str | None = None) -> None:
        if not ref:
            raise http.HttpError("project has no id")
        pid = self._pid(ref)

        params = []
        if branch:
            params.append(f"ref={quote(branch, safe='')}")
        if sha:
            params.append(f"sha={quote(sha, safe='')}")
        params.append("per_page=1")
        query = "?" + "&".join(params)
        pipelines = self._get(f"/projects/{pid}/pipelines{query}")
        if not isinstance(pipelines, list):
            raise http.HttpError("unexpected response")
        if not pipelines:
            return  # no pipeline: status None, no jobs, no error

        latest = pipelines[0]
        pipe.pipeline_id = latest.get("id")
        pipe.web_url = latest.get("web_url")
        pipe.head_sha = latest.get("sha")
        pipe.status = map_status(latest.get("status"))

        raw_jobs = self._get_paged(
            f"/projects/{pid}/pipelines/{pipe.pipeline_id}/jobs"
            f"?per_page={_PER_PAGE}&include_retried=false"
        )
        jobs = [self._to_job(j, pid) for j in raw_jobs]
        jobs.extend(self._bridges(pid, pipe.pipeline_id))
        jobs.sort(key=_min_job_id)
        pipe.jobs = grouping.group_jobs(jobs)
        pipe.jobs.sort(key=_min_job_id)

    def _to_job(self, raw: dict, pid: str, *, with_error: bool = True) -> model.Job:
        job = model.Job(
            name=raw.get("name", "?"),
            status=map_status(raw.get("status")),
            stage=raw.get("stage"),
            job_id=raw.get("id"),
            web_url=raw.get("web_url"),
            started_at=http.parse_iso(raw.get("started_at")),
            finished_at=http.parse_iso(raw.get("finished_at")),
            raw_status=raw.get("status"),
            allow_failure=bool(raw.get("allow_failure", False)),
        )
        if with_error and job.status == "failed":
            job.error = self._error_for(raw, pid)
        return job

    def _error_for(self, raw: dict, pid: str) -> model.ErrorInfo:
        """Root cause of a failed job; the trace is fetched once per job id."""
        job_id = raw.get("id")
        cached = self._traces.get(job_id)
        if cached is not None:
            return cached

        prefix = "(allowed) " if raw.get("allow_failure") else ""
        permanent = True
        try:
            trace = http.get_text(f"{self.api}/projects/{pid}/jobs/{job_id}/trace", self._headers)
            info = extractor.extract(trace, _strip_prefixes(trace, raw.get("web_url")))
        except http.HttpError as e:
            permanent = e.status in _PERMANENT
            reason = raw.get("failure_reason")
            detail = f"{reason}, " if reason else ""
            info = model.ErrorInfo(error_summary=f"log unavailable ({detail}{e.message})")
        info.error_summary = (prefix + info.error_summary)[:200]
        if permanent:
            self._traces[job_id] = info
        return info

    def _bridges(self, pid: str, pipeline_id: Any) -> list[model.Job]:
        try:
            raw_bridges = self._get_paged(
                f"/projects/{pid}/pipelines/{pipeline_id}/bridges?per_page={_PER_PAGE}"
            )
        except http.HttpError as e:
            if e.status in (403, 404):  # bridges not available to this token / old GitLab
                return []
            raise

        out: list[model.Job] = []
        for raw in raw_bridges:
            bridge = model.Job(
                name=raw.get("name", "?"),
                status=map_status(raw.get("status")),
                stage=raw.get("stage"),
                job_id=raw.get("id"),
                web_url=raw.get("web_url"),
                started_at=http.parse_iso(raw.get("started_at")),
                finished_at=http.parse_iso(raw.get("finished_at")),
                raw_status=raw.get("status"),
                allow_failure=bool(raw.get("allow_failure", False)),
            )
            subs = self._downstream_jobs(raw.get("downstream_pipeline"))
            if subs:
                group = model.make_group(bridge.name, subs)
                group.stage = bridge.stage
                group.job_id = bridge.job_id
                group.web_url = bridge.web_url
                bridge = group
            elif bridge.status == "failed":
                reason = raw.get("failure_reason")
                bridge.error = model.ErrorInfo(
                    error_summary=f"bridge failed ({reason})" if reason else "bridge failed"
                )
            out.append(bridge)
        return out

    def _downstream_jobs(self, ds: dict | None) -> list[model.Job]:
        """Jobs of a bridge's downstream pipeline (cached once it has finished)."""
        if not ds or not ds.get("id"):
            return []
        ds_project = ds.get("project_id")
        if ds_project is None:
            return []
        dpid = self._pid(ds_project)
        key = (dpid, ds["id"])
        finished = map_status(ds.get("status")) in _DONE
        if finished and key in self._downstream:
            return self._downstream[key]
        try:
            raw_jobs = self._get_paged(
                f"/projects/{dpid}/pipelines/{ds['id']}/jobs"
                f"?per_page={_PER_PAGE}&include_retried=false"
            )
        except http.HttpError as e:
            if e.status in (403, 404):  # no access to the downstream project
                return []
            raise
        jobs = [self._to_job(j, dpid) for j in raw_jobs]
        jobs.sort(key=_min_job_id)
        if finished:
            self._downstream[key] = jobs
        return jobs
