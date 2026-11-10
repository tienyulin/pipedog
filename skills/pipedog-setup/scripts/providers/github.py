"""GitHub Actions provider (REST API). See docs/DESIGN.md section 5."""

from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import quote

import extractor
import grouping
import model
from providers import http

_PER_PAGE = 100
_MAX_PAGES = 5
_PENDING = ("queued", "waiting", "requested", "pending")
_FAILED = ("failure", "timed_out", "startup_failure")
_PERMANENT = (401, 403, 404, 410)  # log download will not succeed on retry


def map_status(status: str | None, conclusion: str | None = None) -> model.Status:
    """Map GitHub ``status`` / ``conclusion`` onto the normalized status."""
    if status == "completed":
        if conclusion == "success":
            return "passed"
        if conclusion in _FAILED:
            return "failed"
        return "skipped"  # cancelled / skipped / neutral / action_required / stale / None
    if status == "in_progress":
        return "running"
    if status in _PENDING:
        return "pending"
    return "pending"


def _deploy_workflows(project: dict) -> set[str]:
    deploy = project.get("deploy")
    if not isinstance(deploy, dict):
        return set()
    envs = deploy.get("environments")
    if not isinstance(envs, list):
        return set()
    result = set()
    for env in envs:
        if not isinstance(env, dict):
            continue
        gh = env.get("github")
        if isinstance(gh, dict):
            wf = gh.get("workflow")
            if wf and isinstance(wf, str):
                result.add(wf)
    return result


def _is_deploy_workflow(run: dict, deploy_wfs: set[str]) -> bool:
    if not deploy_wfs:
        return False
    path = run.get("path")
    if not path or not isinstance(path, str):
        return False
    run_base = os.path.basename(path)
    for dwf in deploy_wfs:
        if path == dwf or (run_base and run_base == os.path.basename(dwf)):
            return True
    return False


def _run_timestamp(r: dict) -> float:
    t = http.parse_iso(r.get("created_at"))
    return t if t is not None else 0.0


def _run_number(r: dict) -> int:
    num = r.get("run_number")
    return int(num) if isinstance(num, (int, str)) and str(num).isdigit() else 0


class Client:
    """GitHub Actions client. Instances are reused across polls and keep caches."""

    def __init__(self, base_url: str, token: str | None) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token or None
        self._headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            self._headers["Authorization"] = f"Bearer {self._token}"
        self._logs: dict[Any, model.ErrorInfo] = {}  # job_id -> ErrorInfo

    # ------------------------------------------------------------------ #
    # public
    # ------------------------------------------------------------------ #

    def fetch(self, project: dict, branch: str | None, sha: str | None = None) -> model.Pipeline:
        """Fetch the latest workflow run of ``project``. Never raises."""
        repo = str(project.get("repo", ""))
        pipe = model.Pipeline(
            project=project.get("name") or repo,
            short=project.get("short") or (project.get("name") or repo),
            provider="github",
            project_ref=repo,
            local_path=project.get("local_path"),
            branch=branch,
            fetched_at=time.time(),
        )
        try:
            self._fill(pipe, repo, branch, sha, project=project)
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
        return http.get_json(f"{self.base_url}{path}", self._headers)

    def _fill(
        self,
        pipe: model.Pipeline,
        repo: str,
        branch: str | None,
        sha: str | None = None,
        project: dict | None = None,
    ) -> None:
        if repo.count("/") != 1 or not all(repo.split("/")):
            raise http.HttpError("project repo must look like 'owner/repo'")

        params = []
        if branch:
            params.append(f"branch={quote(branch, safe='')}")
        if sha:
            params.append(f"head_sha={quote(sha, safe='')}")
        params.append("per_page=30")
        query = "?" + "&".join(params)
        data = self._get(f"/repos/{repo}/actions/runs{query}")
        runs = data.get("workflow_runs") if isinstance(data, dict) else None
        if not isinstance(runs, list):
            raise http.HttpError("unexpected response")
        if not runs:
            return  # no run: status None, no jobs, no error

        # Pick the target commit from CI runs only: a deploy/rollback run (e.g. a
        # rerun of an old deployment) can be newest yet point at an older sha.
        deploy_wfs = _deploy_workflows(project or {})
        ci_runs = [r for r in runs if not _is_deploy_workflow(r, deploy_wfs)]
        if not ci_runs:
            return  # only deploy runs: treat as no CI run
        target_sha = sha or ci_runs[0].get("head_sha")
        if target_sha is not None:
            non_deploy_runs = [r for r in ci_runs if r.get("head_sha") == target_sha]
        else:
            non_deploy_runs = ci_runs

        # Keep newest run per workflow_id (by created_at / run_number)
        newest_per_wf: dict[Any, dict] = {}
        for r in non_deploy_runs:
            wf_key = r.get("workflow_id") or r.get("path") or r.get("name")
            if wf_key not in newest_per_wf:
                newest_per_wf[wf_key] = r
            else:
                existing = newest_per_wf[wf_key]
                r_key = (_run_timestamp(r), _run_number(r))
                ex_key = (_run_timestamp(existing), _run_number(existing))
                if r_key > ex_key:
                    newest_per_wf[wf_key] = r

        kept_runs = list(newest_per_wf.values())
        if not kept_runs:
            return  # no run: status None, no jobs, no error

        # Order kept runs: oldest run first
        run_indices = {id(r): idx for idx, r in enumerate(runs)}
        kept_runs.sort(
            key=lambda r: (_run_timestamp(r), _run_number(r), -run_indices.get(id(r), 0))
        )

        first_run = kept_runs[0]
        pipe.pipeline_id = first_run.get("id")
        pipe.web_url = first_run.get("html_url")
        pipe.head_sha = target_sha
        pipe.status = model.aggregate_status(
            [map_status(r.get("status"), r.get("conclusion")) for r in kept_runs]
        )

        pipe.jobs = []
        for run in kept_runs:
            run_id = run.get("id")
            raw_jobs: list[dict] = []
            for page in range(1, _MAX_PAGES + 1):
                url = f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page={_PER_PAGE}&filter=latest"
                if page > 1:
                    url += f"&page={page}"
                body = self._get(url)
                batch = body.get("jobs") if isinstance(body, dict) else None
                if not isinstance(batch, list):
                    raise http.HttpError("unexpected response")
                raw_jobs.extend(batch)
                if len(batch) < _PER_PAGE:
                    break

            wf_name = run.get("name")
            jobs = [self._to_job(j, repo, workflow=wf_name) for j in raw_jobs]
            pipe.jobs.extend(grouping.group_jobs(jobs))

    def _to_job(self, raw: dict, repo: str, workflow: str | None = None) -> model.Job:
        status = map_status(raw.get("status"), raw.get("conclusion"))
        raw_status = raw.get("conclusion") or raw.get("status")
        job = model.Job(
            name=raw.get("name", "?"),
            status=status,
            job_id=raw.get("id"),
            web_url=raw.get("html_url"),
            # GitHub stamps queued jobs with a start time; only report real starts.
            started_at=None if status == "pending" else http.parse_iso(raw.get("started_at")),
            finished_at=http.parse_iso(raw.get("completed_at")) if raw.get("status") == "completed" else None,
            raw_status=raw_status,
            workflow=workflow,
        )
        if status == "failed":
            job.error = self._error_for(raw, repo)
        return job

    def _error_for(self, raw: dict, repo: str) -> model.ErrorInfo:
        """Root cause of a failed job; the log is fetched once per job id."""
        job_id = raw.get("id")
        cached = self._logs.get(job_id)
        if cached is not None:
            return cached

        name = repo.split("/", 1)[1]
        prefixes = [f"/home/runner/work/{name}/{name}/", f"/__w/{name}/{name}/"]
        permanent = True
        try:
            log = http.get_text(f"{self.base_url}/repos/{repo}/actions/jobs/{job_id}/logs", self._headers)
            info = extractor.extract(log, prefixes)
        except http.HttpError as e:
            permanent = e.status in _PERMANENT
            step = next(
                (s.get("name") for s in raw.get("steps") or [] if s.get("conclusion") == "failure"),
                None,
            )
            what = f"step '{step}' failed" if step else "job failed"
            info = model.ErrorInfo(error_summary=f"{what} (log unavailable: {e.message})")
        if permanent:
            self._logs[job_id] = info
        return info
