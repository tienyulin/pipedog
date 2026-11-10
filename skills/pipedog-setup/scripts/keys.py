"""Terminal keyboard input and decision selection handler."""

from __future__ import annotations

import io
import os
import select
import sys
import time
import webbrowser
from collections.abc import Mapping, Sequence
try:
    import termios
    import tty
    _HAS_TERMIOS = True
except ImportError:
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]
    _HAS_TERMIOS = False
from dataclasses import dataclass
from typing import Any

import cli
import flowmodel

KEY_MAP: dict[str, tuple[str, bool]] = {
    "a": ("once", False),
    "A": ("allow", True),
    "o": ("once", False),
    "f": ("fix", False),
    "m": ("merge", False),
    "p": ("open", False),
    "n": ("skip", False),
    "d": ("deploy", False),
    "v": ("approve", False),
    "x": ("reject", False),
    "b": ("rollback", False),
    "t": ("retry", False),
}


@dataclass
class SelectionState:
    selected: int | None = None
    selected_id: str | None = None
    selected_sha: str | None = None
    message: str | None = None
    confirm_mode: str | None = None  # "high" | "medium" | None
    confirm_target_id: str | None = None
    confirm_target_sha: str | None = None
    confirm_action: tuple[str, bool] | None = None
    confirm_key: str | None = None
    confirm_time: float | None = None
    confirm_typed: str = ""
    confirm_prompt: str | None = None
    has_pending: bool = True

    @property
    def index(self) -> int | None:
        return self.selected - 1 if self.selected is not None else None


class KeyReader:
    """Non-blocking single keystroke reader. Enabled only when stdin is a TTY."""

    def __init__(self, stdin: Any = None) -> None:
        self.stdin = sys.stdin if stdin is None else stdin
        self._fd: int | None = None
        self._orig_attr: list[Any] | None = None
        self._enabled: bool = False

    def __enter__(self) -> "KeyReader":
        if not _HAS_TERMIOS:
            self._enabled = False
            return self
        try:
            if hasattr(self.stdin, "isatty") and self.stdin.isatty():
                self._fd = self.stdin.fileno()
                self._orig_attr = termios.tcgetattr(self._fd)
                tty.setcbreak(self._fd)
                self._enabled = True
        except (io.UnsupportedOperation, AttributeError, termios.error, OSError):
            self._enabled = False
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if _HAS_TERMIOS and self._enabled and self._fd is not None and self._orig_attr is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._orig_attr)
            except (termios.error, OSError):
                pass
            self._enabled = False

    def read_nowait(self) -> str | None:
        """Read a single character or escape sequence from stdin if available immediately, else None."""
        if not self._enabled or self._fd is None:
            return None
        try:
            r, _, _ = select.select([self._fd], [], [], 0)
        except (OSError, ValueError):
            return None
        if not r:
            return None

        try:
            b1 = os.read(self._fd, 1)
        except OSError:
            return None
        if not b1:
            return None

        # Multi-byte UTF-8 input must not crash (ignore it)
        byte_val = b1[0]
        if byte_val >= 0x80:
            rem = 0
            if (byte_val & 0xE0) == 0xC0:
                rem = 1
            elif (byte_val & 0xF0) == 0xE0:
                rem = 2
            elif (byte_val & 0xF8) == 0xF0:
                rem = 3
            while rem > 0:
                try:
                    r_rem, _, _ = select.select([self._fd], [], [], 0.03)
                except (OSError, ValueError):
                    break
                if r_rem:
                    try:
                        os.read(self._fd, 1)
                    except OSError:
                        break
                    rem -= 1
                else:
                    break
            return None

        if b1 in (b"\r", b"\n"):
            return "enter"

        if b1 == b"\x1b":
            # Wait up to 30 ms (select) for [A/[B (also OA/OB). Bare ESC (nothing follows) = "esc"
            try:
                r2, _, _ = select.select([self._fd], [], [], 0.030)
            except (OSError, ValueError):
                return "esc"
            if not r2:
                return "esc"

            try:
                b2 = os.read(self._fd, 1)
            except OSError:
                return "esc"
            if not b2:
                return "esc"

            if b2 in (b"[", b"O"):
                try:
                    r3, _, _ = select.select([self._fd], [], [], 0.030)
                except (OSError, ValueError):
                    return "esc"
                if not r3:
                    return "esc"
                try:
                    b3 = os.read(self._fd, 1)
                except OSError:
                    return "esc"
                if b3 == b"A":
                    return "up"
                elif b3 == b"B":
                    return "down"
                else:
                    return None
            else:
                return None

        try:
            return b1.decode("utf-8")
        except UnicodeDecodeError:
            return None


def get_decision_url(
    decision: flowmodel.Decision,
    prs: Mapping[tuple[str, str], flowmodel.PrState] | None = None,
) -> str | None:
    """Extract a target URL (PR/MR url or deployment web_url) from a decision."""
    for attr in ("url", "web_url", "target_url"):
        val = getattr(decision, attr, None)
        if val:
            return str(val)
    for r in getattr(decision, "reasons", []) or []:
        if isinstance(r, dict):
            for key in ("url", "web_url", "pr_url", "pipeline_url"):
                val = r.get(key)
                if val:
                    return str(val)
    if prs and getattr(decision, "pr", None):
        pr_state = prs.get((decision.project, decision.source)) or prs.get((decision.project, decision.target))
        if pr_state and getattr(pr_state, "url", None):
            return str(pr_state.url)
    return None


def is_production_target(
    decision: flowmodel.Decision,
    projects: Sequence[Mapping[str, Any]] | None = None,
    environments: Sequence[Mapping[str, Any]] | None = None,
) -> bool:
    """Check whether a decision's target env is production."""
    target = getattr(decision, "target", "")
    if target and target.lower() in ("production", "prod"):
        return True
    for r in getattr(decision, "reasons", []) or []:
        if isinstance(r, dict) and r.get("production") is True:
            return True
    if environments:
        for env in environments:
            if isinstance(env, (dict, Mapping)):
                if env.get("env") == target or env.get("name") == target:
                    if getattr(decision, "project", None) is None or env.get("project") == decision.project:
                        if env.get("production") is True:
                            return True
    if projects:
        for p in projects:
            if isinstance(p, (dict, Mapping)):
                if getattr(decision, "project", None) is not None and p.get("name") != decision.project:
                    continue
                deploy_cfg = p.get("deploy") or {}
                for e in deploy_cfg.get("environments", []):
                    if isinstance(e, (dict, Mapping)):
                        if e.get("name") == target or e.get("environment") == target:
                            if e.get("production") is True:
                                return True
    return False


def recompute(
    state: SelectionState,
    pending: list[flowmodel.Decision],
) -> SelectionState:
    """Recompute display index from selected_id in pending decisions.

    If selected_id is found in pending, update selected to its 1-based index.
    If it disappeared, clear selected and selected_id, setting message to
    'selection cleared: decision no longer pending'.
    """
    has_pending = bool(pending)
    if state.confirm_target_id is not None:
        target_in_pending = None
        for d in pending:
            if d.id == state.confirm_target_id:
                target_in_pending = d
                break
        if target_in_pending is None or (state.confirm_target_sha and target_in_pending.head_sha != state.confirm_target_sha):
            return SelectionState(
                selected=None,
                selected_id=None,
                selected_sha=None,
                message="selection cleared: decision no longer pending",
                has_pending=has_pending,
            )

    if state.selected_id is None:
        if state.selected is not None:
            if 1 <= state.selected <= len(pending):
                dec = pending[state.selected - 1]
                return SelectionState(
                    selected=state.selected,
                    selected_id=dec.id,
                    selected_sha=getattr(dec, "head_sha", None),
                    message=state.message,
                    confirm_mode=state.confirm_mode,
                    confirm_target_id=state.confirm_target_id,
                    confirm_target_sha=state.confirm_target_sha,
                    confirm_action=state.confirm_action,
                    confirm_key=state.confirm_key,
                    confirm_time=state.confirm_time,
                    confirm_typed=state.confirm_typed,
                    confirm_prompt=state.confirm_prompt,
                    has_pending=has_pending,
                )
            return SelectionState(
                selected=None,
                selected_id=None,
                selected_sha=None,
                message="selection cleared: decision no longer pending",
                has_pending=has_pending,
            )
        return SelectionState(
            selected=state.selected,
            selected_id=state.selected_id,
            selected_sha=state.selected_sha,
            message=state.message,
            confirm_mode=state.confirm_mode,
            confirm_target_id=state.confirm_target_id,
            confirm_target_sha=state.confirm_target_sha,
            confirm_action=state.confirm_action,
            confirm_key=state.confirm_key,
            confirm_time=state.confirm_time,
            confirm_typed=state.confirm_typed,
            confirm_prompt=state.confirm_prompt,
            has_pending=has_pending,
        )

    for idx, d in enumerate(pending, start=1):
        if d.id == state.selected_id:
            return SelectionState(
                selected=idx,
                selected_id=state.selected_id,
                selected_sha=state.selected_sha if state.selected_sha is not None else getattr(d, "head_sha", None),
                message=state.message,
                confirm_mode=state.confirm_mode,
                confirm_target_id=state.confirm_target_id,
                confirm_target_sha=state.confirm_target_sha,
                confirm_action=state.confirm_action,
                confirm_key=state.confirm_key,
                confirm_time=state.confirm_time,
                confirm_typed=state.confirm_typed,
                confirm_prompt=state.confirm_prompt,
                has_pending=has_pending,
            )

    return SelectionState(
        selected=None,
        selected_id=None,
        selected_sha=None,
        message="selection cleared: decision no longer pending",
        has_pending=has_pending,
    )


def handle(
    key: str,
    state: SelectionState,
    pending: list[flowmodel.Decision],
    *,
    projects: Sequence[Mapping[str, Any]] | None = None,
    environments: Sequence[Mapping[str, Any]] | None = None,
    prs: Mapping[tuple[str, str], flowmodel.PrState] | None = None,
    now: float | None = None,
) -> tuple[SelectionState, tuple[str, str, bool] | None]:
    """Handle a single keypress.

    Returns (new_selection_state, action) where action is (decision_id, answer, remember).
    """
    has_pending = bool(pending)
    current_now = time.time() if now is None else now

    # High risk confirmation mode handler
    if state.confirm_mode == "high":
        if key == "esc":
            return SelectionState(
                selected=state.selected,
                selected_id=state.selected_id,
                selected_sha=state.selected_sha,
                message="cancelled",
                has_pending=has_pending,
            ), None
        if key == "enter":
            if state.confirm_typed == "prod":
                target_id = state.confirm_target_id
                answer, remember = state.confirm_action or ("", False)
                return SelectionState(
                    selected=None,
                    selected_id=None,
                    selected_sha=None,
                    message=None,
                    has_pending=has_pending,
                ), (target_id, answer, remember)
            return SelectionState(
                selected=state.selected,
                selected_id=state.selected_id,
                selected_sha=state.selected_sha,
                message="cancelled",
                has_pending=has_pending,
            ), None
        if key in ("\x7f", "\x08", "backspace"):
            new_typed = state.confirm_typed[:-1]
            ans = state.confirm_action[0] if state.confirm_action else ""
            sha7 = (state.confirm_target_sha or "")[:7]
            env = ""
            for d in pending:
                if d.id == state.confirm_target_id:
                    env = d.target
                    break
            prompt = f"type prod + Enter to {ans} {sha7} on {env}, Esc cancels: {new_typed}"
            return SelectionState(
                selected=state.selected,
                selected_id=state.selected_id,
                selected_sha=state.selected_sha,
                confirm_mode="high",
                confirm_target_id=state.confirm_target_id,
                confirm_target_sha=state.confirm_target_sha,
                confirm_action=state.confirm_action,
                confirm_key=state.confirm_key,
                confirm_typed=new_typed,
                confirm_prompt=prompt,
                has_pending=has_pending,
            ), None
        if len(key) == 1 and key.isprintable():
            new_typed = state.confirm_typed + key
            ans = state.confirm_action[0] if state.confirm_action else ""
            sha7 = (state.confirm_target_sha or "")[:7]
            env = ""
            for d in pending:
                if d.id == state.confirm_target_id:
                    env = d.target
                    break
            prompt = f"type prod + Enter to {ans} {sha7} on {env}, Esc cancels: {new_typed}"
            return SelectionState(
                selected=state.selected,
                selected_id=state.selected_id,
                selected_sha=state.selected_sha,
                confirm_mode="high",
                confirm_target_id=state.confirm_target_id,
                confirm_target_sha=state.confirm_target_sha,
                confirm_action=state.confirm_action,
                confirm_key=state.confirm_key,
                confirm_typed=new_typed,
                confirm_prompt=prompt,
                has_pending=has_pending,
            ), None
        # Anything else cancels with message cancelled
        return SelectionState(
            selected=state.selected,
            selected_id=state.selected_id,
            selected_sha=state.selected_sha,
            message="cancelled",
            has_pending=has_pending,
        ), None

    # Medium risk confirmation mode handler
    if state.confirm_mode == "medium":
        if state.confirm_time is not None and (current_now - state.confirm_time) > 3.0:
            # Timed out silently -> clear confirm state and fall through to process key
            state = SelectionState(
                selected=state.selected,
                selected_id=state.selected_id,
                selected_sha=state.selected_sha,
                message=None,
                has_pending=has_pending,
            )
        elif key == state.confirm_key:
            # Confirmed within 3s! Apply action
            target_id = state.confirm_target_id
            answer, remember = state.confirm_action or ("", False)
            return SelectionState(
                selected=None,
                selected_id=None,
                selected_sha=None,
                message=None,
                has_pending=has_pending,
            ), (target_id, answer, remember)
        elif key == "esc":
            # Cancelled silently
            return SelectionState(
                selected=state.selected,
                selected_id=state.selected_id,
                selected_sha=state.selected_sha,
                message=None,
                has_pending=has_pending,
            ), None
        else:
            # Different key pressed -> cancel confirm silently and continue to process key
            state = SelectionState(
                selected=state.selected,
                selected_id=state.selected_id,
                selected_sha=state.selected_sha,
                message=None,
                has_pending=has_pending,
            )

    if key == "q":
        raise KeyboardInterrupt

    # Up/down/k/j navigation
    if key in ("up", "down", "k", "j"):
        if not pending:
            return state, None
        n = len(pending)
        if state.selected is None:
            new_sel = 1
        else:
            if key in ("up", "k"):
                new_sel = max(1, state.selected - 1)
            else:
                new_sel = min(n, state.selected + 1)
        target = pending[new_sel - 1]
        return SelectionState(
            selected=new_sel,
            selected_id=target.id,
            selected_sha=getattr(target, "head_sha", None),
            message=None,
            has_pending=has_pending,
        ), None

    if key in "123456789":
        num = int(key)
        if 1 <= num <= len(pending):
            target = pending[num - 1]
            return SelectionState(
                selected=num,
                selected_id=target.id,
                selected_sha=getattr(target, "head_sha", None),
                message=None,
                has_pending=has_pending,
            ), None
        return SelectionState(
            selected=state.selected,
            selected_id=state.selected_id,
            selected_sha=state.selected_sha,
            message=f"no decision [{num}]",
            has_pending=has_pending,
        ), None

    decision = None
    if state.selected_id is not None:
        for d in pending:
            if d.id == state.selected_id:
                decision = d
                break
    elif state.selected is not None and 1 <= state.selected <= len(pending):
        decision = pending[state.selected - 1]

    if key == "w":
        if decision is None:
            return state, None
        url = get_decision_url(decision, prs=prs)
        if not url:
            return SelectionState(
                selected=state.selected,
                selected_id=state.selected_id,
                selected_sha=state.selected_sha,
                message="no link for this decision",
                has_pending=has_pending,
            ), None
        is_ssh = bool(os.environ.get("SSH_CONNECTION"))
        is_linux_headless = sys.platform.startswith("linux") and not os.environ.get("DISPLAY")
        if is_ssh or is_linux_headless:
            return SelectionState(
                selected=state.selected,
                selected_id=state.selected_id,
                selected_sha=state.selected_sha,
                message=f"open: {url}",
                has_pending=has_pending,
            ), None
        try:
            opened = webbrowser.open(url)
        except Exception:
            opened = False
        if not opened:
            return SelectionState(
                selected=state.selected,
                selected_id=state.selected_id,
                selected_sha=state.selected_sha,
                message=f"open: {url}",
                has_pending=has_pending,
            ), None
        return SelectionState(
            selected=state.selected,
            selected_id=state.selected_id,
            selected_sha=state.selected_sha,
            message=None,
            has_pending=has_pending,
        ), None

    if key in KEY_MAP:
        if decision is None:
            return state, None

        answer, remember = KEY_MAP[key]
        valid_answers = cli.VALID_ANSWERS.get(decision.kind, set())

        if answer not in valid_answers:
            return (
                SelectionState(
                    selected=state.selected,
                    selected_id=state.selected_id,
                    selected_sha=state.selected_sha,
                    message=f"key not valid for {decision.kind}",
                    has_pending=has_pending,
                ),
                None,
            )

        # Risk classification:
        # Low risk: yellow (once/skip/fix/allow), any skip
        # High risk: deploy, deploy_approval, rollback, deploy_stuck on production
        # Medium risk: merge, promote, non-prod deploy/approve/rollback
        if answer == "skip" or decision.kind == "yellow":
            is_high = False
            is_med = False
        elif decision.kind in ("deploy", "deploy_approval", "rollback", "deploy_stuck"):
            if is_production_target(decision, projects=projects, environments=environments):
                is_high = True
                is_med = False
            else:
                is_high = False
                is_med = True
        elif decision.kind in ("merge", "promote"):
            is_high = False
            is_med = True
        else:
            is_high = False
            is_med = False

        if is_high:
            sha7 = (decision.head_sha or "")[:7]
            env = decision.target
            prompt = f"type prod + Enter to {answer} {sha7} on {env}, Esc cancels: "
            return SelectionState(
                selected=state.selected,
                selected_id=state.selected_id,
                selected_sha=state.selected_sha,
                confirm_mode="high",
                confirm_target_id=decision.id,
                confirm_target_sha=decision.head_sha,
                confirm_action=(answer, remember),
                confirm_key=key,
                confirm_typed="",
                confirm_prompt=prompt,
                has_pending=has_pending,
            ), None

        if is_med:
            prompt = f"press {key} again within 3s to {answer}"
            return SelectionState(
                selected=state.selected,
                selected_id=state.selected_id,
                selected_sha=state.selected_sha,
                message=prompt,
                confirm_mode="medium",
                confirm_target_id=decision.id,
                confirm_target_sha=decision.head_sha,
                confirm_action=(answer, remember),
                confirm_key=key,
                confirm_time=current_now,
                confirm_prompt=prompt,
                has_pending=has_pending,
            ), None

        return (
            SelectionState(
                selected=None,
                selected_id=None,
                selected_sha=None,
                message=None,
                has_pending=has_pending,
            ),
            (decision.id, answer, remember),
        )

    return state, None

