"""pipedog (pd) - always-on terminal monitor for GitLab CI / GitHub Actions.

Usage:
    monitor.py [--mock] [--config PATH] [--once] [--interval SEC] [--no-notify]

Design (docs/DESIGN.md section 7):
  * Live display with auto_refresh=False; the main thread re-renders every
    second from local data only (elapsed times keep ticking).
  * A background thread polls every poll_interval_sec via engine.tick(),
    fetching all projects in parallel with a ThreadPoolExecutor, then writes
    the snapshot and feeds the Notifier. Polling never blocks the render tick.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import tui

LiveView = tui.LiveView
Live = LiveView

import config
import engine
from engine import (
    MAX_FETCH_WORKERS,
    UNACKED_EVENT_NOTIFY_SEC,
    BrokenClient,
    Engine,
    MockClient,
    Poller,
    build_clients,
    build_forges,
    error_pipeline,
    project_ref,
    resolve_tokens,
)

SKILL_DIR = Path(__file__).resolve().parent.parent
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
RENDER_TICK_SEC = 1.0

__all__ = [
    "BrokenClient",
    "Engine",
    "MAX_FETCH_WORKERS",
    "MockClient",
    "Poller",
    "UNACKED_EVENT_NOTIFY_SEC",
    "build_clients",
    "build_forges",
    "error_pipeline",
    "project_ref",
    "resolve_tokens",
]


# ---------------------------------------------------------------------------
# Lazy imports of sibling modules (easy to stub in tests)
# ---------------------------------------------------------------------------


def _renderer() -> ModuleType:
    import renderer

    return renderer


def _client_factory() -> Callable[[str, str, str | None], Any]:
    from providers import get_client

    return get_client


def _mock_module() -> ModuleType:
    import mock

    return mock


def _as_engine(
    engine_or_poller: Engine | Poller, interval: float | None = None
) -> tuple[Engine, Poller]:
    if isinstance(engine_or_poller, Engine):
        return engine_or_poller, engine_or_poller.poller
    eng = Engine(
        engine_or_poller,
        poll_interval=interval if interval is not None else engine_or_poller.poll_interval,
    )
    return eng, engine_or_poller


def poll_loop(
    engine_or_poller: Engine | Poller,
    interval: float,
    stop: threading.Event,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Background thread body: tick the engine, then wait out the rest of the interval."""
    eng, poller = _as_engine(engine_or_poller, interval=interval)
    while not stop.is_set():
        t0 = monotonic()
        try:
            eng.tick()
        except Exception as exc:  # noqa: BLE001
            poller.last_error = f"poll failed: {type(exc).__name__}"
        stop.wait(max(0.0, interval - (monotonic() - t0)))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TerminalWriter:
    """Queues out-of-band terminal writes (OSC 9 / bell) from the poll thread.

    The main thread drains the queue right before each Live refresh, so writes
    never interleave with a frame being drawn.
    """

    def __init__(self, target: Any = None):
        self.target = target if target is not None else sys.stdout
        self._pending: list[str] = []
        self._lock = threading.Lock()

    def write(self, s: str) -> None:
        with self._lock:
            self._pending.append(s)

    def drain(self) -> None:
        with self._lock:
            data, self._pending = "".join(self._pending), []
        if data:
            f = getattr(self.target, "file", self.target)
            f.write(data)
            f.flush()


def render_view(
    poller: Poller,
    *,
    caps: Any,
    mock: bool,
    now: float | None = None,
    selected: int | None = None,
    subtitle: str | None = None,
    selection_state: Any = None,
    collapse_passed: bool = True,
) -> Any:
    pipelines, updated_at = poller.view()
    kwargs: dict[str, Any] = {
        "now": time.time() if now is None else now,
        "caps": caps,
        "updated_at": updated_at,
        "mock": mock,
        "selected": selected,
        "subtitle_override": subtitle,
        "selection_state": selection_state,
        "collapse_passed": collapse_passed,
    }
    with poller._lock:
        decisions_copy = list(poller.decisions)
        warnings_copy = list(poller.warnings)
        environments_copy = list(getattr(poller, "environments", ()))
        prs_copy = dict(getattr(poller, "prs", {}))
    if decisions_copy:
        kwargs["decisions"] = decisions_copy
    if warnings_copy:
        kwargs["warnings"] = warnings_copy
    if environments_copy:
        kwargs["environments"] = environments_copy
    if prs_copy:
        kwargs["prs"] = prs_copy
    try:
        view: Any = _renderer().render(pipelines, **kwargs)
    except Exception as exc:  # noqa: BLE001 - keep the monitor alive on renderer bugs
        view = tui.Line(f"pipedog: render error: {type(exc).__name__}: {exc}", style="red")
    if poller.last_error:
        warn_icon = "▲" if getattr(caps, "fancy", True) else "[!]"
        return tui.Group(view, tui.Line(f"{warn_icon} {poller.last_error}", style="dim yellow"))
    return view


def run_live(
    engine_or_poller: Engine | Poller,
    *,
    interval: float,
    mock: bool,
    caps: Any,
    console: Any = None,
    stream: Any = None,
    writer: TerminalWriter,
    tick: float = RENDER_TICK_SEC,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    key_reader: Any | None = None,
) -> int:
    """Run the Live loop until Ctrl+C or 'q'; returns the exit code."""
    import cli
    import keys
    import store

    target_stream = stream if stream is not None else (getattr(console, "file", console) or sys.stdout)

    eng, poller = _as_engine(engine_or_poller, interval=interval)
    stop = threading.Event()
    thread = threading.Thread(
        target=poll_loop,
        args=(eng, interval, stop),
        name="pipedog-poll",
        daemon=True,
    )
    reader = key_reader if key_reader is not None else keys.KeyReader()
    with poller._lock:
        init_decisions = list(poller.decisions)
    state = keys.SelectionState(has_pending=bool(init_decisions))
    subtitle: str | None = None
    collapse_passed = True

    try:
        with reader:
            with Live(
                render_view(
                    poller,
                    caps=caps,
                    mock=mock,
                    selected=state.selected,
                    subtitle=subtitle,
                    selection_state=state,
                    collapse_passed=collapse_passed,
                ),
                stream=target_stream,
                auto_refresh=False,
            ) as live:
                thread.start()
                last_render = monotonic() - tick
                poll_interval = min(0.1, tick)
                while True:
                    ch = reader.read_nowait()
                    needs_render = False

                    if ch is not None:
                        if ch == "q" and state.confirm_mode != "high":
                            raise KeyboardInterrupt
                        if ch == "p" and state.selected is None and not state.confirm_mode:
                            collapse_passed = not collapse_passed
                            needs_render = True
                        else:
                            with poller._lock:
                                current_decisions = list(poller.decisions)
                            old_selected_id = state.selected_id
                            state = keys.recompute(state, current_decisions)
                            if old_selected_id is not None and state.selected_id is None:
                                subtitle = state.message
                            old_selected = state.selected
                            saved_sha = state.selected_sha
                            old_confirm_mode = state.confirm_mode
                            old_confirm_typed = state.confirm_typed
                            state, action = keys.handle(
                                ch,
                                state,
                                current_decisions,
                                projects=getattr(poller, "projects", None),
                                environments=getattr(poller, "environments", None),
                                prs=getattr(poller, "prs", None),
                            )
                            if state.message:
                                subtitle = state.message
                                needs_render = True
                            else:
                                if subtitle is not None and (
                                    action is not None
                                    or state.selected != old_selected
                                    or ch in "123456789"
                                    or ch in ("up", "down", "k", "j")
                                ):
                                    subtitle = None
                                    needs_render = True
                                elif (
                                    state.selected != old_selected
                                    or state.confirm_mode != old_confirm_mode
                                    or state.confirm_typed != old_confirm_typed
                                ):
                                    needs_render = True

                            if action is not None:
                                dec_id, ans, rem = action
                                target_d = store.get_decision(dec_id, root=poller.state_root)
                                if (
                                    target_d is not None
                                    and target_d.status == "pending"
                                    and (
                                        target_d.head_sha == saved_sha
                                        or target_d.head_sha == state.selected_sha
                                        or target_d.head_sha == getattr(state, "confirm_target_sha", None)
                                    )
                                ):
                                    failed: str | None = None
                                    try:
                                        cli.apply_decision(
                                            dec_id,
                                            ans,
                                            remember=rem,
                                            root=poller.state_root,
                                            rules_path=poller.rules_file,
                                            answered_by="tui",
                                        )
                                    except Exception as exc:  # noqa: BLE001 - show, never crash the TUI
                                        failed = f"not applied: {exc}"
                                    with poller._lock:
                                        poller.decisions = [
                                            d for d in poller.decisions if d.id != dec_id
                                        ]
                                    state = keys.SelectionState(
                                        selected=None,
                                        message=failed,
                                        has_pending=bool(poller.decisions),
                                    )
                                    subtitle = failed
                                    needs_render = True
                                else:
                                    state = keys.SelectionState(
                                        selected=None,
                                        selected_id=None,
                                        selected_sha=None,
                                        message="decision changed, select again",
                                        has_pending=bool(current_decisions),
                                    )
                                    subtitle = "decision changed, select again"
                                    needs_render = True

                    if state.confirm_mode == "medium" and state.confirm_time is not None:
                        if (time.time() - state.confirm_time) > 3.0:
                            state = keys.SelectionState(
                                selected=state.selected,
                                selected_id=state.selected_id,
                                selected_sha=state.selected_sha,
                                message=None,
                                has_pending=state.has_pending,
                            )
                            if subtitle and "again within 3s" in subtitle:
                                subtitle = None
                            needs_render = True

                    now_mono = monotonic()
                    if needs_render or (tick <= 0.1) or (now_mono - last_render) >= tick:
                        with poller._lock:
                            current_decisions = list(poller.decisions)
                        old_selected_id = state.selected_id
                        state = keys.recompute(state, current_decisions)
                        if old_selected_id is not None and state.selected_id is None:
                            subtitle = state.message
                        writer.drain()
                        live.update(
                            render_view(
                                poller,
                                caps=caps,
                                mock=mock,
                                selected=state.selected,
                                subtitle=subtitle,
                                selection_state=state,
                                collapse_passed=collapse_passed,
                            ),
                            refresh=True,
                        )
                        last_render = now_mono

                    sleep(poll_interval)
    except KeyboardInterrupt:
        pass  # Live.__exit__ already restored the cursor
    finally:
        stop.set()
    try:
        writer.drain()
    except Exception:  # noqa: BLE001
        pass
    return EXIT_OK


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _positive_float(value: str) -> float:
    try:
        f = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from None
    if f <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return f


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="pd",
        description="pipedog - terminal CI/CD pipeline monitor (GitLab CI / GitHub Actions)",
        epilog=(
            "subcommands:\n"
            "  doctor          check health of pipedog, config, tokens, and rate limits\n"
            "  status          query pipeline status from active snapshot\n"
            "  wait            block until snapshot changes or event arrives\n"
            "  ack             acknowledge an unhandled event\n"
            "  decide          answer a pending flow decision\n"
            "  decisions       list pending flow decisions\n"
            "  rules           manage flow rules\n"
            "  comments        view unresolved review comments\n"
            "  reply           post a review comment reply\n"
            "  tick            run a single engine poll tick\n"
            "  install-tick    install background launchd tick agent\n"
            "  uninstall-tick  uninstall background launchd tick agent\n"
            "  envs            list environment deployment states\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--mock", action="store_true", help="run with simulated projects (no network)"
    )
    ap.add_argument(
        "--config",
        metavar="PATH",
        help="config file (default: $PIPEDOG_CONFIG or ~/.config/pipedog/config.json)",
    )
    ap.add_argument(
        "--once",
        action="store_true",
        help="poll once, write snapshot, print, exit (1 if any pipeline failed)",
    )
    ap.add_argument(
        "--interval",
        type=_positive_float,
        metavar="SEC",
        help="poll interval in seconds (overrides poll_interval_sec)",
    )
    ap.add_argument(
        "--no-notify", action="store_true", help="disable failure notifications"
    )
    ap.add_argument(
        "--gallery",
        action="store_true",
        help="print static mock gallery containing every scenario and exit",
    )
    ap.add_argument(
        "--ascii",
        action="store_true",
        help="force ASCII rendering (same as PIPEDOG_ASCII=1)",
    )
    return ap.parse_args(argv)


def missing_config_message(path: Path) -> str:
    example = SKILL_DIR / "config.example.json"
    return (
        f"pipedog: config not found: {path}\n"
        f"\n"
        f"Create it from the example and edit your projects:\n"
        f'  mkdir -p "{path.parent}"\n'
        f'  cp "{example}" "{path}"\n'
        f"\n"
        f"Put tokens in env vars rather than the file:\n"
        f"  export PIPEDOG_GITLAB_TOKEN=...   # or GITLAB_TOKEN\n"
        f"  export PIPEDOG_GITHUB_TOKEN=...   # or GITHUB_TOKEN / `gh auth login`\n"
        f"\n"
        f"Or try the demo without any config:  pd --mock\n"
    )


def _pipedog_threads_busy(timeout: float = 0.5) -> bool:
    """Give our worker threads a moment to finish; True if any is still running."""
    deadline = time.monotonic() + timeout
    me = threading.current_thread()
    for t in threading.enumerate():
        if t is me or not t.name.startswith("pipedog-"):
            continue
        t.join(max(0.0, deadline - time.monotonic()))
    return any(
        t.is_alive() and t is not me and t.name.startswith("pipedog-")
        for t in threading.enumerate()
    )


def _hard_exit(code: int) -> None:
    """Exit without waiting for in-flight HTTP fetches (Ctrl+C must be snappy)."""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(code)


def _run(args: argparse.Namespace) -> int:
    if getattr(args, "ascii", False):
        caps = _renderer().TermCaps(fancy=False, hyperlinks=False)
    else:
        caps = _renderer().detect_caps()

    if getattr(args, "gallery", False):
        mock_mod = _mock_module()
        view = mock_mod.gallery_panel(caps=caps)
        tui.print_view(view)
        return EXIT_OK

    path = config.config_path(args.config)
    writer = TerminalWriter(sys.stdout)

    try:
        eng = engine.build(
            args.config,
            mock=args.mock,
            interval=args.interval,
            no_notify=args.no_notify,
            once=args.once,
            notify_writer=None if args.once else writer.write,
            client_factory=_client_factory(),
        )
    except FileNotFoundError:
        sys.stderr.write(missing_config_message(path))
        return EXIT_USAGE
    except config.ConfigError as exc:
        sys.stderr.write(f"pipedog: {exc}\n")
        return EXIT_USAGE

    poller = eng.poller
    try:
        if args.once:
            eng.tick()
            tui.print_view(render_view(poller, caps=caps, mock=args.mock, collapse_passed=False))
            if poller.last_error:
                sys.stderr.write(f"pipedog: {poller.last_error}\n")
            data = poller.last_data or {}
            return EXIT_FAILURE if data.get("has_failure") else EXIT_OK
        return run_live(
            eng,
            interval=eng.poll_interval,
            mock=args.mock,
            caps=caps,
            writer=writer,
        )
    finally:
        eng.close()


def _cli_subcommands() -> set[str]:
    import cli

    return set(cli.SUBCOMMANDS)


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in _cli_subcommands():
        import cli  # lazy: keep the monitor path free of CLI-only imports

        return cli.main(raw)
    args = parse_args(argv)
    try:
        code = _run(args)
    except KeyboardInterrupt:
        # Ctrl+C outside the Live loop (e.g. during startup / --once): no traceback.
        code = EXIT_OK
    if _pipedog_threads_busy():
        # An HTTP fetch is still in flight; don't make the user wait for its timeout.
        _hard_exit(code)
    return code


if __name__ == "__main__":
    sys.exit(main())
