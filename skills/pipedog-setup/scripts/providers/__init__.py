"""Provider registry: pick a client implementation by provider name."""

from __future__ import annotations

from types import ModuleType
from typing import Protocol

import model


class Client(Protocol):
    """What every provider client (gitlab, github, mock) offers."""

    def fetch(self, project: dict, branch: str | None) -> model.Pipeline: ...


def get_provider(name: str) -> ModuleType:
    """Return the provider module (``gitlab`` / ``github``) for ``name``."""
    if name == "gitlab":
        from providers import gitlab

        return gitlab
    if name == "github":
        from providers import github

        return github
    raise ValueError(f"unknown provider: {name!r}")


def get_client(provider: str, base_url: str, token: str | None) -> Client:
    """Build a client for ``provider``; ``fetch`` never raises."""
    return get_provider(provider).Client(base_url, token)
