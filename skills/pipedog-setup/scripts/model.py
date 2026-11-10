"""Shared data model for pipedog.

This module is the contract between providers (GitLab / GitHub / mock),
the renderer and the snapshot writer. It must stay dependency-free
(stdlib only) so every module can import it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional

# Normalized status. Providers MUST map their native statuses onto these.
#   failed  : failed (GitLab failed, GitHub failure / timed_out / startup_failure)
#   running : running
#   pending : created / pending / waiting_for_resource / preparing / scheduled / queued / waiting / requested
#   passed  : success
#   skipped : skipped / manual (not started) / canceled / neutral / action_required
Status = Literal["failed", "running", "pending", "passed", "skipped"]
STATUSES: tuple[str, ...] = ("failed", "running", "pending", "passed", "skipped")

Provider = Literal["gitlab", "github", "mock"]


@dataclass
class ErrorInfo:
    """Root cause extracted from a failed job's trace log."""

    error_summary: str                # one line, ANSI-free, <= 200 chars
    file: str | None = None           # repo-relative path (CI build-dir prefix stripped)
    line: int | None = None
    log_tail: str = ""                # last 30 clean lines, "\n"-joined


@dataclass
class Job:
    """A job, or a group of sibling jobs aggregated under one parent.

    Leaf job:   sub_jobs == [] ; job_id / web_url / started_at describe the job.
    Group job:  sub_jobs != [] ; status / started_at are aggregated
                (see aggregate_status); job_id may be None.
    Sub-jobs are always leaves (one level of nesting only).
    """

    name: str                          # display name; for groups the shared prefix, e.g. "deploy", "test"
    status: Status
    stage: str | None = None
    job_id: int | str | None = None
    web_url: str | None = None
    started_at: float | None = None    # epoch seconds (UTC); None if not started
    finished_at: float | None = None   # epoch seconds (UTC); None if not finished
    sub_jobs: list["Job"] = field(default_factory=list)
    error: ErrorInfo | None = None     # only for failed leaves
    raw_status: str | None = None      # provider-native status/conclusion, e.g. "neutral", "cancelled"
    allow_failure: bool = False        # GitLab allow_failure (a failed job here is a yellow light)
    workflow: Optional[str] = None

    def leaves(self) -> list["Job"]:
        return self.sub_jobs if self.sub_jobs else [self]

    def passed_count(self) -> int:
        return sum(1 for j in self.leaves() if j.status == "passed")

    def total_count(self) -> int:
        return len(self.leaves())


@dataclass
class Pipeline:
    """Latest pipeline (GitLab) / workflow run (GitHub) for one tracked project."""

    project: str                       # display name from config, e.g. "auth-service"
    short: str                         # short label for PASSED tags, e.g. "auth"
    provider: Provider
    project_ref: str                   # GitLab project id, or GitHub "owner/repo"
    local_path: str | None = None
    branch: str | None = None          # resolved branch (None = latest on any ref)
    pipeline_id: int | str | None = None
    status: Status | None = None       # None when no pipeline found / fetch error
    web_url: str | None = None
    jobs: list[Job] = field(default_factory=list)
    fetch_error: str | None = None     # human readable, e.g. "HTTP 401 Unauthorized"
    fetched_at: float | None = None    # epoch seconds when this data was fetched
    head_sha: str | None = None        # commit SHA the pipeline/run was built for
    target: str | None = None          # target branch if tracked ref or PR target

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Precedence used when aggregating a group's status.
_PRECEDENCE: tuple[str, ...] = ("failed", "running", "pending", "passed", "skipped")


def aggregate_status(statuses: list[str]) -> Status:
    """failed > running > pending > passed > skipped.

    A group with any failed leaf is failed (even if siblings still run),
    so it surfaces in the FAILED section immediately.
    Empty list -> "skipped".
    """
    for s in _PRECEDENCE:
        if s in statuses:
            return s  # type: ignore[return-value]
    return "skipped"


def make_group(name: str, children: list[Job]) -> Job:
    """Build a group Job from leaf children (aggregated status / times)."""
    starts = [c.started_at for c in children if c.started_at is not None]
    status = aggregate_status([c.status for c in children])
    finishes = [c.finished_at for c in children if c.finished_at is not None]
    finished = status in ("failed", "passed", "skipped") and len(finishes) == len(children)
    return Job(
        name=name,
        status=status,
        stage=children[0].stage if children else None,
        started_at=min(starts) if starts else None,
        finished_at=max(finishes) if finished and finishes else None,
        sub_jobs=children,
        workflow=children[0].workflow if children else None,
    )
