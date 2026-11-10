"""Terminal UI renderer for pipedog.

Implements the stage-chain pipeline layout, unified icon set, and pure
`render()` function specified in docs/DESIGN.md and task-chain-view.md.
Stdlib only, Python >= 3.8 compatible.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import os
import shutil
import time
from typing import Any, Dict, List, Optional, Tuple

import cli
import flowmodel
import keys
import model
import tui

from render_chain import (
    ChainView,
    _format_error_detail,
    format_duration,
)
from render_icons import (
    KIND_COLORS,
    KIND_SHORT_LABELS,
    TermCaps,
    UNIFIED_ICONS,
    _DUMB_TERMS,
    _FANCY_TERM_KEYWORDS,
    _FANCY_TERM_PROGRAMS,
    arrow,
    branch,
    connector,
    detect_caps,
    get_color,
    get_icon,
    sep,
)
from render_stages import (
    Stage,
    _get_job_start,
    _stages_for_jobs,
    compute_stage_status,
    get_pipeline_stages,
    group_stages_github,
    group_stages_gitlab,
)

__all__ = [
    "BANNER",
    "ChainView",
    "KIND_COLORS",
    "KIND_SHORT_LABELS",
    "Stage",
    "TermCaps",
    "UNIFIED_ICONS",
    "_DUMB_TERMS",
    "_FANCY_TERM_KEYWORDS",
    "_FANCY_TERM_PROGRAMS",
    "_build_subtitle",
    "_format_decision_summary",
    "_format_error_detail",
    "_get_job_start",
    "_pipeline_duration",
    "_pipeline_header",
    "_pipeline_sort_key",
    "_pr_line",
    "_render_environments",
    "_render_waiting",
    "_render_warnings",
    "_stages_for_jobs",
    "arrow",
    "branch",
    "compute_stage_status",
    "connector",
    "detect_caps",
    "format_duration",
    "get_color",
    "get_icon",
    "get_pipeline_stages",
    "group_stages_github",
    "group_stages_gitlab",
    "render",
    "render_footer",
    "sep",
]

BANNER: str = r"""        __              _                _
(\_____/|''-o     _ __ (_) _ __  ___  __| | ___  __ _
(     (_|\_/     | '_ \| || '_ \/ -_)/ _` |/ _ \/ _` |
 \  __  /        | .__/|_|| .__/\___|\__,_|\___/\__, |
  \_) \_)        |_|      |_|                   |___/"""


_YELLOW_KIND_TEXT: dict[str, str] = {
    "no_approval_rule": "no required review on {target} · A = always allow",
    "allow_failure": "failed (allowed to fail)",
    "neutral": "finished neutral",
    "action_required": "needs action on the CI page",
}


def _yellow_kind_text(kind: str, target: str) -> str:
    text = _YELLOW_KIND_TEXT.get(kind)
    return text.format(target=target or "target") if text else kind


def _format_decision_summary(d: flowmodel.Decision) -> str:
    parts: list[str] = []
    for r in d.reasons:
        if not isinstance(r, dict):
            continue
        if r.get("summary"):
            parts.append(str(r["summary"]))
        elif r.get("kind"):
            job = r.get("job")
            text = _yellow_kind_text(str(r["kind"]), d.target or "")
            if job and job != "*":
                parts.append(f"{job} {text}")
            else:
                parts.append(text)
    if parts:
        summary = ", ".join(parts)
    elif d.kind == "merge":
        summary = "approved, CI green"
    elif d.kind == "promote":
        summary = (
            f"{d.source} CI green at {d.head_sha[:7]}"
            if d.head_sha
            else f"{d.source} CI green"
        )
    elif d.kind == "deploy":
        summary = (
            f"{d.source} verified {d.head_sha[:7]}"
            if d.head_sha
            else f"deploy to {d.target}"
        )
    elif d.kind in ("deploy_approval", "approve"):
        summary = f"platform gate {d.target} waiting for approval"
    elif d.kind == "rollback":
        sha_part = (
            f"; roll back to {d.head_sha[:7]}" if d.head_sha else "; roll back"
        )
        summary = f"verify failed on {d.target or d.source}{sha_part}"
    elif d.kind in ("deploy_stuck", "stuck"):
        summary = f"rollback failed on {d.target or d.source}, retry?"
    else:
        summary = "yellow light"

    if summary:
        clean = (
            summary.lower()
            .replace("→", " ")
            .replace("->", " ")
            .replace(":", " ")
            .replace("(", " ")
            .replace(")", " ")
            .replace("/", " ")
        )
        words = set(clean.split())
        known_kind_words = {
            d.kind.lower(),
            KIND_SHORT_LABELS.get(d.kind, "").lower(),
            "deploy",
            "approval",
            "approve",
            "rollback",
            "stuck",
            "promote",
        }
        target_words = set(d.target.lower().replace("/", " ").split()) if d.target else set()
        source_words = set(d.source.lower().replace("/", " ").split()) if d.source else set()
        allowed_words = known_kind_words | target_words | source_words | {"to"}
        if words and words.issubset(allowed_words):
            return ""

    return summary


def _build_subtitle(
    pipelines: list[model.Pipeline],
    *,
    ts: float,
    caps: TermCaps,
) -> tui.Line:
    """Build Panel subtitle with local update time and per-project pipeline refs."""
    time_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    subtitle = tui.Line(f"updated {time_str}", style="dim")

    for p in pipelines:
        subtitle.append(sep(caps.fancy), style="dim")
        proj_branch = f"{p.project}@{p.branch}" if p.branch else p.project
        subtitle.append(proj_branch, style="dim")
        if p.pipeline_id is not None:
            subtitle.append(" ", style="dim")
            pid_text = f"#{p.pipeline_id}"
            start = len(subtitle)
            subtitle.append(pid_text, style="dim")
            if caps.fancy and caps.hyperlinks and p.web_url:
                subtitle.stylize(f"link {p.web_url}", start, len(subtitle))

    return subtitle


# ---------------------------------------------------------------------------
# Section Builders
# ---------------------------------------------------------------------------


def _pipeline_duration(pipeline: model.Pipeline, now: float) -> str:
    """Compute elapsed duration string for pipeline header."""
    d = getattr(pipeline, "duration", None) or getattr(pipeline, "duration_seconds", None)
    if d is not None:
        return format_duration(d)

    leaves: list[model.Job] = []
    for j in pipeline.jobs:
        leaves.extend(j.leaves())

    starts = [l.started_at for l in leaves if l.started_at is not None]
    finishes = [l.finished_at for l in leaves if l.finished_at is not None]

    if not starts:
        return ""

    t_start = min(starts)
    if pipeline.status == "running":
        elapsed = max(0.0, now - t_start)
        return format_duration(elapsed)
    elif finishes:
        t_finish = max(finishes)
        return format_duration(max(0.0, t_finish - t_start))
    elif pipeline.fetched_at is not None and pipeline.status in ("passed", "failed"):
        return format_duration(max(0.0, pipeline.fetched_at - t_start))
    return ""


def _pipeline_header(
    pipeline: model.Pipeline,
    pr: flowmodel.PrState | None,
    now: float,
    fancy: bool,
) -> tui.Row:
    """Build the header line: project · branch[ → target] [· PR #n | · sha7] + duration."""
    left = tui.Line(no_wrap=True, overflow="ellipsis")
    left.append(pipeline.project, style="bold")

    branch = pipeline.branch
    target = getattr(pipeline, "target", None)
    if not target and hasattr(pipeline, "tracked_ref") and getattr(pipeline, "tracked_ref"):
        target = getattr(pipeline.tracked_ref, "target", None)
    if not target and pr:
        target = pr.target

    if branch:
        left.append(sep(fancy), style="dim")
        if target and target != branch:
            left.append(branch, style="cyan")
            left.append(f" {arrow(fancy)} ", style="dim")
            left.append(target, style="cyan")
        else:
            left.append(branch, style="cyan")

    is_gitlab = pipeline.provider == "gitlab"
    if pr and pr.state == "open":
        pr_label = f"MR !{pr.number}" if is_gitlab else f"PR #{pr.number}"
        left.append(sep(fancy), style="dim")
        left.append(pr_label, style="blue")
    elif (
        hasattr(pipeline, "tracked_ref")
        and getattr(pipeline, "tracked_ref", None)
        and getattr(pipeline.tracked_ref, "pr", None)
    ):
        pr_num = pipeline.tracked_ref.pr
        pr_label = f"MR !{pr_num}" if is_gitlab else f"PR #{pr_num}"
        left.append(sep(fancy), style="dim")
        left.append(pr_label, style="blue")
    elif pipeline.head_sha:
        left.append(sep(fancy), style="dim")
        left.append(pipeline.head_sha[:7], style="dim")
    elif (
        hasattr(pipeline, "tracked_ref")
        and getattr(pipeline, "tracked_ref", None)
        and getattr(pipeline.tracked_ref, "head_sha", None)
    ):
        left.append(sep(fancy), style="dim")
        left.append(pipeline.tracked_ref.head_sha[:7], style="dim")

    dur_str = _pipeline_duration(pipeline, now)

    if pipeline.fetch_error is not None:
        dur_style = "red"
    elif pipeline.status == "failed":
        dur_style = "red"
    elif pipeline.status == "running":
        dur_style = "yellow"
    elif pipeline.status == "passed":
        dur_style = "green"
    else:
        dur_style = "dim"

    return tui.Row(
        left,
        tui.Line(dur_str, style=dur_style, no_wrap=True),
    )


def _pr_line(
    pipeline: model.Pipeline,
    pr: flowmodel.PrState,
    fancy: bool,
) -> tui.Line:
    """Build the single-line PR / MR summary line."""
    prefix = "  MR: " if pipeline.provider == "gitlab" else "  PR: "
    line = tui.Line(no_wrap=True, overflow="ellipsis")
    line.append(prefix, style="dim")

    segments: list[list[tuple[str, str]]] = []

    if pr.review == "approved":
        if pr.approvals_given is not None and pr.approvals_required is not None:
            text = f"{get_icon('passed', fancy)} approved {pr.approvals_given}/{pr.approvals_required}"
        else:
            text = f"{get_icon('passed', fancy)} approved"
        segments.append([(text, "green")])
    elif pr.review == "changes_requested":
        ic = get_icon("failed", fancy)
        segments.append([(ic, "bold red"), (" changes requested", "red")])
    elif pr.review == "review_required":
        text = f"{get_icon('pending', fancy)} review required"
        segments.append([(text, "dim")])
    elif pr.review == "none":
        text = f"{get_icon('warning', fancy)} no approval rule"
        segments.append([(text, "yellow")])

    if pr.unresolved_threads > 0:
        c_str = (
            "1 comment open"
            if pr.unresolved_threads == 1
            else f"{pr.unresolved_threads} comments open"
        )
        segments.append([(c_str, "yellow")])

    if pr.mergeable is True:
        ic = get_icon("passed", fancy)
        segments.append([(f"{ic} mergeable", "green")])
    elif pr.mergeable is False:
        ic = get_icon("failed", fancy)
        segments.append([(ic, "bold red"), (" conflicts", "red")])

    for idx, seg in enumerate(segments):
        if idx > 0:
            line.append(sep(fancy), style="dim")
        for text, style in seg:
            line.append(text, style=style)

    return line


KIND_FOOTER_ACTIONS: dict[str, list[tuple[str, str]]] = {
    "yellow": [("a", "once"), ("A", "always"), ("f", "fix"), ("n", "skip")],
    "merge": [("m", "merge"), ("n", "skip")],
    "promote": [("p", "open"), ("n", "skip")],
    "deploy": [("d", "deploy"), ("n", "skip")],
    "deploy_approval": [("v", "approve"), ("x", "reject"), ("n", "skip")],
    "rollback": [("b", "rollback"), ("n", "skip")],
    "deploy_stuck": [("t", "retry"), ("n", "skip")],
}


def render_footer(
    selection_state: Any,
    decision: flowmodel.Decision | None,
    caps: Any = None,
    collapse_hint: str | None = None,
) -> tui.Line | None:
    """Render footer line for WAITING section based on selection and confirmation state."""
    fancy = getattr(caps, "fancy", True)

    # 1. In confirm mode show the confirm prompt
    if selection_state is not None:
        confirm_prompt = getattr(selection_state, "confirm_prompt", None)
        if confirm_prompt:
            return tui.Line(confirm_prompt, style="bold yellow")
        confirm_mode = getattr(selection_state, "confirm_mode", None)
        if confirm_mode:
            if confirm_mode == "high":
                act = getattr(selection_state, "confirm_action", None)
                ans = act[0] if act else "proceed"
                sha7 = (getattr(selection_state, "confirm_target_sha", "") or "")[:7]
                env = decision.target if decision else ""
                typed = getattr(selection_state, "confirm_typed", "")
                return tui.Line(f"type prod + Enter to {ans} {sha7} on {env}, Esc cancels: {typed}", style="bold yellow")
            elif confirm_mode == "medium":
                act = getattr(selection_state, "confirm_action", None)
                ans = act[0] if act else "proceed"
                key = getattr(selection_state, "confirm_key", "key")
                return tui.Line(f"press {key} again within 3s to {ans}", style="bold yellow")

    # 2. When a decision is selected, list only valid keys for its kind
    if decision is not None:
        actions = KIND_FOOTER_ACTIONS.get(decision.kind)
        if actions is None:
            valid = cli.VALID_ANSWERS.get(decision.kind, set())
            actions = []
            for k, (ans, rem) in keys.KEY_MAP.items():
                if ans in valid and (k, ans) not in actions:
                    actions.append((k, ans))
            if not actions:
                actions = [("n", "skip")]
        items = [f"{k} {label}" for k, label in actions]
        items.append("w web")
        arrow_str = "↑↓" if fancy else "up/down"
        items.append(f"{arrow_str} move")
        return tui.Line("  ".join(items), style="dim")

    # 3. Nothing selected and pending exist: 1-9 / ↑↓ select
    has_pending = getattr(selection_state, "has_pending", True) if selection_state is not None else False
    if selection_state is not None and has_pending:
        arrow_str = "↑↓" if fancy else "up/down"
        base = f"1-9 / {arrow_str} select"
        if collapse_hint:
            return tui.Line(f"{base}  {collapse_hint}", style="dim")
        return tui.Line(base, style="dim")

    # 4. No pending: if collapse_hint applies, return collapse_hint
    if collapse_hint:
        return tui.Line(collapse_hint, style="dim")

    return None


def _render_waiting(
    decisions: Sequence[flowmodel.Decision],
    selected: int | None,
    fancy: bool,
    now: float | None = None,
    *,
    selection_state: Any = None,
    caps: Any = None,
    collapse_hint: str | None = None,
) -> list[Any]:
    if now is None:
        now = time.time()
    wait_hdr = f"{get_icon('needs_you', fancy)} WAITING FOR YOU"
    lines: list[Any] = [tui.Line(wait_hdr, style="bold magenta", no_wrap=True)]

    for idx, d in enumerate(decisions, start=1):
        summary = _format_decision_summary(d)
        if not summary:
            if d.kind == "merge":
                summary = "approved, CI green"
            elif d.kind == "promote":
                summary = (
                    f"{d.source} CI green at {d.head_sha[:7]}"
                    if d.head_sha
                    else f"{d.source} CI green"
                )
            elif d.kind == "deploy":
                summary = (
                    f"{d.source} verified {d.head_sha[:7]}"
                    if d.head_sha
                    else f"deploy to {d.target}"
                )
            elif d.kind in ("deploy_approval", "approve"):
                summary = f"platform gate {d.target} waiting for approval"
            elif d.kind == "rollback":
                sha_part = (
                    f"; roll back to {d.head_sha[:7]}" if d.head_sha else "; roll back"
                )
                summary = f"verify failed on {d.target or d.source}{sha_part}"
            elif d.kind in ("deploy_stuck", "stuck"):
                summary = f"rollback failed on {d.target or d.source}, retry?"
            else:
                summary = "yellow light"

        is_selected = selected is not None and selected == idx
        short_kind = KIND_SHORT_LABELS.get(d.kind, d.kind)
        kind_col = f"{short_kind:<8}"
        src_tgt = f"{d.source} {arrow(fancy)} {d.target}"
        elapsed = max(0.0, now - d.created_at) if d.created_at else 0.0
        wait_text = f"{format_duration(elapsed):>6}"
        # Two lines per decision: what + how long (line 1), why (line 2, indented), so
        # neither the reason nor the waiting time gets cut at narrow widths.
        indent = " " * (len(f"  [{idx}] ") + len(kind_col) + 2 + len(wait_text) + 2)

        if is_selected:
            line_str = f"  [{idx}] {kind_col}  {wait_text}  {d.project}{sep(fancy)}{src_tgt}"
            lines.append(tui.Line(line_str, style="reverse magenta", no_wrap=True, overflow="ellipsis"))
        else:
            line = tui.Line(no_wrap=True, overflow="ellipsis")
            line.append("  ")
            line.append(f"[{idx}]", style="bold magenta")
            line.append(" ")
            kind_color = KIND_COLORS.get(d.kind, KIND_COLORS.get(short_kind, "magenta"))
            line.append(kind_col, style=kind_color)
            line.append("  ")
            line.append(wait_text, style="dim")
            line.append("  ")
            line.append(d.project, style="bold")
            line.append(sep(fancy), style="dim")
            line.append(d.source, style="cyan")
            line.append(f" {arrow(fancy)} ", style="dim")
            line.append(d.target, style="cyan")
            lines.append(line)
        if summary:
            lines.append(tui.Line(f"{indent}{summary}", style="dim", no_wrap=True, overflow="ellipsis"))

    selected_decision: flowmodel.Decision | None = None
    if selected is not None and 1 <= selected <= len(decisions):
        selected_decision = decisions[selected - 1]
    elif selection_state is not None and getattr(selection_state, "selected", None) is not None:
        sel_idx = getattr(selection_state, "selected")
        if 1 <= sel_idx <= len(decisions):
            selected_decision = decisions[sel_idx - 1]

    if selection_state is None and bool(decisions):
        selection_state = keys.SelectionState(
            selected=selected,
            selected_id=selected_decision.id if selected_decision else None,
            selected_sha=getattr(selected_decision, "head_sha", None) if selected_decision else None,
            has_pending=True,
        )

    term_caps = caps if caps is not None else TermCaps(fancy=fancy, hyperlinks=False)
    footer = render_footer(selection_state, selected_decision, term_caps, collapse_hint=collapse_hint)
    if footer is not None:
        lines.append(footer)

    return lines


def _render_warnings(
    warnings: Sequence[str],
    fancy: bool,
) -> list[Any]:
    lines: list[Any] = []
    warn_ic = get_icon("warning", fancy)
    for warn_msg in warnings:
        clean = warn_msg
        for prefix in ("▲ ", "▲", "[!] ", "[!]", "⚠ ", "⚠"):
            if clean.startswith(prefix):
                clean = clean[len(prefix):]
                break
        line = tui.Line(no_wrap=True, overflow="ellipsis")
        line.append(warn_ic, style="yellow")
        line.append(f" {clean}")
        lines.append(line)
    return lines


def _render_environments(
    environments: Sequence[Mapping[str, Any]],
    fancy: bool,
) -> list[Any]:
    lines: list[Any] = [tui.Line("ENVIRONMENTS", style="bold cyan", no_wrap=True)]

    proj_map: dict[str, list[Mapping[str, Any]]] = {}
    for env in environments:
        if not isinstance(env, Mapping):
            continue
        p = str(env.get("project") or "")
        if p not in proj_map:
            proj_map[p] = []
        proj_map[p].append(env)

    max_p_len = max((len(p) for p in proj_map.keys()), default=0)
    pad_w = max(14, max_p_len + 3)

    for proj, env_list in proj_map.items():
        line = tui.Line(no_wrap=True, overflow="ellipsis")
        line.append("  ")
        line.append(f"{proj:<{pad_w}}", style="bold")
        for idx, e in enumerate(env_list):
            if idx > 0:
                line.append(f" {arrow(fancy)} ", style="dim")

            e_name = str(e.get("env") or "")
            st = str(e.get("status") or "none")
            verified = bool(e.get("verified", False))
            sha = str(e.get("sha"))[:7] if e.get("sha") else ""

            line.append(e_name)
            line.append(" ")

            if verified or st == "verified":
                ic = get_icon("passed", fancy)
                line.append(ic, style="green")
                if sha:
                    line.append(" ")
                    line.append(sha, style="dim")
                else:
                    line.append(" verified", style="green")
            elif st == "waiting_approval":
                ic = get_icon("waiting_approval", fancy)
                line.append(f"{ic} waiting approval", style="magenta")
            elif st in ("running", "pending", "verifying"):
                ic = get_icon("running", fancy)
                line.append(ic, style="yellow")
                if sha:
                    line.append(" ")
                    line.append(sha, style="dim")
                else:
                    line.append(f" {st}", style="yellow")
            elif st in ("failed", "stuck") or e.get("rollback"):
                ic = get_icon("failed", fancy)
                if e.get("rollback") or st == "rollback":
                    label = "rolling back"
                elif st == "stuck":
                    label = "stuck"
                else:
                    label = "failed"
                line.append(ic, style="bold red")
                line.append(f" {label}", style="red")
            else:
                ic = get_icon("pending", fancy)
                line.append(f"{ic} none", style="dim")

        lines.append(line)

    return lines


def _pipeline_sort_key(p: model.Pipeline, original_idx: int) -> tuple[int, int]:
    """Sort pipelines: failed (1), running (2), pending (3), passed (4), no-data (5)."""
    if p.fetch_error is not None or (p.pipeline_id is None and not p.jobs):
        rank = 5
    elif p.status == "failed":
        rank = 1
    elif p.status == "running":
        rank = 2
    elif p.status in ("pending", "skipped", "manual"):
        rank = 3
    elif p.status == "passed":
        rank = 4
    else:
        rank = 5
    return (rank, original_idx)


# ---------------------------------------------------------------------------
# Main render entrypoint
# ---------------------------------------------------------------------------


def render(
    pipelines: list[model.Pipeline],
    *,
    now: float,
    caps: TermCaps,
    updated_at: float | None = None,
    mock: bool = False,
    decisions: Sequence[flowmodel.Decision] = (),
    warnings: Sequence[str] = (),
    selected: int | None = None,
    subtitle_override: str | None = None,
    environments: Sequence[Mapping[str, Any]] = (),
    prs: Mapping[tuple[str, str], flowmodel.PrState] | None = None,
    selection_state: Any = None,
    collapse_passed: bool = True,
) -> tui.Panel:
    """Render the pipedog banner and stage-chain status panel as a pure function."""
    banner_renderable = tui.Line(BANNER, style="bold cyan", no_wrap=True, overflow="crop")

    body_parts: list[Any] = []

    # Check which pipelines qualify for collapse
    waiting_projects = {
        d.project for d in decisions if getattr(d, "project", None)
    }

    def _pr_needs_attention(p: model.Pipeline) -> bool:
        pr_obj: flowmodel.PrState | None = None
        if prs and p.branch:
            pr_obj = prs.get((p.project, p.branch))
        if pr_obj is None and hasattr(p, "pr_state"):
            pr_obj = getattr(p, "pr_state")
        if pr_obj is None or pr_obj.state != "open":
            return False
        if pr_obj.review != "approved":
            return True
        if pr_obj.unresolved_threads > 0:
            return True
        if pr_obj.mergeable is False:
            return True
        return False

    collapsible_pipelines = [
        p
        for p in pipelines
        if p.status == "passed"
        and p.fetch_error is None
        and p.project not in waiting_projects
        and not _pr_needs_attention(p)
    ]

    collapsing_applies = len(collapsible_pipelines) >= 4
    collapse_hint: str | None = None
    if collapsing_applies:
        collapse_hint = "p expand passed" if collapse_passed else "p collapse passed"

    # 1. WAITING FOR YOU section
    has_waiting = bool(decisions)
    if has_waiting:
        body_parts.extend(
            _render_waiting(
                decisions,
                selected,
                caps.fancy,
                now=now,
                selection_state=selection_state,
                caps=caps,
                collapse_hint=collapse_hint,
            )
        )

    # 2. Warnings
    has_warnings = bool(warnings)
    if has_warnings:
        body_parts.extend(_render_warnings(warnings, caps.fancy))

    if (has_waiting or has_warnings) and pipelines:
        body_parts.append(tui.Line(""))

    # 3. Pipeline blocks: ordered by failed, running, pending, passed, no-data
    sorted_pipelines = [
        p
        for _, p in sorted(
            enumerate(pipelines),
            key=lambda item: _pipeline_sort_key(item[1], item[0]),
        )
    ]

    collapsible_ids = {id(p) for p in collapsible_pipelines}
    rendered_collapse_summary = False

    summary_line: Any = None
    if collapsing_applies and collapse_passed:
        summary_line = tui.Line(no_wrap=True, overflow="ellipsis")
        summary_line.append(get_icon("passed", caps.fancy), style="green")
        summary_line.append(f" {len(collapsible_pipelines)} passed: ")
        summary_names = [p.project for p in collapsible_pipelines]
        summary_line.append(", ".join(summary_names), style="dim")

    blocks_to_render: list[tuple[str, Any]] = []
    for p in sorted_pipelines:
        if collapsing_applies and collapse_passed and id(p) in collapsible_ids:
            if not rendered_collapse_summary:
                blocks_to_render.append(("summary", summary_line))
                rendered_collapse_summary = True
        else:
            blocks_to_render.append(("pipeline", p))

    for idx, (kind, item) in enumerate(blocks_to_render):
        if kind == "summary":
            body_parts.append(item)
        else:
            p = item
            pr: flowmodel.PrState | None = None
            if prs and p.branch:
                pr = prs.get((p.project, p.branch))
            if pr is None and hasattr(p, "pr_state"):
                pr = getattr(p, "pr_state")

            # Header line
            body_parts.append(_pipeline_header(p, pr, now, caps.fancy))

            # Body: fetch error, no-data, or stages + details
            if p.fetch_error is not None:
                err_line = tui.Line(no_wrap=True, overflow="ellipsis")
                err_line.append("  ")
                err_line.append(get_icon("failed", caps.fancy), style="bold red")
                err_line.append(f" {p.fetch_error}", style="red")
                body_parts.append(err_line)
            elif not p.jobs and p.pipeline_id is None:
                body_parts.append(
                    tui.Line(
                        f"  {get_icon('pending', caps.fancy)} no pipeline yet",
                        style="dim",
                        no_wrap=True,
                        overflow="ellipsis",
                    )
                )
            else:
                if pr and pr.state == "open":
                    body_parts.append(_pr_line(p, pr, caps.fancy))

                distinct_workflows = [w for w in dict.fromkeys(j.workflow for j in p.jobs if j.workflow)]
                if len(distinct_workflows) > 1:
                    for wf in distinct_workflows:
                        wf_jobs = [j for j in p.jobs if j.workflow == wf]
                        stages = _stages_for_jobs(wf_jobs, p.provider)
                        is_all_passed = p.status == "passed" and all(
                            st.status == "passed" for st in stages
                        )
                        body_parts.append(
                            ChainView(
                                stages=stages,
                                fancy=caps.fancy,
                                now=now,
                                is_all_passed=is_all_passed,
                                prefix=wf,
                            )
                        )
                else:
                    stages = get_pipeline_stages(p)
                    is_all_passed = p.status == "passed" and all(
                        st.status == "passed" for st in stages
                    )
                    body_parts.append(
                        ChainView(
                            stages=stages,
                            fancy=caps.fancy,
                            now=now,
                            is_all_passed=is_all_passed,
                        )
                    )

        if idx < len(blocks_to_render) - 1:
            body_parts.append(tui.Line(""))

    # 4. ENVIRONMENTS section
    if environments:
        if body_parts:
            body_parts.append(tui.Line(""))
        body_parts.extend(_render_environments(environments, caps.fancy))

    # 5. Footer when no decisions
    if not has_waiting and collapse_hint is not None:
        term_caps = caps if caps is not None else TermCaps(fancy=caps.fancy, hyperlinks=False)
        footer = render_footer(selection_state, None, term_caps, collapse_hint=collapse_hint)
        if footer is not None:
            if body_parts:
                body_parts.append(tui.Line(""))
            body_parts.append(footer)

    if not body_parts:
        body_parts.append(tui.Line("no pipelines yet", style="dim"))

    title = "pipedog · MOCK" if mock else "pipedog"
    if subtitle_override is not None:
        if isinstance(subtitle_override, tui.Line):
            subtitle: tui.Line = subtitle_override
        else:
            subtitle_style = (
                "bold red" if "not valid" in subtitle_override else "yellow"
            )
            subtitle = tui.Line(subtitle_override, style=subtitle_style)
    else:
        ts = updated_at if updated_at is not None else now
        subtitle = _build_subtitle(pipelines, ts=ts, caps=caps)

    panel_box = tui.ROUNDED if caps.fancy else tui.SQUARE

    return tui.Panel(
        tui.Group(banner_renderable, tui.Line(""), *body_parts),
        title=title,
        subtitle=subtitle,
        box=panel_box,
    )
