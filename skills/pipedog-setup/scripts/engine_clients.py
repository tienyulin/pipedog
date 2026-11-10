"""Client construction and token resolution helpers for pipedog engine."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from types import ModuleType
from typing import Any

import config
import model
from forge import get_forge


def _client_factory() -> Callable[[str, str, str | None], Any]:
    from providers import get_client

    return get_client


def _mock_module() -> ModuleType:
    import mock

    return mock


class MockClient:
    """Adapts MockWorld.fetch(project, branch, now) to the provider Client contract."""

    def __init__(self, world: Any, clock: Callable[[], float] = time.time):
        self.world = world
        self.clock = clock

    def fetch(self, project: dict, branch: str | None, sha: str | None = None) -> model.Pipeline:
        return self.world.fetch(project, branch, self.clock())


class BrokenClient:
    """Stands in for a client that could not be constructed; always reports the error."""

    def __init__(self, message: str):
        self.message = message

    def fetch(self, project: dict, branch: str | None, sha: str | None = None) -> model.Pipeline:
        return error_pipeline(project, branch, self.message)


def project_ref(project: Mapping[str, Any]) -> str:
    return str(project.get("repo") or project.get("id") or project.get("name") or "?")


def error_pipeline(
    project: Mapping[str, Any],
    branch: str | None,
    message: str,
    now: float | None = None,
) -> model.Pipeline:
    return model.Pipeline(
        project=str(project.get("name") or "?"),
        short=str(project.get("short") or "?"),
        provider=project.get("provider", "gitlab"),
        project_ref=project_ref(project),
        local_path=project.get("local_path"),
        branch=branch,
        fetch_error=message,
        fetched_at=now if now is not None else time.time(),
    )


def resolve_tokens(
    cfg: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
    run: Callable[..., Any] | None = None,
) -> dict[str, str | None]:
    """Resolve tokens only for providers that are actually used."""
    used = {p["provider"] for p in cfg.get("projects", [])}
    tokens: dict[str, str | None] = {}
    if "gitlab" in used:
        tokens["gitlab"] = config.gitlab_token(cfg, env)
    if "github" in used:
        tokens["github"] = (
            config.github_token(cfg, env, run)
            if run is not None
            else config.github_token(cfg, env)
        )
    return tokens


def build_clients(
    cfg: Mapping[str, Any],
    tokens: Mapping[str, str | None],
    get_client: Callable[[str, str, str | None], Any],
) -> list[Any]:
    """One Client per project (a Client is never called concurrently)."""
    secrets = [t for t in tokens.values() if t]
    clients: list[Any] = []
    for proj in cfg["projects"]:
        prov = proj["provider"]
        base = cfg["gitlab"]["url"] if prov == "gitlab" else cfg["github"]["api_url"]
        try:
            clients.append(get_client(prov, base, tokens.get(prov)))
        except Exception as exc:  # noqa: BLE001 - surface as a per-project error
            msg = config.redact(
                f"client init failed: {type(exc).__name__}: {exc}", secrets
            )
            clients.append(BrokenClient(msg or "client init failed"))
    return clients


def build_forges(
    cfg: Mapping[str, Any],
    tokens: Mapping[str, str | None],
    forge_factory: Callable[[str, str, str | None], Any] = get_forge,
) -> list[Any]:
    """Build one Forge per project if the project configures flow, else None."""
    forges: list[Any] = []
    for proj in cfg["projects"]:
        if not proj.get("flow"):
            forges.append(None)
            continue
        prov = proj["provider"]
        base = cfg["gitlab"]["url"] if prov == "gitlab" else cfg["github"]["api_url"]
        try:
            forges.append(forge_factory(prov, base, tokens.get(prov)))
        except Exception:  # noqa: BLE001
            forges.append(None)
    return forges
