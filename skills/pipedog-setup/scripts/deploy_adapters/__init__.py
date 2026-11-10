"""Deploy adapter registry and public interface."""

from __future__ import annotations

from typing import Any, Callable

from deploy_adapters.base import DeployAdapter, DeployError
from deploy_adapters.command import CommandDeployAdapter
from deploy_adapters.github import GitHubDeployAdapter
from deploy_adapters.gitlab import GitLabDeployAdapter

_REGISTRY: dict[str, Callable[..., DeployAdapter]] = {}


def register(name: str, factory: Callable[..., DeployAdapter]) -> None:
    _REGISTRY[name] = factory


def get(
    name: str,
    base_url: str = "",
    token: str | None = None,
    **kwargs: Any,
) -> DeployAdapter:
    factory = _REGISTRY.get(name)
    if factory is None:
        raise ValueError(f"unknown deploy adapter: {name}")
    return factory(base_url=base_url, token=token, **kwargs)


register("github", GitHubDeployAdapter)
register("gitlab", GitLabDeployAdapter)
register("command", CommandDeployAdapter)

__all__ = [
    "CommandDeployAdapter",
    "DeployAdapter",
    "DeployError",
    "GitHubDeployAdapter",
    "GitLabDeployAdapter",
    "get",
    "register",
]
