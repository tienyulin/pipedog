"""GitHub Deployments & Actions deploy adapter."""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import quote

import deploymodel
from deploy_adapters.base import (
    DeployError,
    after_branch,
    as_env_dict,
    env_name,
    platform_env_name,
    redact_secrets,
    substitute_placeholders,
)
from providers import http

_RUN_ID_RE = re.compile(r"/actions/runs/(\d+)")
_SHA40_RE = re.compile(r"\b[0-9a-f]{40}\b", re.IGNORECASE)
_SHA_SHORT_RE = re.compile(r"\b[0-9a-f]{7,39}\b", re.IGNORECASE)

_STATUS_MAP: dict[str, deploymodel.EnvStatus] = {
    "success": "success",
    "inactive": "success",
    "failure": "failed",
    "error": "failed",
    "in_progress": "running",
    "queued": "running",
    "pending": "running",
    "waiting": "waiting_approval",
}


class GitHubDeployAdapter:
    def __init__(self, base_url: str, token: str | None = None, **_: Any) -> None:
        self.base_url = (base_url or "https://api.github.com").rstrip("/")
        self._token = token or None
        self._headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            self._headers["Authorization"] = f"Bearer {self._token}"
        self._run_cache: dict[str, str | None] = {}
        self._run_lookups_remaining = 10

    @contextmanager
    def _wrap_errors(self, *, is_approve: bool = False) -> Iterator[None]:
        self._run_lookups_remaining = 10
        try:
            yield
        except DeployError:
            raise
        except http.HttpError as e:
            if is_approve and e.status in (403, 422):
                raise DeployError("not an allowed approver") from None
            msg = redact_secrets(e.message, [self._token])
            raise DeployError(msg) from None
        except Exception as e:
            msg = redact_secrets(str(e), [self._token])
            raise DeployError(msg) from None

    @staticmethod
    def _repo(project: dict[str, Any]) -> str:
        repo = str(project.get("repo") or project.get("project_ref") or "")
        if repo.count("/") != 1:
            raise DeployError(f"invalid repo: {repo}")
        return repo

    def _post_allow_empty(self, url: str, body: Any) -> Any:
        try:
            return http.post_json(url, self._headers, body)
        except http.HttpError as e:
            if e.status is None and e.message == "invalid JSON response":
                return {}
            raise

    @staticmethod
    def _extract_run_id(
        dep: dict[str, Any], latest_status: dict[str, Any] | None
    ) -> str | None:
        for obj in (latest_status or {}, dep):
            if not isinstance(obj, dict):
                continue
            if obj.get("run_id") is not None:
                return str(obj["run_id"])
            payload = obj.get("payload")
            if isinstance(payload, dict) and payload.get("run_id") is not None:
                return str(payload["run_id"])
            for url_key in ("target_url", "log_url", "environment_url", "url"):
                val = obj.get(url_key)
                if isinstance(val, str):
                    m = _RUN_ID_RE.search(val)
                    if m:
                        return m.group(1)
        return None

    def _real_sha(
        self,
        repo: str,
        dep: dict[str, Any],
        latest_status: dict[str, Any] | None = None,
    ) -> str | None:
        run_id = self._extract_run_id(dep, latest_status)
        if not run_id:
            return None
        if run_id in self._run_cache:
            return self._run_cache[run_id]
        if getattr(self, "_run_lookups_remaining", 0) <= 0:
            return None
        self._run_lookups_remaining -= 1
        try:
            run_data = http.get_json(
                f"{self.base_url}/repos/{repo}/actions/runs/{run_id}",
                self._headers,
            )
        except Exception as exc:
            # Cache only permanent misses; a transient error must not hide the run forever.
            if isinstance(exc, http.HttpError) and exc.status in (404, 410):
                self._run_cache[run_id] = None
            return None

        if isinstance(run_data, dict):
            title = run_data.get("display_title")
            if isinstance(title, str):
                m40 = _SHA40_RE.search(title)
                if m40:
                    sha = m40.group(0)
                    self._run_cache[run_id] = sha
                    return sha
                m_short = _SHA_SHORT_RE.search(title)
                if m_short:
                    sha = m_short.group(0)
                    self._run_cache[run_id] = sha
                    return sha
        self._run_cache[run_id] = None
        return None

    def _inspect_deployment(
        self, repo: str, dep: dict[str, Any]
    ) -> tuple[deploymodel.EnvStatus, float | None, str | None, str | None, str | None]:
        dep_id = dep.get("id")
        statuses = http.get_json(
            f"{self.base_url}/repos/{repo}/deployments/{dep_id}/statuses",
            self._headers,
        )
        latest: dict[str, Any] = {}
        if isinstance(statuses, list) and statuses and isinstance(statuses[0], dict):
            latest = statuses[0]

        raw_state = str(latest.get("state") or "").lower()
        status: deploymodel.EnvStatus = _STATUS_MAP.get(raw_state, "running" if not raw_state else "unknown")
        deployed_at = http.parse_iso(
            latest.get("updated_at")
            or latest.get("created_at")
            or dep.get("updated_at")
            or dep.get("created_at")
        )
        web_url = (
            latest.get("environment_url")
            or latest.get("target_url")
            or latest.get("log_url")
            or dep.get("url")
        )
        run_id = self._extract_run_id(dep, latest)
        real_sha = self._real_sha(repo, dep, latest) if run_id else None
        return status, deployed_at, web_url, run_id, real_sha

    def status(
        self, project: dict[str, Any], env: dict[str, Any] | deploymodel.EnvConfig
    ) -> deploymodel.EnvState:
        repo = self._repo(project)
        name = env_name(env)
        plat = platform_env_name(env)
        with self._wrap_errors():
            deps = http.get_json(
                f"{self.base_url}/repos/{repo}/deployments?environment={quote(plat, safe='')}",
                self._headers,
            )
            if not isinstance(deps, list) or not deps or not isinstance(deps[0], dict):
                return deploymodel.EnvState(
                    env=name,
                    sha=None,
                    status="none",
                    deployed_at=None,
                    web_url=None,
                    deployment_id=None,
                )
            dep = deps[0]
            st, deployed_at, web_url, _, real = self._inspect_deployment(repo, dep)
            return deploymodel.EnvState(
                env=name,
                sha=real or dep.get("sha"),
                status=st,
                deployed_at=deployed_at,
                web_url=web_url,
                deployment_id=str(dep["id"]) if dep.get("id") is not None else None,
            )

    @staticmethod
    def _sha_matches(dep_sha: str | None, target_sha: str) -> bool:
        if not dep_sha:
            return True
        if not target_sha:
            return False
        return (
            dep_sha == target_sha
            or dep_sha.startswith(target_sha)
            or target_sha.startswith(dep_sha)
        )

    def find_deployment(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        sha: str,
    ) -> deploymodel.DeploymentRecord | None:
        repo = self._repo(project)
        plat = platform_env_name(env)
        with self._wrap_errors():
            checked_ids: set[Any] = set()
            deps = http.get_json(
                f"{self.base_url}/repos/{repo}/deployments?environment={quote(plat, safe='')}&sha={quote(sha, safe='')}",
                self._headers,
            )
            if isinstance(deps, list):
                for dep in deps:
                    if not isinstance(dep, dict):
                        continue
                    if dep.get("id") is not None:
                        checked_ids.add(dep["id"])
                    st, deployed_at, web_url, run_id, real = self._inspect_deployment(repo, dep)
                    cand_sha = real or dep.get("sha")
                    if cand_sha and self._sha_matches(cand_sha, sha):
                        return deploymodel.DeploymentRecord(
                            id=str(dep.get("id") or ""),
                            sha=str(cand_sha),
                            status=st,
                            created_at=deployed_at,
                            web_url=web_url,
                            run_id=run_id,
                        )

            try:
                all_deps = http.get_json(
                    f"{self.base_url}/repos/{repo}/deployments?environment={quote(plat, safe='')}",
                    self._headers,
                )
                if isinstance(all_deps, list):
                    for dep in all_deps[:10]:
                        if not isinstance(dep, dict):
                            continue
                        if dep.get("id") is not None and dep["id"] in checked_ids:
                            continue
                        st, deployed_at, web_url, run_id, real = self._inspect_deployment(repo, dep)
                        cand_sha = real or dep.get("sha")
                        if cand_sha and self._sha_matches(cand_sha, sha):
                            return deploymodel.DeploymentRecord(
                                id=str(dep.get("id") or ""),
                                sha=str(cand_sha),
                                status=st,
                                created_at=deployed_at,
                                web_url=web_url,
                                run_id=run_id,
                            )
            except Exception:
                pass
            return None

    def history(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        limit: int = 10,
    ) -> list[deploymodel.DeploymentRecord]:
        repo = self._repo(project)
        plat = platform_env_name(env)
        out: list[deploymodel.DeploymentRecord] = []
        with self._wrap_errors():
            deps = http.get_json(
                f"{self.base_url}/repos/{repo}/deployments?environment={quote(plat, safe='')}",
                self._headers,
            )
            if not isinstance(deps, list):
                return []
            for dep in deps[: max(0, int(limit))]:
                if not isinstance(dep, dict):
                    continue
                st, deployed_at, web_url, run_id, real = self._inspect_deployment(repo, dep)
                out.append(
                    deploymodel.DeploymentRecord(
                        id=str(dep.get("id") or ""),
                        sha=str(real or dep.get("sha") or ""),
                        status=st,
                        created_at=deployed_at,
                        web_url=web_url,
                        run_id=run_id,
                    )
                )
        return out

    def pending_approval(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        sha: str | None = None,
    ) -> deploymodel.PendingGate | None:
        repo = self._repo(project)
        name = env_name(env)
        plat = platform_env_name(env)
        query = f"?head_sha={quote(sha, safe='')}" if sha else "?status=waiting"
        with self._wrap_errors():
            raw_runs = http.get_json(
                f"{self.base_url}/repos/{repo}/actions/runs{query}",
                self._headers,
            )
            runs = (
                raw_runs.get("workflow_runs")
                if isinstance(raw_runs, dict)
                else raw_runs
            )
            if not isinstance(runs, list):
                return None

            for run in runs:
                if not isinstance(run, dict):
                    continue
                if run.get("status") != "waiting":
                    continue
                if sha and run.get("head_sha") and run.get("head_sha") != sha:
                    continue
                run_id = run.get("id")
                if run_id is None:
                    continue
                pending = http.get_json(
                    f"{self.base_url}/repos/{repo}/actions/runs/{run_id}/pending_deployments",
                    self._headers,
                )
                if not isinstance(pending, list):
                    continue
                env_ids: list[int] = []
                for item in pending:
                    if not isinstance(item, dict):
                        continue
                    env_obj = item.get("environment")
                    if isinstance(env_obj, dict):
                        item_name = env_obj.get("name")
                        item_id = env_obj.get("id")
                    else:
                        item_name = env_obj or item.get("name")
                        item_id = item.get("environment_id") or item.get("id")
                    if item_name in (plat, name) and item_id is not None:
                        env_ids.append(int(item_id))
                if env_ids:
                    return deploymodel.PendingGate(
                        id=str(run_id),
                        env=name,
                        sha=str(run.get("head_sha") or sha or ""),
                        environment_ids=env_ids,
                        web_url=run.get("html_url") or run.get("url"),
                    )
        return None

    def _review_pending_deployment(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        gate: deploymodel.PendingGate | str,
        state: str,
        comment: str,
    ) -> None:
        repo = self._repo(project)
        name = env_name(env)
        plat = platform_env_name(env)
        if isinstance(gate, deploymodel.PendingGate):
            run_id = gate.id
            env_ids = list(gate.environment_ids)
        else:
            run_id = str(gate)
            env_ids = []

        with self._wrap_errors(is_approve=True):
            if not env_ids:
                pending = http.get_json(
                    f"{self.base_url}/repos/{repo}/actions/runs/{run_id}/pending_deployments",
                    self._headers,
                )
                if isinstance(pending, list):
                    for item in pending:
                        if not isinstance(item, dict):
                            continue
                        env_obj = item.get("environment")
                        if isinstance(env_obj, dict):
                            item_name = env_obj.get("name")
                            item_id = env_obj.get("id")
                        else:
                            item_name = env_obj or item.get("name")
                            item_id = item.get("environment_id") or item.get("id")
                        if item_name in (plat, name) and item_id is not None:
                            env_ids.append(int(item_id))

            payload = {
                "environment_ids": env_ids,
                "state": state,
                "comment": comment,
            }
            self._post_allow_empty(
                f"{self.base_url}/repos/{repo}/actions/runs/{run_id}/pending_deployments",
                payload,
            )

    def approve(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        gate: deploymodel.PendingGate | str,
        comment: str = "",
    ) -> None:
        self._review_pending_deployment(project, env, gate, "approved", comment)

    def reject(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        gate: deploymodel.PendingGate | str,
        comment: str = "",
    ) -> None:
        self._review_pending_deployment(project, env, gate, "rejected", comment)

    def deploy(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        sha: str,
    ) -> deploymodel.DeploymentRecord:
        repo = self._repo(project)
        env_d = as_env_dict(env)
        gh_cfg = env_d.get("github") or {}
        workflow = gh_cfg.get("workflow")
        if not workflow:
            raise DeployError("github.workflow is not configured")

        plat = platform_env_name(env_d)
        branch = after_branch(project, env_d)
        mapping = {
            "sha": sha,
            "env": plat,
            "branch": branch,
            "previous_sha": "",
            "previous_id": "",
        }
        ref_tpl = str(gh_cfg.get("ref") or "{branch}")
        ref_val = substitute_placeholders(ref_tpl, mapping, shell_quote=False)
        raw_inputs = gh_cfg.get("inputs") or {}
        inputs_val = {
            str(k): substitute_placeholders(str(v), mapping, shell_quote=False)
            for k, v in raw_inputs.items()
        }
        payload = {"ref": ref_val, "inputs": inputs_val}

        dispatched_at = time.time()
        with self._wrap_errors():
            res = self._post_allow_empty(
                f"{self.base_url}/repos/{repo}/actions/workflows/{quote(str(workflow), safe='')}/dispatches",
                payload,
            )
            if isinstance(res, dict) and res.get("id") is not None:
                return deploymodel.DeploymentRecord(
                    id=str(res["id"]),
                    sha=sha,
                    status="running",
                    created_at=dispatched_at,
                    web_url=res.get("html_url") or res.get("url"),
                )

        return deploymodel.DeploymentRecord(
            id=f"dispatch-{sha[:7]}",
            sha=sha,
            status="running",
            created_at=dispatched_at,
        )

    def resolve_dispatch(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        dispatched_at: float | str,
        sha: str | None = None,
    ) -> deploymodel.DeploymentRecord | None:
        if isinstance(dispatched_at, str) and isinstance(sha, (int, float)):
            dispatched_at, sha = sha, dispatched_at

        def _to_epoch(val: Any) -> float | None:
            if val is None:
                return None
            if isinstance(val, (int, float)):
                return float(val)
            if isinstance(val, str):
                return http.parse_iso(val)
            return None

        ts_val = _to_epoch(dispatched_at)
        target_ts = (ts_val if ts_val is not None else 0.0) - 5.0
        repo = self._repo(project)
        plat = platform_env_name(env)

        with self._wrap_errors():
            deps = http.get_json(
                f"{self.base_url}/repos/{repo}/deployments?environment={quote(plat, safe='')}",
                self._headers,
            )
            if not isinstance(deps, list):
                return None

            candidates: list[tuple[float, dict[str, Any]]] = []
            for dep in deps:
                if not isinstance(dep, dict):
                    continue
                dep_ts = _to_epoch(dep.get("created_at"))
                if dep_ts is not None and dep_ts >= target_ts:
                    candidates.append((dep_ts, dep))

            if not candidates:
                return None

            candidates.sort(key=lambda item: item[0])
            dep = candidates[0][1]
            st, deployed_at, web_url, run_id, real = self._inspect_deployment(repo, dep)
            return deploymodel.DeploymentRecord(
                id=str(dep.get("id") or ""),
                sha=str(real or sha or dep.get("sha") or ""),
                status=st,
                created_at=deployed_at or _to_epoch(dep.get("created_at")),
                web_url=web_url,
                run_id=run_id,
            )

    def rollback(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        to: deploymodel.DeploymentRecord,
    ) -> deploymodel.DeploymentRecord:
        repo = self._repo(project)
        if not to.run_id:
            raise DeployError(f"deployment {to.id} has no run_id to rerun")
        with self._wrap_errors():
            self._post_allow_empty(
                f"{self.base_url}/repos/{repo}/actions/runs/{quote(str(to.run_id), safe='')}/rerun",
                {},
            )
        return deploymodel.DeploymentRecord(
            id=f"dispatch-{to.sha[:7]}",
            sha=to.sha,
            status="running",
            created_at=time.time(),
            web_url=to.web_url,
            run_id=str(to.run_id),
        )
