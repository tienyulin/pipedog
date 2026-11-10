"""Data types for flow automation (feature -> develop -> main).

Contract shared by rules / store / gate / forge / flow / flow_exec / cli.
Stdlib only. See docs/superpowers/specs/2026-11-08-flow-automation-design.md.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Union, get_args, get_origin, get_type_hints

Transition = str  # e.g. "feature→develop", "develop→main"


@dataclass
class PrState:
    number: int
    url: str
    source: str
    target: str
    head_sha: str
    state: Literal["open", "merged", "closed"]
    # "none" = the repo has no approval rule at all (treated as a yellow light)
    review: Literal["approved", "changes_requested", "review_required", "none"]
    unresolved_threads: int = 0
    comment_ids: list[str] = field(default_factory=list)  # rc-/ic-/rv-/d- prefixed ids
    author_is_me: bool = False
    approvals_given: Optional[int] = None
    approvals_required: Optional[int] = None
    mergeable: Optional[bool] = None


@dataclass
class TrackedRef:
    project: str                 # config project name
    branch: str                  # source branch being driven up the chain
    target: Optional[str] = None    # next branch in the chain
    pr: Optional[int] = None
    owned: bool = False          # PR was opened by pd (kept tracked after checkout moves)
    head_sha: Optional[str] = None
    ci_fix_rounds: int = 0
    review_fix_rounds: int = 0
    notified: list[str] = field(default_factory=list)  # one-shot notification keys already sent


@dataclass
class Rule:
    id: str
    action: Literal["allow", "ask"]
    project: str                 # "*" allowed
    transition: str              # "*" allowed
    job: str                     # fnmatch glob, "*" allowed
    kind: str                    # allow_failure | neutral | action_required | no_approval_rule | "*"
    created_at: str              # local ISO 8601
    source_run: Optional[str] = None


@dataclass
class Decision:
    id: str
    kind: Literal["yellow", "merge", "promote"]
    project: str
    source: str
    target: str
    pr: Optional[int]
    head_sha: str
    reasons: list[dict[str, Any]]   # yellow: [{"job": str, "kind": str}]; others: free-form summary
    created_at: float
    status: Literal["pending", "answered", "executed", "void"] = "pending"
    answer: Optional[str] = None       # allow | once | fix | merge | open | skip
    remember: bool = False
    note: Optional[str] = None         # e.g. void reason
    answered_by: Optional[str] = None  # cli | tui | ...
    answered_at: Optional[float] = None


@dataclass
class Event:
    id: str
    type: Literal["ci_failed", "yellow_fix", "review_changes"]
    project: str
    branch: str
    local_path: Optional[str]
    pr: Optional[int]
    head_sha: str
    payload: dict[str, Any]         # ci_failed/yellow_fix: {"failed_jobs": [...]} ; review_changes: {"comment_ids": [...]}
    created_at: float
    acked_at: Optional[float] = None
    notified_at: Optional[float] = None


# ---- actions returned by flow.step (executed by flow_exec.apply) ----

@dataclass
class OpenPr:
    project: str
    source: str
    target: str
    title: str
    head_sha: Optional[str] = None


@dataclass
class Ask:
    decision: Decision


@dataclass
class Emit:
    event: Event


@dataclass
class Merge:
    decision_id: str


@dataclass
class Notify:
    message: str


@dataclass
class VoidDecisions:
    project: str
    source: str
    reason: str


@dataclass
class Untrack:
    project: str
    branch: str


Action = Union[OpenPr, Ask, Emit, Merge, Notify, VoidDecisions, Untrack]


def to_json(obj: Any) -> dict[str, Any]:
    """Dataclass -> plain dict (JSON-serializable)."""
    return dataclasses.asdict(obj)


def from_json(cls: type, data: dict[str, Any]) -> Any:
    """Plain dict -> dataclass; nested dataclass fields (e.g. Ask.decision) are rebuilt.

    Unknown keys are ignored so older/newer state files stay readable.
    """
    try:
        hints = get_type_hints(cls)
    except Exception:
        hints = {}
        for k, v in getattr(cls, "__annotations__", {}).items():
            if isinstance(v, str):
                target_cls = globals().get(v)
                if target_cls is not None:
                    hints[k] = target_cls
            elif dataclasses.is_dataclass(v):
                hints[k] = v

    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        typ = hints.get(f.name)
        if dataclasses.is_dataclass(typ) and isinstance(value, dict):
            value = from_json(typ, value)  # type: ignore[arg-type]
        elif get_origin(typ) is Union:
            for arg in get_args(typ):
                if dataclasses.is_dataclass(arg) and isinstance(value, dict):
                    value = from_json(arg, value)
                    break
        kwargs[f.name] = value
    return cls(**kwargs)
