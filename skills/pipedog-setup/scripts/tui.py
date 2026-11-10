"""Minimal stdlib-only terminal UI primitives for pipedog.

Provides lightweight primitives for Line, Row, Indent, Panel,
LiveView, and ANSI styling/width calculation without third-party dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
import io
import os
import re
import shutil
import signal
import sys
import threading
from typing import Any
import unicodedata

_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-9;?]*[a-zA-Z]|\][^\x07\x1b]*(?:\x07|\x1b\\))"
)

_STYLE_MAP: dict[str, str] = {
    "bold": "\x1b[1m",
    "dim": "\x1b[2m",
    "reverse": "\x1b[7m",
    "red": "\x1b[31m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "blue": "\x1b[34m",
    "magenta": "\x1b[35m",
    "cyan": "\x1b[36m",
    "white": "\x1b[37m",
}
_RESET = "\x1b[0m"


def strip_ansi(s: str) -> str:
    """Remove ANSI escape sequences from string."""
    return _ANSI_RE.sub("", s)


def char_width(c: str) -> int:
    """Return terminal column width of character (W/F = 2 cols, others = 1 col)."""
    return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


def str_width(s: str) -> int:
    """Return visible terminal column width of string, ignoring ANSI escape sequences."""
    clean = strip_ansi(s)
    return sum(char_width(c) for c in clean)


def crop_text(s: str, width: int) -> str:
    """Crop string to fit within width columns without adding ellipsis."""
    if width <= 0:
        return ""
    cur_w = 0
    res: list[str] = []
    for c in s:
        w = char_width(c)
        if cur_w + w > width:
            break
        res.append(c)
        cur_w += w
    return "".join(res)


def ellipsize_text(s: str, width: int) -> str:
    """Truncate string to fit within width columns, ending with '…' if truncated."""
    if str_width(s) <= width:
        return s
    if width <= 0:
        return ""
    if width == 1:
        return "…"
    target = width - 1  # 1 col for '…'
    cur_w = 0
    res: list[str] = []
    for c in s:
        w = char_width(c)
        if cur_w + w > target:
            break
        res.append(c)
        cur_w += w
    return "".join(res) + "…"


def format_styled(text: str, style: str = "", link: str = "") -> str:
    """Apply ANSI style sequences and OSC 8 hyperlink to text."""
    if not text:
        return ""
    codes: list[str] = []
    for part in style.split():
        if part in _STYLE_MAP:
            codes.append(_STYLE_MAP[part])
        elif part.startswith("link:"):
            link = part[5:]

    res = text
    if codes:
        res = f"{''.join(codes)}{res}{_RESET}"
    if link:
        res = f"\x1b]8;;{link}\x1b\\{res}\x1b]8;;\x1b\\"
    return res


def word_wrap(text: str, width: int) -> list[str]:
    """Wrap words on space boundaries to fit within width columns."""
    if not text or width <= 0:
        return [text]
    words = text.split(" ")
    lines: list[str] = []
    cur_line: list[str] = []
    cur_w = 0
    for w in words:
        ww = str_width(w)
        if not cur_line:
            cur_line.append(w)
            cur_w = ww
        elif cur_w + 1 + ww <= width:
            cur_line.append(w)
            cur_w += 1 + ww
        else:
            lines.append(" ".join(cur_line))
            cur_line = [w]
            cur_w = ww
    if cur_line:
        lines.append(" ".join(cur_line))
    return lines or [text]


@dataclass
class Span:
    """Styled text segment within a Line."""

    text: str
    style: str = ""
    link: str = ""


class Line:
    """Styled single-line or multi-line text with overflow control."""

    def __init__(
        self,
        text: str = "",
        style: str = "",
        overflow: str = "ellipsis",
        no_wrap: bool = True,
        link: str = "",
    ) -> None:
        self.overflow = overflow
        self.no_wrap = no_wrap
        self.spans: list[Span] = []
        if text:
            self.spans.append(Span(text=text, style=style, link=link))
        self._default_style = style

    def append(self, text: str, style: str = "", link: str = "") -> Line:
        self.spans.append(Span(text=text, style=style, link=link))
        return self

    def stylize(self, style: str, start: int = 0, end: int | None = None) -> None:
        """Apply styling (e.g. 'link <url>') to character range."""
        link = ""
        if style.startswith("link "):
            link = style[5:].strip()

        char_idx = 0
        end_idx = len(self.text) if end is None else end
        for span in self.spans:
            span_len = len(span.text)
            span_start = char_idx
            span_end = char_idx + span_len
            if span_start < end_idx and span_end > start:
                if link:
                    span.link = link
                elif style:
                    span.style = f"{span.style} {style}".strip()
            char_idx = span_end

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)

    @property
    def plain(self) -> str:
        return self.text

    @property
    def style(self) -> str:
        if self.spans:
            return self.spans[0].style or self._default_style
        return self._default_style

    def __len__(self) -> int:
        return len(self.text)

    def __str__(self) -> str:
        return self.text

    def __repr__(self) -> str:
        return f"Line({self.text!r}, style={self.style!r})"

    def render(self, width: int, color: bool = True) -> str:
        full_text = self.text
        if not full_text:
            return ""

        w = str_width(full_text)
        if w > width:
            if self.overflow == "crop":
                truncated = crop_text(full_text, width)
            else:
                truncated = ellipsize_text(full_text, width)

            if not color:
                return truncated

            # Re-map styling to truncated portion
            if len(self.spans) <= 1:
                style = self.spans[0].style if self.spans else self._default_style
                link = self.spans[0].link if self.spans else ""
                return format_styled(truncated, style=style, link=link)

            # Multiple spans: truncate character-by-character
            remaining_w = width if self.overflow == "crop" else (width - 1)
            parts: list[str] = []
            cur_w = 0
            for s in self.spans:
                sw = str_width(s.text)
                if cur_w + sw <= remaining_w:
                    parts.append(format_styled(s.text, style=s.style, link=s.link))
                    cur_w += sw
                else:
                    needed = max(0, remaining_w - cur_w)
                    piece = crop_text(s.text, needed)
                    cur_w += str_width(piece)
                    if piece:
                        parts.append(format_styled(piece, style=s.style, link=s.link))
                    break
            if self.overflow != "crop":
                parts.append("…")
            return "".join(parts)

        if not color:
            return full_text

        parts = [format_styled(s.text, style=s.style, link=s.link) for s in self.spans]
        return "".join(parts)


# Text alias for backwards compatibility
Text = Line


class Row:
    """Two-column row: left ellipsized, right right-aligned."""

    def __init__(self, left: Line | str, right: Line | str) -> None:
        self.left = Line(left) if isinstance(left, str) else left
        self.right = Line(right) if isinstance(right, str) else right

    def render(self, width: int, color: bool = True) -> str:
        rw = str_width(self.right.text)
        if rw >= width:
            return self.right.render(width, color=color)

        max_left_w = max(0, width - rw - 1)
        left_text = self.left.text
        if str_width(left_text) > max_left_w:
            left_str = ellipsize_text(left_text, max_left_w)
        else:
            left_str = left_text

        lw = str_width(left_str)
        spaces_count = max(1, width - lw - rw)
        spaces = " " * spaces_count

        if not color:
            return f"{left_str}{spaces}{self.right.text}"

        left_styled = self.left.render(max_left_w, color=True)
        right_styled = self.right.render(rw, color=True)
        return f"{left_styled}{spaces}{right_styled}"


class Indent:
    """Indent content by n spaces on the left, wrapping if needed."""

    def __init__(self, lines: Any, n: int = 2) -> None:
        self.lines = lines
        self.n = n

    def render_lines(self, width: int, color: bool = True) -> list[str]:
        inner_w = max(0, width - self.n)
        pad = " " * self.n

        if isinstance(self.lines, Line):
            if not self.lines.no_wrap:
                words_lines = word_wrap(self.lines.text, inner_w)
                out: list[str] = []
                for wl in words_lines:
                    line_str = f"{pad}{wl}"
                    if color and self.lines.style:
                        line_str = format_styled(line_str, style=self.lines.style)
                    out.append(line_str)
                return out
            rendered = self.lines.render(inner_w, color=color)
            return [f"{pad}{rendered}"]

        sub_lines = to_lines(self.lines, width=inner_w, color=color)
        return [f"{pad}{sl}" for sl in sub_lines]


class Group:
    """Group of renderables."""

    def __init__(self, *renderables: Any) -> None:
        self.renderables: list[Any] = list(renderables)

    def __iter__(self):
        return iter(self.renderables)


@dataclass(frozen=True)
class Box:
    top_left: str
    top_right: str
    bottom_left: str
    bottom_right: str
    horizontal: str
    vertical: str


ROUNDED = Box(
    top_left="╭",
    top_right="╮",
    bottom_left="╰",
    bottom_right="╯",
    horizontal="─",
    vertical="│",
)

SQUARE = Box(
    top_left="┌",
    top_right="┐",
    bottom_left="└",
    bottom_right="┘",
    horizontal="─",
    vertical="│",
)


class _BoxModule:
    ROUNDED = ROUNDED
    SQUARE = SQUARE


box = _BoxModule()


class Panel:
    """Bordered box enclosing content, with optional title and subtitle."""

    def __init__(
        self,
        renderable: Any = None,
        *,
        title: str | Line | None = None,
        subtitle: str | Line | None = None,
        box: Box = ROUNDED,
        title_align: str = "center",
        subtitle_align: str = "center",
        padding: tuple[int, int] = (0, 1),
    ) -> None:
        self.renderable = renderable
        self.lines = renderable
        self.title = title
        self.subtitle = subtitle
        self.box = box
        self.title_align = title_align
        self.subtitle_align = subtitle_align
        self.padding = padding

    def render(self, width: int, color: bool = True) -> list[str]:
        b = self.box
        inner_w = max(0, width - 4)

        # Top border
        avail = max(0, width - 2)
        if self.title:
            t_text = self.title.text if isinstance(self.title, Line) else str(self.title)
            t_style = self.title.style if isinstance(self.title, Line) else ""
            tw = str_width(t_text)
            if tw + 2 <= avail:
                dashes = avail - (tw + 2)
                left_d = dashes // 2
                right_d = dashes - left_d
                t_str = format_styled(t_text, t_style) if (color and t_style) else t_text
                top = f"{b.top_left}{b.horizontal * left_d} {t_str} {b.horizontal * right_d}{b.top_right}"
            else:
                top = f"{b.top_left}{b.horizontal * avail}{b.top_right}"
        else:
            top = f"{b.top_left}{b.horizontal * avail}{b.top_right}"

        # Bottom border
        if self.subtitle:
            s_text = self.subtitle.text if isinstance(self.subtitle, Line) else str(self.subtitle)
            s_style = self.subtitle.style if isinstance(self.subtitle, Line) else ""
            s_spans = self.subtitle.spans if isinstance(self.subtitle, Line) else []
            sw = str_width(s_text)
            if sw + 2 <= avail:
                dashes = avail - (sw + 2)
                left_d = dashes // 2
                right_d = dashes - left_d
                if color:
                    if s_spans:
                        s_str = "".join(
                            format_styled(sp.text, style=sp.style, link=sp.link)
                            for sp in s_spans
                        )
                    elif s_style:
                        s_str = format_styled(s_text, s_style)
                    else:
                        s_str = s_text
                else:
                    s_str = s_text
                bottom = f"{b.bottom_left}{b.horizontal * left_d} {s_str} {b.horizontal * right_d}{b.bottom_right}"
            else:
                bottom = f"{b.bottom_left}{b.horizontal * avail}{b.bottom_right}"
        else:
            bottom = f"{b.bottom_left}{b.horizontal * avail}{b.bottom_right}"

        # Middle lines
        body_lines = to_lines(self.renderable, width=inner_w, color=color)
        middle: list[str] = []
        for line in body_lines:
            vw = str_width(line)
            pad = " " * max(0, inner_w - vw)
            middle.append(f"{b.vertical} {line}{pad} {b.vertical}")

        return [top, *middle, bottom]


def to_lines(view: Any, width: int = 80, color: bool | None = None) -> list[str]:
    """Render view to a list of terminal output lines."""
    if os.environ.get("NO_COLOR"):
        use_color = False
    elif color is not None:
        use_color = bool(color)
    else:
        use_color = True

    if view is None:
        return []

    if isinstance(view, Panel):
        return view.render(width, color=use_color)

    if isinstance(view, Group):
        res: list[str] = []
        for item in view.renderables:
            res.extend(to_lines(item, width=width, color=use_color))
        return res

    if isinstance(view, (list, tuple)):
        res = []
        for item in view:
            res.extend(to_lines(item, width=width, color=use_color))
        return res

    if isinstance(view, Row):
        return [view.render(width, color=use_color)]

    if isinstance(view, Indent):
        return view.render_lines(width, color=use_color)

    if hasattr(view, "render_lines"):
        return view.render_lines(width, color=use_color)

    if hasattr(view, "render") and not isinstance(view, (Panel, Line, Row)):
        res = view.render(width, color=use_color)
        if isinstance(res, list):
            return res
        return [str(res)]

    if isinstance(view, Line):
        if "\n" in view.text:
            out: list[str] = []
            for sub in view.text.split("\n"):
                sub_line = Line(
                    sub,
                    style=view.style,
                    overflow=view.overflow,
                    no_wrap=view.no_wrap,
                )
                out.append(sub_line.render(width, color=use_color))
            return out
        return [view.render(width, color=use_color)]

    if isinstance(view, str):
        if "\n" in view:
            return [Line(s).render(width, color=use_color) for s in view.split("\n")]
        return [Line(view).render(width, color=use_color)]

    return [str(view)]


def print_view(
    view: Any,
    stream: Any = None,
    width: int | None = None,
    color: bool | None = None,
) -> None:
    """Print rendered view to stream (default sys.stdout)."""
    target = stream if stream is not None else sys.stdout
    if hasattr(target, "file"):
        target = target.file
    if width is None:
        width = shutil.get_terminal_size((80, 24)).columns
    if color is None:
        is_tty = getattr(target, "isatty", lambda: False)()
        color = is_tty and ("NO_COLOR" not in os.environ)
    lines = to_lines(view, width=width, color=color)
    target.write("\n".join(lines) + "\n")
    target.flush()


class LiveView:
    """Context manager for in-place terminal rewriting without flicker."""

    def __init__(
        self,
        *args: Any,
        stream: Any = None,
        tty: bool | None = None,
        auto_refresh: bool = False,
        **kwargs: Any,
    ) -> None:
        target_stream = stream
        initial_view = None
        if args:
            first = args[0]
            if hasattr(first, "write"):
                target_stream = first
            else:
                initial_view = first
                if len(args) > 1 and hasattr(args[1], "write"):
                    target_stream = args[1]

        if target_stream is None:
            target_stream = kwargs.get("console") or sys.stdout

        if hasattr(target_stream, "file"):
            target_stream = target_stream.file

        self.initial_view = initial_view or kwargs.get("initial_view")
        self.stream = target_stream
        self.tty = (
            tty
            if tty is not None
            else getattr(self.stream, "isatty", lambda: False)()
        )
        self._prev_line_count = 0
        self._printed_once = False
        self._winch_flag = False
        self._old_winch_handler: Any = None

    def _handle_winch(self, signum: Any = None, frame: Any = None) -> None:
        self._winch_flag = True
        if callable(self._old_winch_handler) and self._old_winch_handler not in (
            getattr(signal, "SIG_IGN", None),
            getattr(signal, "SIG_DFL", None),
        ):
            try:
                self._old_winch_handler(signum, frame)
            except Exception:
                pass

    _on_winch = _handle_winch

    def __enter__(self) -> LiveView:
        if self.tty:
            self.stream.write("\x1b[?1049h\x1b[?25l")
            self.stream.flush()
            if hasattr(signal, "SIGWINCH"):
                try:
                    if threading.current_thread() is threading.main_thread():
                        self._old_winch_handler = signal.signal(
                            signal.SIGWINCH, self._handle_winch
                        )
                except (ValueError, TypeError, OSError):
                    pass
        if self.initial_view is not None:
            self.update(self.initial_view)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self.tty:
            try:
                self.stream.write("\x1b[?25h\x1b[?1049l")
                self.stream.flush()
            except Exception:
                pass
            if self._old_winch_handler is not None and hasattr(signal, "SIGWINCH"):
                try:
                    if threading.current_thread() is threading.main_thread():
                        signal.signal(signal.SIGWINCH, self._old_winch_handler)
                except (ValueError, TypeError, OSError):
                    pass
                self._old_winch_handler = None

    def update(self, view: Any, refresh: bool = True) -> None:
        size = shutil.get_terminal_size((80, 24))
        width = size.columns
        height = size.lines
        color = self.tty and ("NO_COLOR" not in os.environ)
        lines = to_lines(view, width=width, color=color)

        if not self.tty:
            if not self._printed_once:
                self.stream.write("\n".join(lines) + "\n")
                self.stream.flush()
                self._printed_once = True
            return

        # Clip to terminal height if content is taller than screen
        if height > 0 and len(lines) > height:
            keep_count = max(0, height - 1)
            hidden_count = len(lines) - keep_count
            clipped_lines = lines[:keep_count]
            more_msg = f"… {hidden_count} more lines (resize or press p)"
            if color:
                more_line = format_styled(more_msg, style="dim")
            else:
                more_line = more_msg
            clipped_lines.append(more_line)
            lines = clipped_lines

        out_parts: list[str] = ["\x1b[H"]
        for line in lines:
            out_parts.append(f"{line}\n")
        out_parts.append("\x1b[J")

        self.stream.write("".join(out_parts))
        self.stream.flush()
        self._prev_line_count = len(lines)
        self._winch_flag = False


# Live alias for backwards compatibility
Live = LiveView
