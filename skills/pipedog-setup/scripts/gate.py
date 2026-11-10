"""CI and PR review gate classification.

Pure logic module determining whether a pipeline/run is green, yellow,
red, running, or canceled, and whether a PR/MR is approved, blocked,
waiting, or subject to no approval rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import model
from flowmodel import PrState

Color = Literal["green", "yellow", "red", "running", "canceled", "none"]
ReviewStatus = Literal["approved", "blocked", "waiting", "no_rule"]


@dataclass
class Light:
    """CI status classification result."""

    color: Color
    reasons: list[dict[str, Any]] = field(default_factory=list)


def _all_leaves(p: model.Pipeline) -> list[model.Job]:
    """Collect all leaf jobs under the pipeline (expanding group jobs)."""
    leaves: list[model.Job] = []
    for job in p.jobs:
        leaves.extend(job.leaves())
    return leaves


def classify_ci(p: model.Pipeline) -> Light:
    """Classify a pipeline's status into a Light signal.

    Rules:
    - p.status is None -> "none"
    - Any leaf status == "failed" and not allow_failure -> "red"
    - Any running / pending -> "running"
    - Any leaf raw_status in {"canceled", "cancelled"} -> "canceled"
    - Any leaf allow_failure and failed, or raw_status in {"neutral", "action_required"} -> "yellow" (with reasons)
    - Otherwise -> "green"
    """
    if p.status is None:
        return Light(color="none")

    leaves = _all_leaves(p)

    # 1. Any non-allow-failure failure is immediately red.
    if any(leaf.status == "failed" and not leaf.allow_failure for leaf in leaves):
        return Light(color="red")
    if not leaves and p.status == "failed":
        return Light(color="red")

    # 2. Any active job means the run is still in flight.
    if any(leaf.status in ("running", "pending") for leaf in leaves):
        return Light(color="running")
    if not leaves and p.status in ("running", "pending"):
        return Light(color="running")

    # 3. Explicitly canceled run.
    canceled_names = {"canceled", "cancelled"}
    if any((leaf.raw_status or "").lower() in canceled_names for leaf in leaves):
        return Light(color="canceled")
    if (getattr(p, "raw_status", None) or "").lower() in canceled_names:
        return Light(color="canceled")

    # 4. Yellow light (allow-failure failure, neutral, action_required).
    reasons: list[dict[str, Any]] = []
    for leaf in leaves:
        raw = (leaf.raw_status or "").lower()
        if leaf.status == "failed" and leaf.allow_failure:
            reasons.append({"job": leaf.name, "kind": "allow_failure"})
        elif raw in {"neutral", "action_required"}:
            reasons.append({"job": leaf.name, "kind": raw})

    if reasons:
        return Light(color="yellow", reasons=reasons)

    # 5. Otherwise green.
    return Light(color="green")


def classify_review(pr: PrState) -> ReviewStatus:
    """Classify PR review status.

    Rules:
    - changes_requested or unresolved_threads > 0 -> "blocked"
    - review_required -> "waiting"
    - none -> "no_rule"
    - approved -> "approved"
    """
    if pr.review == "changes_requested" or pr.unresolved_threads > 0:
        return "blocked"
    if pr.review == "review_required":
        return "waiting"
    if pr.review == "none":
        return "no_rule"
    if pr.review == "approved":
        return "approved"
    raise ValueError(f"Unknown review status: {pr.review!r}")
