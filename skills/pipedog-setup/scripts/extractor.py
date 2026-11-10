"""Log cleaning and root-cause extraction for failed CI jobs.

Public API (see docs/DESIGN.md section 5):

    clean_log(raw)                       -> list[str]
    extract(raw, strip_prefixes=())      -> model.ErrorInfo

Only the last ``_WINDOW`` clean lines are inspected; the error summary is chosen
by priority tier (pytest > python traceback > compiler diagnostics > keywords >
last meaningful line) and, within a tier, the *last* occurrence wins.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Optional, Tuple

import model

_WINDOW = 80          # number of trailing clean lines inspected for the root cause
_TAIL_LINES = 30      # number of trailing clean lines kept as ``log_tail``
_SUMMARY_MAX = 200    # hard cap for ``error_summary``

# --------------------------------------------------------------------------- #
# clean_log
# --------------------------------------------------------------------------- #

_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ESC_OTHER = re.compile(r"\x1b[@-Z\\-_]")
# GitLab collapsible-section markers, e.g. "section_start:1700000000:step_script\r".
_SECTION = re.compile(r"section_(?:start|end):\d+:[^\s\x1b]*\r?")
# GitHub Actions per-line timestamp, e.g. "2026-11-08T07:15:00.1234567Z ".
_GL_STREAM = re.compile(r"^[0-9a-f]{2}[OE]\+? ?")
_GH_TS = re.compile(r"^\ufeff?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?")


def clean_log(raw: str) -> list[str]:
    """Return the log as a list of plain-text lines.

    Removes ANSI escapes (CSI / OSC), resolves ``\\r`` overwrites (progress bars),
    and strips GitHub timestamps and GitLab section markers. Lines that consisted
    only of a section marker are dropped; trailing blank lines are trimmed.
    """
    text = _OSC.sub("", raw)
    text = _CSI.sub("", text)
    text = _ESC_OTHER.sub("", text)
    text = text.replace("\r\n", "\n")

    out: list[str] = []
    for line in text.split("\n"):
        had_marker = _SECTION.search(line) is not None
        if had_marker:
            line = _SECTION.sub("", line)
        if "\r" in line:
            # A bare "\r" returns the cursor to column 0: the last visible segment wins.
            parts = [p for p in line.split("\r") if p.strip()]
            line = parts[-1] if parts else ""
        stripped = _GH_TS.sub("", line)
        if stripped != line:
            # GitLab timestamped logs: "<ts> 00O <text>" (stream index + O/E, "+" = continuation).
            stripped = _GL_STREAM.sub("", stripped)
        line = stripped.rstrip()
        if had_marker and not line:
            continue
        out.append(line)

    while out and not out[-1]:
        out.pop()
    return out


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

# Paths that are never part of the repository under test.
_EXTERNAL_MARKERS = (
    "site-packages",
    "dist-packages",
    "/usr/lib/",
    "/usr/local/lib/",
    "/opt/hostedtoolcache/",
    "/opt/homebrew/",
    "/Library/Frameworks/",
    "node_modules/",
    "<frozen",
    "<string>",
    "<stdin>",
)

# Runner chatter that says nothing about the root cause.
_RUNNER_NOISE = re.compile(
    r"^(?:"
    r"Cleaning up\b"
    r"|ERROR: Job failed\b"
    r"|##\[error\]Process completed with exit code \d+"
    r"|##\[(?:warning|debug|notice|group|endgroup)\]"
    r"|Post job cleanup\b"
    r"|Uploading artifacts\b"
    r"|Not uploading\b"
    r"|Saving cache\b"
    r"|Running after_script\b"
    r"|Running on .* via "
    r"|WARNING: (?:Upload|Failed to) "
    r")"
)
# Lines matching a keyword but carrying no information (skipped in the keyword tier).
_LOW_VALUE = re.compile(r"A complete log of this run|For a full report|This is probably not a problem with npm")
_GH_DIRECTIVE = re.compile(r"^##\[[a-zA-Z0-9_-]+\]\s*")


def _is_noise(line: str) -> bool:
    return bool(_RUNNER_NOISE.match(line.strip()))


def _is_external(path: str) -> bool:
    return any(marker in path for marker in _EXTERNAL_MARKERS)


def _normalize_path(path: str, prefixes: Sequence[str]) -> str:
    """Strip the first matching CI build-dir prefix and any leading ``./``."""
    p = path.strip()
    for prefix in prefixes:
        if prefix and p.startswith(prefix):
            p = p[len(prefix):].lstrip("/")
            break
    while p.startswith("./"):
        p = p[2:]
    return p


def _one_line(text: str) -> str:
    """Collapse whitespace, drop GitHub's ``##[...]`` directives and cap the length."""
    text = text.strip()
    m = _GH_DIRECTIVE.match(text)
    if m:
        rest = text[m.end():].strip()
        text = rest if rest else text[m.end():]
    text = " ".join(text.split())
    if len(text) > _SUMMARY_MAX:
        text = text[: _SUMMARY_MAX - 3] + "..."
    return text


# A tier hit: (index in window, summary, file or None, line or None)
_Hit = Tuple[int, str, Optional[str], Optional[int]]

# --------------------------------------------------------------------------- #
# Tier 1: pytest
# --------------------------------------------------------------------------- #

_PYTEST_E = re.compile(r"^E(?: {2,}(.*))?$")
_PYTEST_LOC = re.compile(r"^(?P<file>[^\s:'\"]+\.py):(?P<line>\d+): ?(?P<rest>.*)$")
_PYTEST_LINE_STYLE = re.compile(r"^[A-Z][\w.]*(?:Error|Exception|Failed|Failure|Exit)\b")


def _tier_pytest(win: list[str], prefixes: Sequence[str]) -> _Hit | None:
    n = len(win)
    best: _Hit | None = None
    consumed: set[int] = set()   # location lines already attributed to an "E" block
    prev_end = 0

    i = 0
    while i < n:
        if not _PYTEST_E.match(win[i]):
            i += 1
            continue
        start = i
        while i < n and _PYTEST_E.match(win[i]):
            i += 1
        end = i  # exclusive

        summary = ""
        for k in range(start, end):
            m = _PYTEST_E.match(win[k])
            if m and m.group(1):
                summary = m.group(1).strip()
                break
        if not summary:
            prev_end = end
            continue

        # Location: nearest repo frame; lines right after the block first (long format),
        # then lines before it (short format).
        candidates = [k for k in range(end, min(end + 3, n))]
        candidates += list(range(start - 1, prev_end - 1, -1))
        file_: str | None = None
        line_: int | None = None
        for k in candidates:
            lm = _PYTEST_LOC.match(win[k])
            if not lm or _is_external(lm.group("file")):
                continue
            file_ = _normalize_path(lm.group("file"), prefixes)
            line_ = int(lm.group("line"))
            consumed.add(k)
            break
        # After-block location lines that we did not pick must not be mistaken for
        # "--tb=line" style hits below.
        for k in range(end, min(end + 3, n)):
            if _PYTEST_LOC.match(win[k]):
                consumed.add(k)

        best = (start, summary, file_, line_)
        prev_end = end

    # "--tb=line" style: "path/test_x.py:42: AssertionError: msg"
    for k in range(n):
        if k in consumed:
            continue
        lm = _PYTEST_LOC.match(win[k])
        if not lm:
            continue
        rest = lm.group("rest").strip()
        if not rest or not _PYTEST_LINE_STYLE.match(rest):
            continue
        if best is not None and best[0] > k:
            continue
        ext = _is_external(lm.group("file"))
        file_ = None if ext else _normalize_path(lm.group("file"), prefixes)
        line_ = None if ext else int(lm.group("line"))
        best = (k, rest, file_, line_)

    return best


# --------------------------------------------------------------------------- #
# Tier 2: Python traceback
# --------------------------------------------------------------------------- #

_PY_FRAME = re.compile(r'^\s*File "(?P<file>.+)", line (?P<line>\d+)')
_PY_EXC = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Failure|Failed)(?::.*)?$")
_PY_EXC_GENERIC = re.compile(r"^[A-Za-z_][\w.]*(?::.*)?$")
_PY_HEADER = re.compile(r"^Traceback \(most recent call last\):")


def _tier_traceback(win: list[str], prefixes: Sequence[str]) -> _Hit | None:
    n = len(win)
    best: _Hit | None = None
    i = 0
    while i < n:
        if not _PY_FRAME.match(win[i]):
            i += 1
            continue
        has_header = i > 0 and bool(_PY_HEADER.match(win[i - 1]))
        frames: list[tuple[str, int]] = []
        j = i
        while j < n and (_PY_FRAME.match(win[j]) or (win[j][:1].isspace())):
            fm = _PY_FRAME.match(win[j])
            if fm:
                frames.append((fm.group("file"), int(fm.group("line"))))
            j += 1

        if j < n:
            exc_line = win[j].strip()
            ok = bool(_PY_EXC.match(exc_line)) or (
                has_header and bool(_PY_EXC_GENERIC.match(exc_line)) and not _is_noise(exc_line)
            )
            if ok:
                file_: str | None = None
                line_: int | None = None
                for f, ln in reversed(frames):
                    if not _is_external(f):
                        file_ = _normalize_path(f, prefixes)
                        line_ = ln
                        break
                best = (j, exc_line, file_, line_)
        i = max(j, i + 1)
    return best


# --------------------------------------------------------------------------- #
# Tier 3: compiler / linter diagnostics
# --------------------------------------------------------------------------- #

# gcc / clang / mypy:  path:line[:col]: error: message
_DIAG_COLON = re.compile(
    r"^(?P<file>(?:[A-Za-z]:)?[^\s:()]+?\.\w+):(?P<line>\d+)(?::\d+)?:\s*"
    r"(?P<msg>(?:fatal )?error\b.*)$"
)
# tsc / msvc:  path(line,col): error TS1234: message
_DIAG_PAREN = re.compile(
    r"^(?P<file>[^\s()]+)\((?P<line>\d+)(?:,\d+)?\):\s*(?P<msg>error\b.*)$"
)
# go:  ./main.go:12:5: message
_DIAG_GO = re.compile(r"^(?P<file>\S+\.go):(?P<line>\d+):\d+:\s*(?P<msg>\S.*)$")


def _tier_diagnostic(win: list[str], prefixes: Sequence[str]) -> _Hit | None:
    best: _Hit | None = None
    for idx, raw in enumerate(win):
        line = raw.strip()
        for rx in (_DIAG_COLON, _DIAG_PAREN, _DIAG_GO):
            m = rx.match(line)
            if m:
                best = (
                    idx,
                    m.group("msg").strip(),
                    _normalize_path(m.group("file"), prefixes),
                    int(m.group("line")),
                )
                break
    return best


# --------------------------------------------------------------------------- #
# Tier 4: keyword lines
# --------------------------------------------------------------------------- #

_KEYWORDS = re.compile(
    r"Error:|ERROR|Exception|\bFAIL(?:ED)?\b|CrashLoopBackOff|ImagePullBackOff|OOMKilled"
    r"|exit code \d+|npm ERR!|##\[error\]|\bFailed to \w"
)
_PYTEST_FAILED = re.compile(r"^FAILED (?P<file>[^\s:]+\.py)::")


def _tier_keyword(win: list[str], prefixes: Sequence[str]) -> _Hit | None:
    best: _Hit | None = None
    for idx, raw in enumerate(win):
        line = raw.strip()
        if not line or _is_noise(line) or _LOW_VALUE.search(line):
            continue
        if not _KEYWORDS.search(line):
            continue
        file_: str | None = None
        fm = _PYTEST_FAILED.match(line)
        if fm:
            file_ = _normalize_path(fm.group("file"), prefixes)
        best = (idx, line, file_, None)
    return best


# --------------------------------------------------------------------------- #
# Tier 5: fallback
# --------------------------------------------------------------------------- #


def _tier_fallback(win: list[str]) -> str:
    non_empty = [ln.strip() for ln in win if ln.strip()]
    if not non_empty:
        return "no log output"
    for line in reversed(non_empty):
        if not _is_noise(line):
            return line
    return non_empty[-1]  # only runner chatter left


# --------------------------------------------------------------------------- #
# extract
# --------------------------------------------------------------------------- #


def extract(raw: str, strip_prefixes: Sequence[str] = ()) -> model.ErrorInfo:
    """Extract the root cause from a raw job log."""
    lines = clean_log(raw)
    win = lines[-_WINDOW:]
    log_tail = "\n".join(lines[-_TAIL_LINES:])
    prefixes = list(strip_prefixes)

    for tier in (_tier_pytest, _tier_traceback, _tier_diagnostic, _tier_keyword):
        hit = tier(win, prefixes)
        if hit is not None:
            _, summary, file_, line_ = hit
            return model.ErrorInfo(
                error_summary=_one_line(summary), file=file_, line=line_, log_tail=log_tail
            )

    return model.ErrorInfo(error_summary=_one_line(_tier_fallback(win)), log_tail=log_tail)
