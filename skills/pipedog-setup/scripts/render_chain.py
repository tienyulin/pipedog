"""Renderable stage chain with wrapping and aligned detail lines."""

from __future__ import annotations

import model
from render_icons import branch, connector, get_icon, sep
from render_stages import Stage
import tui


def format_duration(seconds: float | None) -> str:
    """Format elapsed seconds as `45s`, `1m20s`, or `1h02m`."""
    if seconds is None:
        total_secs = 0
    else:
        total_secs = max(0, int(seconds))

    if total_secs < 60:
        return f"{total_secs}s"
    if total_secs < 3600:
        mins, secs = divmod(total_secs, 60)
        return f"{mins}m{secs:02d}s"
    hours, rem = divmod(total_secs, 3600)
    mins = rem // 60
    return f"{hours}h{mins:02d}m"


def _format_error_detail(error: model.ErrorInfo | None) -> str:
    """Format a failed leaf's ErrorInfo as `<file>:<line> <summary>` or fallback."""
    if error is None:
        return "(fetching log…)"

    loc = ""
    if error.file:
        loc = f"{error.file}:{error.line}" if error.line is not None else error.file

    summary = error.error_summary.strip()
    if loc and summary:
        base = f"{loc} {summary}"
    elif loc:
        base = loc
    elif summary:
        base = summary
    else:
        base = "(fetching log…)"

    if getattr(error, "flaky", False):
        base += " · flaky?"
    elif getattr(error, "seen", 0) >= 1:
        base += f" · seen {error.seen}× in 7d"

    return base


class ChainView:
    """Renderable stage chain with wrapping and aligned detail lines."""

    def __init__(
        self,
        stages: list[Stage],
        fancy: bool,
        now: float,
        is_all_passed: bool = False,
        prefix: str | None = None,
    ) -> None:
        self.stages = stages
        self.fancy = fancy
        self.now = now
        self.is_all_passed = is_all_passed
        self.prefix = prefix

    def render_lines(self, width: int, color: bool = True) -> list[str]:
        if not self.stages:
            return []

        conn = connector(self.fancy)
        conn_w = tui.str_width(conn)
        indent = "  "
        indent_w = 2

        prefix_str = f"{self.prefix}: " if self.prefix else ""
        prefix_w = tui.str_width(prefix_str)

        # 1. Format stage nodes
        stage_nodes: list[tuple[Stage, str]] = []
        for st in self.stages:
            ic = get_icon(st.status, self.fancy)
            node_label = f"{ic} {st.name}"
            avail_w = max(4, width - indent_w - prefix_w)
            if indent_w + prefix_w + tui.str_width(node_label) > width:
                node_label = tui.ellipsize_text(node_label, avail_w)
            stage_nodes.append((st, node_label))

        # 2. Wrap into lines
        lines: list[list[tuple[Stage, str, int]]] = []
        cur_line: list[tuple[Stage, str, int]] = []
        cur_col = indent_w + prefix_w

        for st, label in stage_nodes:
            node_w = tui.str_width(label)
            if not cur_line:
                cur_line.append((st, label, cur_col))
                cur_col += node_w
            else:
                if cur_col + conn_w + node_w <= width:
                    cur_col += conn_w
                    cur_line.append((st, label, cur_col))
                    cur_col += node_w
                else:
                    lines.append(cur_line)
                    cur_col = indent_w
                    cur_line = [(st, label, cur_col)]
                    cur_col += node_w

        if cur_line:
            lines.append(cur_line)

        # 3. Render stage chain lines
        stage_cols: dict[int, int] = {}
        rendered_lines: list[str] = []

        for line_idx, line in enumerate(lines):
            line_parts: list[tuple[str, str]] = [(indent, "")]
            if line_idx == 0 and self.prefix:
                line_parts.append((prefix_str, "dim"))
            for idx, (st, label, col) in enumerate(line):
                stage_cols[id(st)] = col
                ic = get_icon(st.status, self.fancy)
                if label.startswith(ic):
                    name_part = label[len(ic):]
                else:
                    name_part = label
                    ic = ""

                if st.status == "failed":
                    ic_style = "bold red"
                    name_style = "red"
                elif st.status == "running":
                    ic_style = "bold yellow"
                    name_style = "bold yellow"
                elif st.status == "passed":
                    ic_style = "green"
                    name_style = "green"
                elif st.status == "allowed":
                    ic_style = "yellow"
                    name_style = "yellow"
                else:
                    ic_style = "dim"
                    name_style = "dim"

                if ic:
                    line_parts.append((ic, ic_style))
                line_parts.append((name_part, name_style))

                if idx < len(line) - 1:
                    line_parts.append((conn, "dim"))
            if not color:
                rendered_lines.append("".join(p[0] for p in line_parts))
            else:
                rendered_lines.append(
                    "".join(tui.format_styled(p[0], style=p[1]) for p in line_parts)
                )

        # 4. Detail lines under chain (only if not all passed)
        if self.is_all_passed:
            return rendered_lines

        detail_items: list[tuple[Stage, list[tuple[str, str]]]] = []
        for st in self.stages:
            for job in st.jobs:
                if job.sub_jobs:
                    # Job group
                    for sub in job.sub_jobs:
                        if sub.status == "failed" and not sub.allow_failure:
                            err_str = _format_error_detail(sub.error)
                            ic = get_icon("failed", self.fancy)
                            spans = [
                                (ic, "bold red"),
                                (f" {sub.name}", "red"),
                                (sep(self.fancy), "dim"),
                                (err_str, "red"),
                            ]
                            detail_items.append((st, spans))
                        elif sub.status == "failed" and sub.allow_failure:
                            ic = get_icon("allowed", self.fancy)
                            spans = [(f"{ic} {sub.name} failed (allowed)", "yellow")]
                            detail_items.append((st, spans))
                    if job.status == "running" or any(s.status == "running" for s in job.sub_jobs):
                        elapsed = (
                            max(0.0, self.now - job.started_at)
                            if job.started_at is not None
                            else 0.0
                        )
                        dur_str = format_duration(elapsed)
                        counts = f"({job.passed_count()}/{job.total_count()})"
                        ic = get_icon("running", self.fancy)
                        spans = [
                            (f"{ic} {job.name} {counts}", "yellow"),
                            (" ", ""),
                            (dur_str, ""),
                        ]
                        detail_items.append((st, spans))
                else:
                    # Leaf job
                    if job.status == "failed" and not job.allow_failure:
                        err_str = _format_error_detail(job.error)
                        ic = get_icon("failed", self.fancy)
                        spans = [
                            (ic, "bold red"),
                            (f" {job.name}", "red"),
                            (sep(self.fancy), "dim"),
                            (err_str, "red"),
                        ]
                        detail_items.append((st, spans))
                    elif job.status == "failed" and job.allow_failure:
                        ic = get_icon("allowed", self.fancy)
                        spans = [(f"{ic} {job.name} failed (allowed)", "yellow")]
                        detail_items.append((st, spans))
                    elif job.status == "running":
                        elapsed = (
                            max(0.0, self.now - job.started_at)
                            if job.started_at is not None
                            else 0.0
                        )
                        dur_str = format_duration(elapsed)
                        ic = get_icon("running", self.fancy)
                        spans = [
                            (f"{ic} {job.name}", "yellow"),
                            (" ", ""),
                            (dur_str, ""),
                        ]
                        detail_items.append((st, spans))
                    elif job.status in ("cancelled", "manual"):
                        status_lbl = job.raw_status or job.status
                        ic = get_icon("pending", self.fancy)
                        spans = [(f"{ic} {job.name} ({status_lbl})", "dim")]
                        detail_items.append((st, spans))

        if not detail_items:
            return rendered_lines

        br = branch(self.fancy)
        br_w = tui.str_width(br)
        shown_items = detail_items[:3]
        extra_count = len(detail_items) - 3

        for st, spans in shown_items:
            plain_text = "".join(p[0] for p in spans)
            col_offset = stage_cols.get(id(st), 4)
            if col_offset + br_w + tui.str_width(plain_text) <= width:
                pad = " " * col_offset
            else:
                pad = "    "
            line = tui.Line(no_wrap=True, overflow="ellipsis")
            line.append(pad)
            line.append(br, style="dim")
            for t_str, s_style in spans:
                line.append(t_str, style=s_style)
            rendered_lines.append(line.render(width, color=color))

        if extra_count > 0:
            pad = "    "
            line = tui.Line(no_wrap=True, overflow="ellipsis")
            line.append(pad)
            line.append(br, style="dim")
            line.append(f"+{extra_count} more", style="dim")
            rendered_lines.append(line.render(width, color=color))

        return rendered_lines
