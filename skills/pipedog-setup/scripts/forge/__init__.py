"""Platform forge interfaces and factory for GitHub and GitLab."""

from __future__ import annotations

from typing import Any, Literal
import flowmodel


class ForgeError(Exception):
    """Platform write or review API error. Message never contains credentials."""


class Forge:
    def me(self) -> str:
        raise NotImplementedError

    def find_open_pr(self, project: dict, source: str, target: str) -> flowmodel.PrState | None:
        raise NotImplementedError

    def get_pr(self, project: dict, number: int) -> flowmodel.PrState:
        raise NotImplementedError

    def get_commit_subject(self, project: dict, sha: str) -> str | None:
        raise NotImplementedError

    def create_pr(
        self,
        project: dict,
        source: str,
        target: str,
        title: str,
        reviewers: list[str] = (),
        **kwargs: Any,
    ) -> flowmodel.PrState:
        raise NotImplementedError

    def list_comments(self, project: dict, number: int) -> list[dict[str, Any]]:
        raise NotImplementedError

    def reply(self, project: dict, number: int, comment_id: str, body: str) -> str:
        raise NotImplementedError

    def merge(
        self,
        project: dict,
        number: int,
        *,
        method: Literal["squash", "merge"],
        delete_branch: bool,
        expected_sha: str,
    ) -> None:
        raise NotImplementedError


def get_forge(provider: str, base_url: str, token: str | None) -> Forge:
    if provider == "github":
        from forge.github import GitHubForge
        return GitHubForge(base_url, token)
    if provider == "gitlab":
        from forge.gitlab import GitLabForge
        return GitLabForge(base_url, token)
    raise ValueError(f"unknown provider: {provider}")
