"""Action executor for flow automation.

Executes Action items returned by flow.step against the Forge and store.
Stdlib only.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
import time
from typing import Any, Literal

import config
import flow
import flowmodel
import gate
import store
from flowmodel import (
    Action,
    Ask,
    Emit,
    Merge,
    Notify,
    OpenPr,
    TrackedRef,
    Untrack,
    VoidDecisions,
)
from forge import Forge, ForgeError


def _track_next_target_if_intermediate(
    project_name: str,
    target_branch: str,
    chain: list[str],
    root: Path | None,
) -> None:
    next_tgt = flow.next_target(chain, target_branch)
    if next_tgt is None:
        return

    def _add(owned: list[TrackedRef]) -> list[TrackedRef] | None:
        if any(r.project == project_name and r.branch == target_branch for r in owned):
            return None
        owned.append(
            TrackedRef(
                project=project_name,
                branch=target_branch,
                target=next_tgt,
                pr=None,
                owned=True,
            )
        )
        return owned

    store.update_owned(_add, root=root)


def _remove_owned(project_name: str, branch: str, root: Path | None) -> None:
    def _rm(owned: list[TrackedRef]) -> list[TrackedRef] | None:
        filtered = [
            r for r in owned if not (r.project == project_name and r.branch == branch)
        ]
        return filtered if len(filtered) != len(owned) else None

    store.update_owned(_rm, root=root)


def _pr_label(project: dict[str, Any] | None, number: int | None) -> str:
    if number is None:
        return ""
    provider = (project or {}).get("provider")
    return f"MR !{number}" if provider == "gitlab" else f"PR #{number}"


def _upsert_owned(ref: TrackedRef, root: Path | None) -> None:
    def _up(owned: list[TrackedRef]) -> list[TrackedRef]:
        for idx, existing in enumerate(owned):
            if existing.project == ref.project and existing.branch == ref.branch:
                owned[idx] = TrackedRef(
                    project=ref.project,
                    branch=ref.branch,
                    target=ref.target or existing.target,
                    pr=ref.pr if ref.pr is not None else existing.pr,
                    owned=True,
                    head_sha=ref.head_sha or existing.head_sha,
                    ci_fix_rounds=existing.ci_fix_rounds,
                    review_fix_rounds=existing.review_fix_rounds,
                    notified=list(existing.notified),
                )
                return owned
        owned.append(ref)
        return owned

    store.update_owned(_up, root=root)


def apply(
    actions: Sequence[Action],
    *,
    forge: Forge | None,
    project: dict[str, Any],
    notify: Callable[[str], None] | None = None,
    root: Path | None = None,
    secrets: Sequence[str | None] = (),
) -> list[str]:
    """Execute actions via forge/store/notify; return redacted log messages written to actions.log."""
    logs: list[str] = []
    flow_cfg = project.get("flow") or {}
    chain: list[str] = list(flow_cfg.get("chain") or ["feature/*", "develop", "main"])
    reviewers: list[str] = list(flow_cfg.get("reviewers") or [])

    def _log(raw_msg: str) -> None:
        clean = config.redact(raw_msg, secrets) or raw_msg
        store.log_action(clean, root=root)
        logs.append(clean)

    def _notify(raw_msg: str) -> None:
        if notify is not None:
            clean = config.redact(raw_msg, secrets) or raw_msg
            try:
                notify(clean)
            except Exception:
                pass

    for action in actions:
        if isinstance(action, OpenPr):
            if forge is None:
                continue
            head_sha = getattr(action, "head_sha", None) or (project.get("head_sha") if project else None)
            title = action.title
            if hasattr(forge, "get_commit_subject") and head_sha:
                subj = forge.get_commit_subject(project, head_sha)
                if subj:
                    title = subj
                elif not title:
                    title = action.source
            elif not title:
                title = action.source

            body = None
            if head_sha:
                body = f"Opened by pipedog (pd) after CI passed on {head_sha[:7]}."

            pr = forge.create_pr(
                project,
                action.source,
                action.target,
                title,
                reviewers,
                body=body,
            )
            _upsert_owned(
                TrackedRef(
                    project=action.project,
                    branch=action.source,
                    target=action.target,
                    pr=pr.number,
                    owned=True,
                    head_sha=pr.head_sha or None,
                ),
                root,
            )
            ref_label = _pr_label(project, pr.number)
            _log(
                f"opened {ref_label} {action.project} {action.source}→{action.target} ({pr.url})"
            )

        elif isinstance(action, Ask):
            store.put_decision(action.decision, root=root)
            _log(
                f"asked {action.decision.kind} decision {action.decision.id} "
                f"for {action.decision.project} {action.decision.source}→{action.decision.target}"
            )
            _notify(
                f"Pipedog: decision needed ({action.decision.kind}) "
                f"{action.decision.project} {action.decision.source}→{action.decision.target}"
            )

        elif isinstance(action, Emit):
            store.put_event(action.event, root=root)
            _log(
                f"emitted {action.event.type} event {action.event.id} "
                f"for {action.event.project} {action.event.branch}"
            )

        elif isinstance(action, VoidDecisions):
            for d in store.list_decisions(root=root):
                if (
                    d.project == action.project
                    and d.source == action.source
                    and d.status in ("pending", "answered")
                ):
                    d.status = "void"
                    d.note = action.reason
                    d.answered_at = time.time()
                    store.put_decision(d, root=root)
            _log(f"voided decisions for {action.project} {action.source}: {action.reason}")

        elif isinstance(action, Untrack):
            _remove_owned(action.project, action.branch, root)
            _log(f"untracked {action.project} {action.branch}")

        elif isinstance(action, Notify):
            _notify(action.message)
            _log(f"notify: {action.message}")

        elif isinstance(action, Merge):
            if forge is None:
                continue
            d = store.get_decision(action.decision_id, root=root)
            if d is None or d.status != "answered" or d.answer != "merge" or d.pr is None:
                continue

            pr = forge.get_pr(project, d.pr)
            rev = gate.classify_review(pr)
            if (
                pr.state != "open"
                or pr.head_sha != d.head_sha
                or rev in ("blocked", "waiting")
            ):
                d.status = "void"
                d.note = "head moved"
                d.answered_at = time.time()
                store.put_decision(d, root=root)
                _log(f"voided decision {d.id} before merge: head moved")
                continue

            transition = flow.transition_of(chain, d.source, d.target)
            merge_map = flow_cfg.get("merge") or {}
            m_cfg = merge_map.get(transition) or {}
            lane = flow.lane_for(flow_cfg, d.source)
            target = flow.target_for(flow_cfg, d.source)
            is_first = (lane is None) and (target == chain[1] if len(chain) >= 2 else True)
            method: Literal["squash", "merge"] = m_cfg.get(
                "method", "merge" if lane is not None else ("squash" if is_first else "merge")
            )
            delete_branch = bool(
                m_cfg.get("delete_branch", True if (lane is not None or is_first) else False)
            )
            is_literal_chain = any(
                c == d.source and not any(ch in c for ch in "*?[") for c in chain
            )
            if is_literal_chain:
                delete_branch = False

            try:
                forge.merge(
                    project,
                    d.pr,
                    method=method,
                    delete_branch=delete_branch,
                    expected_sha=d.head_sha,
                )
            except ForgeError as exc:
                if "head moved" in str(exc).lower():
                    d.status = "void"
                    d.note = "head moved"
                    d.answered_at = time.time()
                    store.put_decision(d, root=root)
                    _log(f"voided decision {d.id} during merge: head moved")
                    continue
                raise

            d.status = "executed"
            store.put_decision(d, root=root)
            _remove_owned(d.project, d.source, root)

            src_idx = flow._chain_index(chain, d.source)
            tgt_idx = flow._chain_index(chain, d.target)
            is_backward = src_idx is not None and tgt_idx is not None and tgt_idx < src_idx
            is_lane = lane is not None
            if not is_lane and not is_backward:
                _track_next_target_if_intermediate(d.project, d.target, chain, root)

            ref_label = _pr_label(project, d.pr)
            _log(f"merged {ref_label} {d.project} {d.source}→{d.target} ({method})")

            if lane is not None and lane.get("back_merge"):
                into_branch = lane["into"]
                bm_branch = lane["back_merge"]
                bm_title = f"Back-merge {d.source} into {bm_branch}"
                bm_body = f"Opened by pipedog (pd) after merging {d.source} into {into_branch}."
                try:
                    bm_pr = forge.create_pr(
                        project,
                        into_branch,
                        bm_branch,
                        bm_title,
                        reviewers,
                        body=bm_body,
                    )
                    bm_ref = TrackedRef(
                        project=d.project,
                        branch=into_branch,
                        target=bm_branch,
                        pr=bm_pr.number,
                        owned=True,
                        head_sha=bm_pr.head_sha or None,
                    )
                    _upsert_owned(bm_ref, root)
                    bm_ref_label = _pr_label(project, bm_pr.number)
                    _log(f"opened back-merge {bm_ref_label} {d.project} {into_branch}→{bm_branch}")
                except ForgeError as exc:
                    found_pr = None
                    try:
                        found_pr = forge.find_open_pr(project, into_branch, bm_branch)
                    except Exception:
                        pass
                    if found_pr is not None:
                        bm_ref = TrackedRef(
                            project=d.project,
                            branch=into_branch,
                            target=bm_branch,
                            pr=found_pr.number,
                            owned=True,
                            head_sha=found_pr.head_sha or None,
                        )
                        _upsert_owned(bm_ref, root)
                        bm_ref_label = _pr_label(project, found_pr.number)
                        _log(f"opened back-merge {bm_ref_label} {d.project} {into_branch}→{bm_branch}")
                    else:
                        _log(f"back-merge PR failed: {exc}")
                        _notify(f"back-merge PR failed: {exc}")

    return logs
