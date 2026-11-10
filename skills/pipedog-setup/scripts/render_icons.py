"""Icon, color, and terminal capability helpers for pipedog renderer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os

_FANCY_TERM_PROGRAMS: frozenset[str] = frozenset(
    {"ghostty", "iTerm.app", "WezTerm", "vscode"}
)
_FANCY_TERM_KEYWORDS: tuple[str, ...] = (
    "ghostty",
    "kitty",
    "wezterm",
    "alacritty",
)
_DUMB_TERMS: frozenset[str] = frozenset({"dumb", "linux", "vt100"})

# Unified single-width icon set (no emoji anywhere in output)
UNIFIED_ICONS: dict[str, dict[str, str]] = {
    "passed": {"fancy": "✓", "ascii": "[ok]", "color": "green"},
    "failed": {"fancy": "✗", "ascii": "[x]", "color": "red"},
    "running": {"fancy": "●", "ascii": "[..]", "color": "yellow"},
    "pending": {"fancy": "○", "ascii": "[ ]", "color": "dim"},
    "skipped": {"fancy": "○", "ascii": "[ ]", "color": "dim"},
    "manual": {"fancy": "○", "ascii": "[ ]", "color": "dim"},
    "allowed": {"fancy": "▲", "ascii": "[!]", "color": "yellow"},
    "warning": {"fancy": "▲", "ascii": "[!]", "color": "yellow"},
    "needs_you": {"fancy": "◆", "ascii": "[?]", "color": "magenta"},
    "waiting_approval": {"fancy": "◆", "ascii": "[?]", "color": "magenta"},
}


def get_icon(name: str, fancy: bool) -> str:
    spec = UNIFIED_ICONS.get(name)
    if spec:
        return spec["fancy"] if fancy else spec["ascii"]
    return "○" if fancy else "[ ]"


def get_color(name: str) -> str:
    spec = UNIFIED_ICONS.get(name)
    return spec["color"] if spec else "dim"


def connector(fancy: bool) -> str:
    return " ── " if fancy else " -- "


def arrow(fancy: bool) -> str:
    return "→" if fancy else "->"


def sep(fancy: bool) -> str:
    return " · " if fancy else " | "


def branch(fancy: bool) -> str:
    return "└ " if fancy else "`- "


@dataclass(frozen=True)
class TermCaps:
    """Detected terminal display capabilities."""

    fancy: bool        # Rounded borders + Unicode single-width indicators
    hyperlinks: bool   # OSC 8 clickable links


def detect_caps(env: Mapping[str, str] | None = None) -> TermCaps:
    """Detect terminal capabilities from environment variables.

    Follows DESIGN.md section 6 rules without touching global state when
    an explicit `env` mapping is passed.
    """
    if env is None:
        env = os.environ

    term = env.get("TERM", "")
    term_lower = term.lower()
    term_program = env.get("TERM_PROGRAM", "")
    colorterm = env.get("COLORTERM", "").lower()
    ascii_override = env.get("PIPEDOG_ASCII", "")

    if ascii_override == "1" or term_lower in _DUMB_TERMS:
        return TermCaps(fancy=False, hyperlinks=False)

    fancy = (
        term_program in _FANCY_TERM_PROGRAMS
        or any(kw in term_lower for kw in _FANCY_TERM_KEYWORDS)
        or (
            term_lower.endswith(("-256color", "-truecolor"))
            and colorterm in {"truecolor", "24bit"}
        )
    )
    hyperlinks = bool(fancy and term_program != "Apple_Terminal")
    return TermCaps(fancy=fancy, hyperlinks=hyperlinks)


KIND_SHORT_LABELS: dict[str, str] = {
    "merge": "merge",
    "yellow": "yellow",
    "promote": "promote",
    "deploy": "deploy",
    "deploy_approval": "approve",
    "approve": "approve",
    "rollback": "rollback",
    "deploy_stuck": "stuck",
    "stuck": "stuck",
}

KIND_COLORS: dict[str, str] = {
    "merge": "green",
    "yellow": "yellow",
    "promote": "cyan",
    "deploy": "blue",
    "deploy_approval": "magenta",
    "approve": "magenta",
    "rollback": "red",
    "deploy_stuck": "bold red",
    "stuck": "bold red",
}
