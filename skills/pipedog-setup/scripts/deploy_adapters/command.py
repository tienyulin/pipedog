"""Local CLI command deploy adapter."""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any, Callable

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

_STRIPPED_ENV_KEYS = (
    "PIPEDOG_GITLAB_TOKEN",
    "GITLAB_TOKEN",
    "PIPEDOG_GITHUB_TOKEN",
    "GITHUB_TOKEN",
)

_JSON_STATUS_MAP: dict[str, deploymodel.EnvStatus] = {
    "success": "success",
    "healthy": "success",
    "succeeded": "success",
    "ok": "success",
    "failed": "failed",
    "degraded": "failed",
    "error": "failed",
    "unhealthy": "failed",
    "running": "running",
    "progressing": "running",
    "in_progress": "running",
    "pending": "pending",
    "waiting_approval": "waiting_approval",
    "waiting": "waiting_approval",
    "none": "none",
    "unknown": "unknown",
}


def _default_runner(
    cmd: str,
    *,
    cwd: str | None = None,
    timeout: float = 300.0,
    env: dict[str, str] | None = None,
) -> Any:
    return subprocess.run(
        ["/bin/sh", "-c", cmd],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class CommandDeployAdapter:
    def __init__(
        self,
        base_url: str = "",
        token: str | None = None,
        *,
        runner: Callable[..., Any] | None = None,
        **_: Any,
    ) -> None:
        self.base_url = base_url
        self._token = token or None
        self._runner = runner or _default_runner

    def _clean_env(self) -> tuple[dict[str, str], list[str | None]]:
        clean: dict[str, str] = {}
        secrets: list[str | None] = [self._token]
        for k, v in os.environ.items():
            if k in _STRIPPED_ENV_KEYS:
                secrets.append(v)
                continue
            clean[k] = v
        return clean, secrets

    @staticmethod
    def _tail_stderr(stderr: str | None, secrets: list[str | None], lines: int = 20) -> str:
        raw = (stderr or "").strip()
        if not raw:
            return "command failed"
        tail = "\n".join(raw.splitlines()[-lines:])
        return redact_secrets(tail, secrets)

    def _exec(
        self,
        project: dict[str, Any],
        cmd_cfg: dict[str, Any],
        cmd_str: str,
    ) -> tuple[Any, list[str | None]]:
        cwd = project.get("local_path") or None
        timeout = float(cmd_cfg.get("timeout_sec") or 300.0)
        clean_env, secrets = self._clean_env()
        try:
            proc = self._runner(cmd_str, cwd=cwd, timeout=timeout, env=clean_env)
        except (subprocess.TimeoutExpired, TimeoutError) as e:
            raise DeployError(f"command timeout after {timeout}s") from e
        except Exception as e:
            raise DeployError(redact_secrets(str(e), secrets)) from None
        return proc, secrets

    def status(
        self, project: dict[str, Any], env: dict[str, Any] | deploymodel.EnvConfig
    ) -> deploymodel.EnvState:
        env_d = as_env_dict(env)
        name = env_name(env_d)
        plat = platform_env_name(env_d)
        cmd_cfg = env_d.get("command") or {}
        raw_cmd = cmd_cfg.get("status")
        if not raw_cmd:
            raise DeployError("command.status is not configured")

        mapping = {
            "sha": "",
            "env": plat,
            "branch": after_branch(project, env_d),
            "previous_sha": "",
            "previous_id": "",
        }
        cmd_str = substitute_placeholders(str(raw_cmd), mapping, shell_quote=True)
        parse_mode = str(cmd_cfg.get("parse") or "json").lower()

        proc, secrets = self._exec(project, cmd_cfg, cmd_str)
        rc = int(getattr(proc, "returncode", 0))
        stdout = str(getattr(proc, "stdout", "") or "")
        stderr = str(getattr(proc, "stderr", "") or "")

        if parse_mode == "kubectl-rollout":
            if rc == 0:
                st: deploymodel.EnvStatus = "success"
            elif "timed out" in stderr.lower() or "timed out" in stdout.lower():
                st = "running"
            else:
                st = "failed"
            return deploymodel.EnvState(env=name, sha=None, status=st)

        if parse_mode == "exit-code":
            st = "success" if rc == 0 else "failed"
            return deploymodel.EnvState(env=name, sha=None, status=st)

        if rc != 0:
            raise DeployError(self._tail_stderr(stderr or stdout, secrets))

        try:
            data = json.loads(stdout)
        except Exception:
            raise DeployError("invalid JSON output from status command") from None

        if not isinstance(data, dict):
            raise DeployError("status JSON output must be an object")

        if parse_mode == "argocd":
            status_obj = data.get("status") if isinstance(data.get("status"), dict) else {}
            sync_obj = status_obj.get("sync") if isinstance(status_obj.get("sync"), dict) else {}
            health_obj = (
                status_obj.get("health") if isinstance(status_obj.get("health"), dict) else {}
            )
            op_obj = (
                status_obj.get("operationState")
                if isinstance(status_obj.get("operationState"), dict)
                else {}
            )

            sha = sync_obj.get("revision")
            health = str(health_obj.get("status") or "")
            phase = str(op_obj.get("phase") or "")
            if phase == "Running":
                argo_st: deploymodel.EnvStatus = "running"
            elif health == "Healthy":
                argo_st = "success"
            elif health == "Degraded":
                argo_st = "failed"
            elif health == "Progressing":
                argo_st = "running"
            else:
                argo_st = "unknown"

            deployed_at = http.parse_iso(
                op_obj.get("finishedAt") or status_obj.get("reconciledAt")
            )
            return deploymodel.EnvState(
                env=name,
                sha=str(sha) if sha else None,
                status=argo_st,
                deployed_at=deployed_at,
            )

        # Default: parse_mode == "json"
        sha_val = data.get("sha") or data.get("revision")
        raw_st = str(data.get("status") or data.get("health") or "").lower()
        mapped_st: deploymodel.EnvStatus = _JSON_STATUS_MAP.get(raw_st, "unknown")
        raw_deployed = data.get("deployed_at")
        deployed_at = (
            float(raw_deployed)
            if isinstance(raw_deployed, (int, float))
            else http.parse_iso(str(raw_deployed) if raw_deployed else None)
        )
        dep_id = data.get("id") or data.get("deployment_id")
        return deploymodel.EnvState(
            env=name,
            sha=str(sha_val) if sha_val else None,
            status=mapped_st,
            deployed_at=deployed_at,
            web_url=data.get("web_url"),
            deployment_id=str(dep_id) if dep_id is not None else None,
        )

    def history(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        limit: int = 10,
    ) -> list[deploymodel.DeploymentRecord]:
        return []

    def find_deployment(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        sha: str,
    ) -> deploymodel.DeploymentRecord | None:
        st = self.status(project, env)
        if st.sha == sha and st.status != "none":
            return deploymodel.DeploymentRecord(
                id=st.deployment_id or f"cmd-{sha[:7]}",
                sha=sha,
                status=st.status,
                created_at=st.deployed_at,
                web_url=st.web_url,
            )
        return None

    def pending_approval(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        sha: str | None = None,
    ) -> deploymodel.PendingGate | None:
        return None

    def approve(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        gate: deploymodel.PendingGate | str,
        comment: str = "",
    ) -> None:
        raise DeployError("command adapter does not support remote approval")

    def reject(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        gate: deploymodel.PendingGate | str,
        comment: str = "",
    ) -> None:
        raise DeployError("command adapter does not support remote approval")

    def deploy(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        sha: str,
    ) -> deploymodel.DeploymentRecord:
        env_d = as_env_dict(env)
        plat = platform_env_name(env_d)
        cmd_cfg = env_d.get("command") or {}
        raw_cmd = cmd_cfg.get("deploy")
        if not raw_cmd:
            raise DeployError("command.deploy is not configured")

        mapping = {
            "sha": sha,
            "env": plat,
            "branch": after_branch(project, env_d),
            "previous_sha": "",
            "previous_id": "",
        }
        cmd_str = substitute_placeholders(str(raw_cmd), mapping, shell_quote=True)
        proc, secrets = self._exec(project, cmd_cfg, cmd_str)
        if int(getattr(proc, "returncode", 0)) != 0:
            stderr = str(getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "")
            raise DeployError(self._tail_stderr(stderr, secrets))

        return deploymodel.DeploymentRecord(
            id=f"cmd-{sha[:7]}",
            sha=sha,
            status="running",
            created_at=time.time(),
        )

    def rollback(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        to: deploymodel.DeploymentRecord,
    ) -> deploymodel.DeploymentRecord:
        env_d = as_env_dict(env)
        plat = platform_env_name(env_d)
        cmd_cfg = env_d.get("command") or {}
        raw_cmd = cmd_cfg.get("rollback") or cmd_cfg.get("deploy")
        if not raw_cmd:
            raise DeployError("command.rollback is not configured")

        mapping = {
            "sha": to.sha,
            "previous_sha": to.sha,
            "previous_id": to.id,
            "env": plat,
            "branch": after_branch(project, env_d),
        }
        cmd_str = substitute_placeholders(str(raw_cmd), mapping, shell_quote=True)
        proc, secrets = self._exec(project, cmd_cfg, cmd_str)
        if int(getattr(proc, "returncode", 0)) != 0:
            stderr = str(getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "")
            raise DeployError(self._tail_stderr(stderr, secrets))

        return deploymodel.DeploymentRecord(
            id=to.id or f"cmd-{to.sha[:7]}",
            sha=to.sha,
            status="running",
            created_at=time.time(),
            web_url=to.web_url,
        )
