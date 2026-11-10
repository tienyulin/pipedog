"""GitLab Deployments & manual job deploy adapter."""

from __future__ import annotations

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

_STATUS_MAP: dict[str, deploymodel.EnvStatus] = {
    "success": "success",
    "failed": "failed",
    "canceled": "failed",
    "running": "running",
    "created": "running",
    "blocked": "waiting_approval",
}


class GitLabDeployAdapter:
    def __init__(self, base_url: str, token: str | None = None, **_: Any) -> None:
        raw = (base_url or "https://gitlab.com").rstrip("/")
        self.api_url = raw if raw.endswith("/api/v4") else f"{raw}/api/v4"
        self._token = token or None
        self._headers: dict[str, str] = {}
        if self._token:
            self._headers["PRIVATE-TOKEN"] = self._token

    @contextmanager
    def _wrap_errors(self, *, is_approve: bool = False) -> Iterator[None]:
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
    def _project_id(project: dict[str, Any]) -> str:
        pid = project.get("id") or project.get("project_id") or project.get("project_ref")
        if pid is None or str(pid) == "":
            raise DeployError("missing GitLab project id")
        return quote(str(pid), safe="")

    def _post_allow_empty(self, url: str, body: Any) -> Any:
        try:
            return http.post_json(url, self._headers, body)
        except http.HttpError as e:
            if e.status is None and e.message == "invalid JSON response":
                return {}
            raise

    @staticmethod
    def _dep_to_record(dep: dict[str, Any], default_sha: str = "") -> deploymodel.DeploymentRecord:
        raw_status = str(dep.get("status") or "").lower()
        st: deploymodel.EnvStatus = _STATUS_MAP.get(
            raw_status, "running" if not raw_status else "unknown"
        )
        deployable = dep.get("deployable")
        job_id: str | None = None
        web_url = dep.get("web_url")
        if isinstance(deployable, dict):
            if deployable.get("id") is not None:
                job_id = str(deployable["id"])
            if not web_url:
                web_url = deployable.get("web_url")
        created_at = http.parse_iso(dep.get("updated_at") or dep.get("created_at"))
        return deploymodel.DeploymentRecord(
            id=str(dep.get("id") or ""),
            sha=str(dep.get("sha") or default_sha),
            status=st,
            created_at=created_at,
            web_url=web_url,
            job_id=job_id,
        )

    def status(
        self, project: dict[str, Any], env: dict[str, Any] | deploymodel.EnvConfig
    ) -> deploymodel.EnvState:
        pid = self._project_id(project)
        name = env_name(env)
        plat = platform_env_name(env)
        with self._wrap_errors():
            deps = http.get_json(
                f"{self.api_url}/projects/{pid}/deployments?environment={quote(plat, safe='')}&order_by=id&sort=desc",
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
            rec = self._dep_to_record(deps[0])
            return deploymodel.EnvState(
                env=name,
                sha=rec.sha or None,
                status=rec.status,
                deployed_at=rec.created_at,
                web_url=rec.web_url,
                deployment_id=rec.id or None,
            )

    def find_deployment(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        sha: str,
    ) -> deploymodel.DeploymentRecord | None:
        pid = self._project_id(project)
        plat = platform_env_name(env)
        with self._wrap_errors():
            deps = http.get_json(
                f"{self.api_url}/projects/{pid}/deployments?environment={quote(plat, safe='')}&order_by=id&sort=desc",
                self._headers,
            )
            if not isinstance(deps, list):
                return None
            for dep in deps:
                if not isinstance(dep, dict):
                    continue
                if dep.get("sha") == sha:
                    return self._dep_to_record(dep, default_sha=sha)
        return None

    def history(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        limit: int = 10,
    ) -> list[deploymodel.DeploymentRecord]:
        pid = self._project_id(project)
        plat = platform_env_name(env)
        out: list[deploymodel.DeploymentRecord] = []
        with self._wrap_errors():
            deps = http.get_json(
                f"{self.api_url}/projects/{pid}/deployments?environment={quote(plat, safe='')}&order_by=id&sort=desc",
                self._headers,
            )
            if not isinstance(deps, list):
                return []
            for dep in deps[: max(0, int(limit))]:
                if not isinstance(dep, dict):
                    continue
                out.append(self._dep_to_record(dep))
        return out

    def pending_approval(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        sha: str | None = None,
    ) -> deploymodel.PendingGate | None:
        pid = self._project_id(project)
        name = env_name(env)
        plat = platform_env_name(env)
        with self._wrap_errors():
            deps = http.get_json(
                f"{self.api_url}/projects/{pid}/deployments?environment={quote(plat, safe='')}&order_by=id&sort=desc",
                self._headers,
            )
            if not isinstance(deps, list):
                return None
            for dep in deps:
                if not isinstance(dep, dict):
                    continue
                if str(dep.get("status") or "").lower() != "blocked":
                    continue
                if sha and dep.get("sha") and dep.get("sha") != sha:
                    continue
                rec = self._dep_to_record(dep, default_sha=sha or "")
                return deploymodel.PendingGate(
                    id=rec.id,
                    env=name,
                    sha=rec.sha,
                    environment_ids=[],
                    web_url=rec.web_url,
                )
        return None

    def _review_deployment(
        self,
        project: dict[str, Any],
        gate: deploymodel.PendingGate | str,
        status: str,
        comment: str,
    ) -> None:
        pid = self._project_id(project)
        dep_id = gate.id if isinstance(gate, deploymodel.PendingGate) else str(gate)
        with self._wrap_errors(is_approve=True):
            self._post_allow_empty(
                f"{self.api_url}/projects/{pid}/deployments/{quote(dep_id, safe='')}/approval",
                {"status": status, "comment": comment},
            )

    def approve(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        gate: deploymodel.PendingGate | str,
        comment: str = "",
    ) -> None:
        self._review_deployment(project, gate, "approved", comment)

    def reject(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        gate: deploymodel.PendingGate | str,
        comment: str = "",
    ) -> None:
        self._review_deployment(project, gate, "rejected", comment)

    def deploy(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        sha: str,
    ) -> deploymodel.DeploymentRecord:
        pid = self._project_id(project)
        env_d = as_env_dict(env)
        gl_cfg = env_d.get("gitlab") or {}
        raw_job = gl_cfg.get("job")
        if not raw_job:
            raise DeployError("gitlab.job is not configured")

        plat = platform_env_name(env_d)
        branch = after_branch(project, env_d)
        mapping = {
            "sha": sha,
            "env": plat,
            "branch": branch,
            "previous_sha": "",
            "previous_id": "",
        }
        job_name = substitute_placeholders(str(raw_job), mapping, shell_quote=False)

        with self._wrap_errors():
            pipes = http.get_json(
                f"{self.api_url}/projects/{pid}/pipelines?sha={quote(sha, safe='')}",
                self._headers,
            )
            if not isinstance(pipes, list) or not pipes or not isinstance(pipes[0], dict):
                raise DeployError(f"no pipeline found for sha {sha[:7]}")
            pipe_id = pipes[0].get("id")
            jobs = http.get_json(
                f"{self.api_url}/projects/{pid}/pipelines/{pipe_id}/jobs",
                self._headers,
            )
            if not isinstance(jobs, list):
                raise DeployError(f"no jobs found in pipeline {pipe_id}")

            target_job: dict[str, Any] | None = None
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                if job.get("name") == job_name and job.get("status") == "manual":
                    target_job = job
                    break
            if target_job is None:
                for job in jobs:
                    if isinstance(job, dict) and job.get("name") == job_name:
                        target_job = job
                        break
            if target_job is None or target_job.get("id") is None:
                raise DeployError(f"manual job {job_name!r} not found in pipeline {pipe_id}")

            job_id = str(target_job["id"])
            res = self._post_allow_empty(
                f"{self.api_url}/projects/{pid}/jobs/{quote(job_id, safe='')}/play",
                {},
            )
            played_id = str(res.get("id") or job_id) if isinstance(res, dict) else job_id
            web_url = (
                (res.get("web_url") if isinstance(res, dict) else None)
                or target_job.get("web_url")
            )
            return deploymodel.DeploymentRecord(
                id=played_id,
                sha=sha,
                status="running",
                created_at=time.time(),
                web_url=web_url,
                job_id=played_id,
            )

    def rollback(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        to: deploymodel.DeploymentRecord,
    ) -> deploymodel.DeploymentRecord:
        pid = self._project_id(project)
        if not to.job_id:
            raise DeployError(f"deployment {to.id} has no job_id to retry")
        with self._wrap_errors():
            res = self._post_allow_empty(
                f"{self.api_url}/projects/{pid}/jobs/{quote(str(to.job_id), safe='')}/retry",
                {},
            )
            new_job_id = (
                str(res.get("id") or to.job_id) if isinstance(res, dict) else str(to.job_id)
            )
            web_url = (
                (res.get("web_url") if isinstance(res, dict) else None)
                or to.web_url
            )
            return deploymodel.DeploymentRecord(
                id=str(to.id),
                sha=to.sha,
                status="running",
                created_at=time.time(),
                web_url=web_url,
                job_id=new_job_id,
            )
