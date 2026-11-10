"""Failure transition detection and desktop/terminal notifications.

See docs/DESIGN.md section 7 (Notify). Rules:
  * state is tracked per (provider, project_ref, pipeline_id);
  * the very first observation of a project never notifies (already failed at start);
  * any non-failed -> failed transition notifies;
  * a new pipeline_id of a known project that is already failed notifies;
  * a pipeline is notified at most once.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Tuple

import model

MODES: tuple[str, ...] = ("auto", "osc9", "osascript", "bell", "off")
OSC9_TERMINALS: frozenset[str] = frozenset({"ghostty", "iTerm.app", "WezTerm"})
MULTIPLEXER_ENVS: tuple[str, ...] = ("TMUX", "HERDR_ENV")

Key = Tuple[str, str, str]


def osc9(msg: str) -> str:
    return "\x1b]9;" + msg + "\x07"


def _sanitize(text: str) -> str:
    # Strip control characters so a project/job name can't break the OSC sequence,
    # but preserve newlines for multi-line messages like the fix hint.
    return "".join(ch for ch in text if ch.isprintable() or ch == "\n")


def effective_status(p: model.Pipeline) -> str | None:
    """Pipeline status, but 'failed' as soon as any leaf failed (pipeline may still run)."""
    if p.fetch_error or p.pipeline_id is None:
        return None
    for job in p.jobs:
        if any(leaf.status == "failed" for leaf in job.leaves()):
            return "failed"
    return p.status


def _first_failed_job(p: model.Pipeline) -> str | None:
    for job in p.jobs:
        for leaf in job.leaves():
            if leaf.status == "failed":
                return leaf.name
    return None


def _failed_job_url(p: model.Pipeline) -> str | None:
    for job in p.jobs:
        for leaf in job.leaves():
            if leaf.status == "failed" and leaf.web_url:
                return leaf.web_url
    return None


def message(p: model.Pipeline) -> str:
    msg = f"Pipedog Alert: {p.project} CI Failed!"
    job = _first_failed_job(p)
    if job:
        msg += f" ({job})"
        hint = f"fix: ask your agent to run pipedog-ci-fix for {p.project} ({job})"
    else:
        hint = f"fix: ask your agent to run pipedog-ci-fix for {p.project}"
    return _sanitize(f"{msg}\n{hint}")


def _default_write(s: str) -> None:
    sys.stdout.write(s)
    sys.stdout.flush()


def _default_spawn(argv: list[str]) -> None:
    # Fire-and-forget so a slow osascript never blocks the poll thread.
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def _applescript_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _infer_event(msg: str) -> str:
    lower = msg.lower()
    if "decision needed" in lower or "waiting for an agent" in lower:
        return "decision"
    if any(k in lower for k in ("stuck", "rollback", "deploy", "verified", "platform did not deploy")):
        return "deploy"
    return "failed"


class WebhookSink:
    def __init__(
        self,
        url: str,
        fmt: str,
        events: Iterable[str],
        post: Callable[..., Any] | None = None,
        *,
        clock: Callable[[], float] | None = None,
        stderr: Any = None,
    ) -> None:
        fmt_clean = fmt.strip().lower() if isinstance(fmt, str) else fmt
        if fmt_clean not in ("slack", "discord", "json"):
            raise ValueError(f"unknown webhook format {fmt!r}")
        self.url = url
        self.fmt = fmt_clean
        self.events = {e.strip().lower() for e in events}
        self._post = post
        self._clock = clock or time.time
        self._stderr = stderr or sys.stderr
        self._backoff_until: float = 0.0

    @property
    def host(self) -> str:
        parts = urllib.parse.urlsplit(self.url)
        return parts.netloc or parts.hostname or ""

    def send(
        self,
        event: str,
        text: str = "",
        url: str | None = None,
        *,
        kind: str | None = None,
        msg: str | None = None,
        link: str | None = None,
        error_summary: str | None = None,
        log_tail: str | None = None,
    ) -> bool:
        eff_event = event or kind or ""
        if eff_event not in self.events:
            return False

        now = self._clock()
        if now < self._backoff_until:
            return False

        raw_text = text or msg or error_summary or ""
        summary_text = raw_text[:200]
        eff_link = url if url is not None else link

        if self.fmt == "slack":
            body_text = f"{summary_text}\n{eff_link}" if eff_link else summary_text
            payload = {"text": body_text}
        elif self.fmt == "discord":
            body_text = f"{summary_text}\n{eff_link}" if eff_link else summary_text
            payload = {"content": body_text}
        elif self.fmt == "json":
            payload = {"event": eff_event, "text": summary_text, "url": eff_link}
        else:
            payload = {"text": summary_text}

        try:
            if self._post is not None:
                try:
                    self._post(self.url, {"Content-Type": "application/json"}, payload, timeout=5)
                except TypeError:
                    try:
                        self._post(self.url, payload)
                    except TypeError:
                        self._post(self.url, {"Content-Type": "application/json"}, payload)
            else:
                import providers.http
                providers.http.post_json(self.url, {}, payload, timeout=5)
            self._backoff_until = 0.0
            return True
        except Exception:
            self._backoff_until = now + 300.0
            try:
                self._stderr.write(f"pipedog: webhook {self.host} failed\n")
                self._stderr.flush()
            except Exception:
                pass
            return False

    deliver = send
    __call__ = send


class Notifier:
    def __init__(
        self,
        mode: str = "auto",
        write: Callable[[str], None] | None = None,
        *,
        env: Mapping[str, str] | None = None,
        platform: str | None = None,
        spawn: Callable[[list[str]], Any] | None = None,
        sinks: Sequence[WebhookSink] | None = None,
    ):
        if mode not in MODES:
            raise ValueError(f"unknown notify mode {mode!r}")
        self.mode = mode
        self._write = write or _default_write
        self._env = os.environ if env is None else env
        self._platform = platform or sys.platform
        self._spawn = spawn or _default_spawn
        self.sinks = list(sinks or [])
        self._last: dict[Key, str | None] = {}
        self._known_projects: set[tuple[str, str]] = set()
        self._notified: set[Key] = set()

    # -- channel selection -------------------------------------------------

    def channels(self) -> list[str]:
        """Concrete delivery channels for the configured mode."""
        if self.mode == "off":
            return []
        if self.mode != "auto":
            return [self.mode]
        mac = self._platform == "darwin"
        if self._env.get("TERM_PROGRAM", "") in OSC9_TERMINALS:
            chans = ["osc9"]
            # Multiplexers may swallow OSC 9; also send a macOS notification.
            if mac and any(self._env.get(k) for k in MULTIPLEXER_ENVS):
                chans.append("osascript")
            return chans
        if mac:
            return ["osascript"]
        return ["bell"]

    def _deliver(
        self, msg: str, event: str | None = None, url: str | None = None
    ) -> None:
        for ch in self.channels():
            try:
                if ch == "osc9":
                    self._write(osc9(msg))
                elif ch == "bell":
                    self._write("\a")
                elif ch == "osascript":
                    script = (f"display notification {_applescript_quote(msg)} "
                              f"with title {_applescript_quote('pipedog')}")
                    self._spawn(["osascript", "-e", script])
            except Exception:
                # Notifications are best effort; never break the monitor.
                pass

        eff_event = event or _infer_event(msg)
        for sink in self.sinks:
            try:
                sink.deliver(eff_event, msg, url=url)
            except Exception:
                pass

    def deliver(
        self, msg: str, event: str | None = None, url: str | None = None
    ) -> None:
        self._deliver(msg, event=event, url=url)

    def notify(
        self, event: str, msg: str, url: str | None = None
    ) -> None:
        self._deliver(msg, event=event, url=url)

    # -- transitions -------------------------------------------------------

    def observe(self, pipelines: list[model.Pipeline]) -> list[str]:
        fired: list[str] = []
        for p in pipelines:
            status = effective_status(p)
            if status is None:
                continue  # fetch error / no pipeline: nothing to compare
            proj = (p.provider, str(p.project_ref))
            key: Key = (p.provider, str(p.project_ref), str(p.pipeline_id))
            first_for_project = proj not in self._known_projects
            prev_known = key in self._last
            prev = self._last.get(key)
            self._known_projects.add(proj)
            self._last[key] = status

            if status != "failed" or key in self._notified:
                continue
            if first_for_project:
                # Already failed when the monitor started: don't alert. A later
                # retry (failed -> running -> failed) is a real transition and alerts.
                continue
            if prev_known and prev == "failed":
                continue
            self._notified.add(key)
            msg = message(p)
            fired.append(msg)
            link = p.web_url or _failed_job_url(p)
            self._deliver(msg, event="failed", url=link)
        return fired
