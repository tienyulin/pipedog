"""Pure state machine for flow automation (feature -> develop -> main).

No I/O is performed here; callers supply an Observation and execute the
returned Action list via flow_exec.
"""

from __future__ import annotations

import dataclasses
import fnmatch
from dataclasses import dataclass, field
from typing import Any

import flowmodel
import gate
import model
import rules as rules_mod
import snapshot
import store
from flowmodel import (
    Action,
    Ask,
    Decision,
    Emit,
    Event,
    Merge,
    Notify,
    OpenPr,
    PrState,
    Rule,
    TrackedRef,
    Untrack,
    VoidDecisions,
)


@dataclass
class Observation:
    """Snapshot of external state for a single TrackedRef at one poll."""

    pipeline: model.Pipeline | None
    pr: PrState | None
    existing_decisions: list[Decision]
    rules: list[Rule]
    now: float
    seen_comments: set[str] = field(default_factory=set)


def _layer_name(pattern: str, branch: str) -> str:
    """Turn a chain pattern like 'feature/*' into its layer name ('feature')."""
    if "/*" in pattern:
        prefix = pattern.split("/*", 1)[0]
        if prefix:
            return prefix
    if any(ch in pattern for ch in "*?["):
        stripped = pattern.rstrip("*?[]").rstrip("/-_")
        if stripped:
            return stripped
        if "/" in branch:
            return branch.split("/", 1)[0]
    return pattern


def _chain_index(chain: list[str], branch: str) -> int | None:
    for idx, pattern in enumerate(chain):
        if branch == pattern or fnmatch.fnmatchcase(branch, pattern):
            return idx
    return None


def next_target(chain: list[str], branch: str) -> str | None:
    """Return the next branch in the chain after branch, or None if at the end / unmatched."""
    idx = _chain_index(chain, branch)
    if idx is None or idx + 1 >= len(chain):
        return None
    return chain[idx + 1]


def transition_of(chain: list[str], source: str, target: str) -> str:
    """Format transition name (e.g. 'feature→develop' for 'feature/x' -> 'develop')."""
    src_idx = _chain_index(chain, source)
    if src_idx is not None:
        src_name = _layer_name(chain[src_idx], source)
    elif "/" in source:
        src_name = source.split("/", 1)[0]
    else:
        src_name = source

    tgt_idx = _chain_index(chain, target)
    if tgt_idx is not None:
        tgt_name = _layer_name(chain[tgt_idx], target)
    else:
        tgt_name = target

    return f"{src_name}→{tgt_name}"


def _is_feature_branch(chain: list[str], branch: str) -> bool:
    if not chain:
        return False
    return branch == chain[0] or fnmatch.fnmatchcase(branch, chain[0])


def lane_for(flow_cfg: dict[str, Any], branch: str) -> dict[str, Any] | None:
    """Return the first lane whose branch equals or fnmatches branch."""
    for lane in flow_cfg.get("lanes") or []:
        pat = lane.get("branch", "")
        if branch == pat or fnmatch.fnmatchcase(branch, pat):
            return lane
    return None


def target_for(flow_cfg: dict[str, Any], branch: str) -> str | None:
    """Return the lane's into target if branch matches a lane, else next_target."""
    lane = lane_for(flow_cfg, branch)
    if lane is not None:
        return lane.get("into")
    chain = list(flow_cfg.get("chain") or [])
    return next_target(chain, branch)


def is_entry_branch(flow_cfg: dict[str, Any], branch: str) -> bool:
    """True if branch matches chain[0] (feature branch) or any lane."""
    chain = list(flow_cfg.get("chain") or [])
    if _is_feature_branch(chain, branch):
        return True
    return lane_for(flow_cfg, branch) is not None


def _failed_jobs_payload(pipeline: model.Pipeline | None) -> list[dict[str, Any]]:
    if pipeline is None:
        return []
    entries = [
        snapshot._failed_entry(pipeline, leaf, parent)
        for leaf, parent in snapshot.failed_leaves(pipeline)
    ]
    if not entries and pipeline.status == "failed":
        entries.append(
            {
                "repo": pipeline.project,
                "provider": pipeline.provider,
                "local_path": pipeline.local_path,
                "branch": pipeline.branch,
                "pipeline_id": pipeline.pipeline_id,
                "web_url": pipeline.web_url,
                "job_name": "pipeline",
                "parent_job": None,
                "job_id": pipeline.pipeline_id,
                "job_web_url": pipeline.web_url,
                "file": None,
                "line": None,
                "error_summary": pipeline.fetch_error,
                "log_tail": "",
            }
        )
    return entries


def _yellow_jobs_payload(
    pipeline: model.Pipeline | None,
    unallowed: list[dict[str, Any]],
    ref: TrackedRef,
    project: dict[str, Any],
) -> list[dict[str, Any]]:
    if pipeline is not None:
        entries = []
        for leaf, parent in snapshot.yellow_leaves(pipeline):
            if any(r.get("job") in ("*", leaf.name) for r in unallowed):
                entries.append(snapshot._failed_entry(pipeline, leaf, parent))
        if entries:
            return entries
    out = []
    for r in unallowed:
        out.append(
            {
                "repo": ref.project,
                "provider": getattr(pipeline, "provider", None) if pipeline else None,
                "local_path": project.get("local_path"),
                "branch": ref.branch,
                "pipeline_id": getattr(pipeline, "pipeline_id", None) if pipeline else None,
                "web_url": getattr(pipeline, "web_url", None) if pipeline else None,
                "job_name": r.get("job", ""),
                "parent_job": None,
                "job_id": r.get("job_id"),
                "job_web_url": r.get("web_url"),
                "file": r.get("file"),
                "line": r.get("line"),
                "error_summary": r.get("error_summary"),
                "log_tail": r.get("log_tail", ""),
            }
        )
    return out


def _resolve_yellow(
    *,
    reasons: list[dict[str, Any]],
    ref: TrackedRef,
    obs: Observation,
    active_decisions: list[Decision],
    transition: str,
    target: str,
    current_sha: str,
    project: dict[str, Any],
    actions: list[Action],
) -> bool:
    """Evaluate yellow reasons against rules and existing decisions.

    Returns True if all yellow reasons are allowed (treat as green);
    returns False if an action was appended or we must wait.
    """
    unallowed: list[dict[str, Any]] = []
    for reason in reasons:
        matched = rules_mod.match(
            obs.rules,
            project=ref.project,
            transition=transition,
            job=str(reason.get("job", "*")),
            kind=str(reason.get("kind", "*")),
        )
        if matched is None or matched.action != "allow":
            unallowed.append(reason)

    if not unallowed:
        return True

    yellow_decisions = [d for d in active_decisions if d.kind == "yellow"]
    if not yellow_decisions:
        ask_key = f"ask:yellow:{current_sha}"
        if ask_key not in ref.notified:
            ref.notified.append(ask_key)
            web_url = (
                obs.pipeline.web_url
                if (obs.pipeline and obs.pipeline.web_url)
                else None
            )
            if web_url:
                reasons_to_store = [
                    dict(r, web_url=web_url, transition=transition)
                    for r in unallowed
                ]
            else:
                reasons_to_store = [
                    dict(r, transition=transition) if "transition" in r else r
                    for r in unallowed
                ]
            dec = Decision(
                id=store.new_id("dec", now=obs.now),
                kind="yellow",
                project=ref.project,
                source=ref.branch,
                target=target,
                pr=obs.pr.number if obs.pr else ref.pr,
                head_sha=current_sha,
                reasons=reasons_to_store,
                created_at=obs.now,
            )
            actions.append(Ask(decision=dec))
        return False

    latest = yellow_decisions[-1]
    if latest.status == "pending":
        return False

    if latest.status == "answered":
        if latest.answer in ("allow", "once"):
            return True
        if latest.answer == "fix":
            fix_key = f"yellow_fix:{current_sha}:{latest.id}"
            if fix_key not in ref.notified:
                ref.notified.append(fix_key)
                failed_jobs = _yellow_jobs_payload(obs.pipeline, unallowed, ref, project)
                ev = Event(
                    id=store.new_id("ev", now=obs.now),
                    type="yellow_fix",
                    project=ref.project,
                    branch=ref.branch,
                    local_path=project.get("local_path"),
                    pr=obs.pr.number if obs.pr else ref.pr,
                    head_sha=current_sha,
                    payload={"failed_jobs": failed_jobs, "reasons": unallowed},
                    created_at=obs.now,
                )
                actions.append(Emit(event=ev))
            return False

    return False


def step(
    ref: TrackedRef,
    obs: Observation,
    flow_cfg: dict[str, Any],
    project: dict[str, Any],
) -> tuple[TrackedRef, list[Action]]:
    """Advance ref by one state-machine step given obs; return (updated_ref, actions)."""
    new_ref = dataclasses.replace(ref, notified=list(ref.notified))
    actions: list[Action] = []
    chain: list[str] = list(flow_cfg.get("chain") or [])
    max_fix_rounds = int(flow_cfg.get("max_fix_rounds", 3))

    # 1. PR merged or closed externally -> untrack (and notify if merged).
    if obs.pr is not None and obs.pr.state in ("merged", "closed"):
        if obs.pr.state == "merged":
            ref_label = (
                f"MR !{obs.pr.number}"
                if (project or {}).get("provider") == "gitlab"
                else f"PR #{obs.pr.number}"
            )
            actions.append(
                Notify(
                    message=f"merged {new_ref.project} {obs.pr.source}→{obs.pr.target} ({ref_label})"
                )
            )
        actions.append(Untrack(project=new_ref.project, branch=new_ref.branch))
        return new_ref, actions

    # 2. Detect head SHA movement and void stale decisions.
    pipe_sha = obs.pipeline.head_sha if obs.pipeline and obs.pipeline.head_sha else None
    pr_sha = obs.pr.head_sha if obs.pr and obs.pr.head_sha else None
    current_sha = pr_sha or pipe_sha or new_ref.head_sha or ""

    head_moved = (
        new_ref.head_sha is not None
        and bool(current_sha)
        and new_ref.head_sha != current_sha
    ) or any(
        d.status in ("pending", "answered")
        and bool(current_sha)
        and d.head_sha != current_sha
        for d in obs.existing_decisions
    )
    if head_moved:
        actions.append(
            VoidDecisions(
                project=new_ref.project,
                source=new_ref.branch,
                reason="head moved",
            )
        )
    if current_sha:
        new_ref.head_sha = current_sha

    active_decisions = [
        d
        for d in obs.existing_decisions
        if d.status in ("pending", "answered")
        and (not current_sha or d.head_sha == current_sha)
    ]

    if obs.pr is not None:
        new_ref.pr = obs.pr.number
        new_ref.target = obs.pr.target
    elif new_ref.target is None:
        new_ref.target = target_for(flow_cfg, new_ref.branch)

    target = new_ref.target or target_for(flow_cfg, new_ref.branch) or ""
    transition = transition_of(chain, new_ref.branch, target) if target else new_ref.branch
    is_feature = is_entry_branch(flow_cfg, new_ref.branch)

    if obs.pr is not None and pr_sha and pr_sha != pipe_sha:
        light = gate.Light(color="running")
    elif obs.pipeline is not None:
        light = gate.classify_ci(obs.pipeline)
    else:
        light = gate.Light(color="none")

    if light.color in ("none", "running"):
        return new_ref, actions

    # 8. Canceled pipeline -> notify once and stop.
    if light.color == "canceled":
        canc_key = f"canceled:{current_sha}"
        if canc_key not in new_ref.notified:
            new_ref.notified.append(canc_key)
            actions.append(
                Notify(
                    message=f"{new_ref.project} {new_ref.branch} pipeline canceled ({current_sha})"
                )
            )
        return new_ref, actions

    # 3 & 7. CI red -> intermediate branch notifies once; feature branch emits ci_failed up to max_fix_rounds.
    if light.color == "red":
        if not is_feature:
            red_key = f"red:{current_sha}"
            if red_key not in new_ref.notified:
                new_ref.notified.append(red_key)
                actions.append(
                    Notify(
                        message=f"{new_ref.project} {new_ref.branch} CI failed ({current_sha})"
                    )
                )
            return new_ref, actions

        emit_key = f"ci_failed:{current_sha}"
        if emit_key in new_ref.notified:
            return new_ref, actions

        if new_ref.ci_fix_rounds < max_fix_rounds:
            new_ref.ci_fix_rounds += 1
            new_ref.notified.append(emit_key)
            ev = Event(
                id=store.new_id("ev", now=obs.now),
                type="ci_failed",
                project=new_ref.project,
                branch=new_ref.branch,
                local_path=project.get("local_path"),
                pr=obs.pr.number if obs.pr else new_ref.pr,
                head_sha=current_sha,
                payload={"failed_jobs": _failed_jobs_payload(obs.pipeline)},
                created_at=obs.now,
            )
            actions.append(Emit(event=ev))
        else:
            give_up_key = f"give_up_ci:{new_ref.branch}:{current_sha}"
            if give_up_key not in new_ref.notified:
                new_ref.notified.append(give_up_key)
                actions.append(
                    Notify(
                        message=(
                            f"auto-fix gave up on {new_ref.project} {new_ref.branch} "
                            f"after {new_ref.ci_fix_rounds} rounds"
                        )
                    )
                )
        return new_ref, actions

    # 4. CI yellow -> check rules and yellow decisions.
    if light.color == "yellow":
        allowed = _resolve_yellow(
            reasons=light.reasons,
            ref=new_ref,
            obs=obs,
            active_decisions=active_decisions,
            transition=transition,
            target=target,
            current_sha=current_sha,
            project=project,
            actions=actions,
        )
        if not allowed:
            return new_ref, actions

    # CI is green (or yellow allowed as green).
    new_ref.ci_fix_rounds = 0

    # 5 & 7. No PR yet.
    if obs.pr is None:
        if is_feature:
            if new_ref.pr is None and target:
                new_ref.target = target
                title = str(project.get("last_commit_subject") or new_ref.branch)
                actions.append(
                    OpenPr(
                        project=new_ref.project,
                        source=new_ref.branch,
                        target=target,
                        title=title,
                        head_sha=current_sha,
                    )
                )
            return new_ref, actions

        # 7. Intermediate branch (e.g. develop) with a next target in chain.
        if target:
            new_ref.target = target
            promote_decisions = [d for d in active_decisions if d.kind == "promote"]
            if promote_decisions:
                latest_p = promote_decisions[-1]
                if latest_p.status == "answered" and latest_p.answer == "open":
                    open_key = f"promoted:{current_sha}:{latest_p.id}"
                    if open_key not in new_ref.notified:
                        new_ref.notified.append(open_key)
                        actions.append(
                            OpenPr(
                                project=new_ref.project,
                                source=new_ref.branch,
                                target=target,
                                title=f"Promote {new_ref.branch} to {target}",
                            )
                        )
                return new_ref, actions

            promote_ask_key = f"ask:promote:{current_sha}"
            if promote_ask_key not in new_ref.notified:
                new_ref.notified.append(promote_ask_key)
                summary = f"{new_ref.branch} CI green at {current_sha[:7]}"
                commits_ahead = getattr(obs, "commits_ahead", None) or (
                    getattr(obs.pr, "commits_ahead", None) if obs.pr else None
                )
                if commits_ahead is not None:
                    summary += f", {commits_ahead} commits ahead of {target}"
                dec = Decision(
                    id=store.new_id("dec", now=obs.now),
                    kind="promote",
                    project=new_ref.project,
                    source=new_ref.branch,
                    target=target,
                    pr=None,
                    head_sha=current_sha,
                    reasons=[{"summary": summary}],
                    created_at=obs.now,
                )
                actions.append(Ask(decision=dec))
        return new_ref, actions

    # 6. Open PR exists -> evaluate review gate.
    rev = gate.classify_review(obs.pr)
    unseen_ids = [
        cid for cid in obs.pr.comment_ids if cid not in obs.seen_comments
    ]
    new_ids = [
        cid
        for cid in unseen_ids
        if cid not in new_ref.notified
        and f"comment:{cid}" not in new_ref.notified
        and f"comment:{current_sha}:{cid}" not in new_ref.notified
    ]
    rev_sha_key = f"review_changes:{current_sha}"
    set_key = f"review_changes:{current_sha}:{','.join(unseen_ids)}"
    should_emit = (bool(new_ids) and set_key not in new_ref.notified) or (
        rev == "blocked"
        and not obs.pr.comment_ids
        and rev_sha_key not in new_ref.notified
    )

    if should_emit:
        if new_ref.review_fix_rounds < max_fix_rounds:
            new_ref.review_fix_rounds += 1
            for cid in new_ids:
                new_ref.notified.append(f"comment:{cid}")
                new_ref.notified.append(f"comment:{current_sha}:{cid}")
            if set_key not in new_ref.notified:
                new_ref.notified.append(set_key)
            if rev_sha_key not in new_ref.notified:
                new_ref.notified.append(rev_sha_key)
            ev = Event(
                id=store.new_id("ev", now=obs.now),
                type="review_changes",
                project=new_ref.project,
                branch=new_ref.branch,
                local_path=project.get("local_path"),
                pr=obs.pr.number,
                head_sha=current_sha,
                payload={"comment_ids": new_ids},
                created_at=obs.now,
            )
            actions.append(Emit(event=ev))
        else:
            give_up_rev = f"give_up_review:{new_ref.branch}:{current_sha}"
            if give_up_rev not in new_ref.notified:
                new_ref.notified.append(give_up_rev)
                actions.append(
                    Notify(
                        message=(
                            f"auto-fix gave up on review for {new_ref.project} {new_ref.branch} "
                            f"after {new_ref.review_fix_rounds} rounds"
                        )
                    )
                )
        return new_ref, actions

    if unseen_ids or rev == "blocked":
        return new_ref, actions

    if rev == "waiting":
        return new_ref, actions

    if rev == "no_rule":
        allowed = _resolve_yellow(
            reasons=[{"job": "*", "kind": "no_approval_rule"}],
            ref=new_ref,
            obs=obs,
            active_decisions=active_decisions,
            transition=transition,
            target=obs.pr.target,
            current_sha=current_sha,
            project=project,
            actions=actions,
        )
        if not allowed:
            return new_ref, actions

    # Review is approved (or no_rule allowed) and CI is green.
    new_ref.review_fix_rounds = 0
    merge_decisions = [d for d in active_decisions if d.kind == "merge"]
    if merge_decisions:
        latest_m = merge_decisions[-1]
        if latest_m.status == "answered" and latest_m.answer == "merge":
            actions.append(Merge(decision_id=latest_m.id))
        return new_ref, actions

    merge_ask_key = f"ask:merge:{current_sha}"
    if merge_ask_key not in new_ref.notified:
        new_ref.notified.append(merge_ask_key)
        parts: list[str] = []
        if rev == "no_rule":
            parts.append("no approval rule (allowed)")
        elif obs.pr.approvals_given is not None and obs.pr.approvals_required is not None:
            parts.append(f"approved {obs.pr.approvals_given}/{obs.pr.approvals_required}")
        else:
            parts.append("approved")
        parts.append("CI green")
        if obs.pr.mergeable is True:
            parts.append("mergeable")
        summary = ", ".join(parts)
        dec = Decision(
            id=store.new_id("dec", now=obs.now),
            kind="merge",
            project=new_ref.project,
            source=new_ref.branch,
            target=obs.pr.target,
            pr=obs.pr.number,
            head_sha=current_sha,
            reasons=[{"summary": summary}],
            created_at=obs.now,
        )
        actions.append(Ask(decision=dec))

    return new_ref, actions
