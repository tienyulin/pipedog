"""Base protocol, error type, and shared helpers for deploy adapters."""

from __future__ import annotations

import dataclasses
import shlex
from typing import Any, Mapping, Protocol, runtime_checkable

import deploymodel


class DeployError(Exception):
    """Deployment adapter error. Message must never contain tokens or secrets."""


@runtime_checkable
class DeployAdapter(Protocol):
    def status(
        self, project: dict[str, Any], env: dict[str, Any] | deploymodel.EnvConfig
    ) -> deploymodel.EnvState: ...

    def history(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        limit: int = 10,
    ) -> list[deploymodel.DeploymentRecord]: ...

    def pending_approval(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        sha: str | None = None,
    ) -> deploymodel.PendingGate | None: ...

    def approve(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        gate: deploymodel.PendingGate | str,
        comment: str = "",
    ) -> None: ...

    def reject(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        gate: deploymodel.PendingGate | str,
        comment: str = "",
    ) -> None: ...

    def deploy(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        sha: str,
    ) -> deploymodel.DeploymentRecord: ...

    def rollback(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        to: deploymodel.DeploymentRecord,
    ) -> deploymodel.DeploymentRecord: ...

    def find_deployment(
        self,
        project: dict[str, Any],
        env: dict[str, Any] | deploymodel.EnvConfig,
        sha: str,
    ) -> deploymodel.DeploymentRecord | None: ...


def as_env_dict(env: Mapping[str, Any] | deploymodel.EnvConfig) -> dict[str, Any]:
    if dataclasses.is_dataclass(env) and not isinstance(env, type):
        return dataclasses.asdict(env)
    return dict(env)


def env_name(env: Mapping[str, Any] | deploymodel.EnvConfig) -> str:
    d = as_env_dict(env)
    return str(d.get("name") or "")


def platform_env_name(env: Mapping[str, Any] | deploymodel.EnvConfig) -> str:
    d = as_env_dict(env)
    return str(d.get("environment") or d.get("name") or "")


def after_branch(
    project: Mapping[str, Any],
    env: Mapping[str, Any] | deploymodel.EnvConfig | None = None,
) -> str:
    deploy_cfg = project.get("deploy")
    if isinstance(deploy_cfg, Mapping) and deploy_cfg.get("after_branch"):
        return str(deploy_cfg["after_branch"])
    flow_cfg = project.get("flow")
    if isinstance(flow_cfg, Mapping):
        chain = flow_cfg.get("chain")
        if isinstance(chain, list) and chain:
            return str(chain[-1])
    branch = project.get("branch")
    if isinstance(branch, str) and branch and branch != "auto":
        return branch
    return "main"


def substitute_placeholders(
    template: str,
    mapping: Mapping[str, Any],
    *,
    shell_quote: bool = False,
) -> str:
    result = str(template)
    for key in ("sha", "previous_sha", "previous_id", "env", "branch"):
        placeholder = "{" + key + "}"
        if placeholder in result:
            raw_val = "" if mapping.get(key) is None else str(mapping[key])
            rep = shlex.quote(raw_val) if shell_quote else raw_val
            result = result.replace(placeholder, rep)
    return result


def redact_secrets(text: str, secrets: list[str | None]) -> str:
    out = str(text)
    for secret in secrets:
        if secret and len(secret) >= 4:
            out = out.replace(secret, "***")
    return out
