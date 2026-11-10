"""Single-tick flow and pipeline polling engine shared by TUI, wait, and tick."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import config
import flowmodel
import model
import notify
import snapshot
import store
from engine_clients import (
    BrokenClient,
    MockClient,
    _client_factory,
    _mock_module,
    build_clients,
    build_forges,
    error_pipeline,
    project_ref,
    resolve_tokens,
)
from forge import get_forge
from poller import (
    MAX_FETCH_WORKERS,
    UNACKED_EVENT_NOTIFY_SEC,
    Poller,
)
from poller_cd import CdPollerMixin

_active_holder: Engine | None = None


def _dict_to_job(d: dict[str, Any]) -> model.Job:
    err_d = d.get("error")
    err = model.ErrorInfo(**err_d) if isinstance(err_d, dict) else None
    subs = [_dict_to_job(s) for s in d.get("sub_jobs", []) if isinstance(s, dict)]
    return model.Job(
        name=d.get("name", ""),
        status=d.get("status", "skipped"),
        stage=d.get("stage"),
        job_id=d.get("job_id"),
        web_url=d.get("web_url"),
        started_at=d.get("started_at"),
        finished_at=d.get("finished_at"),
        sub_jobs=subs,
        error=err,
        raw_status=d.get("raw_status"),
        allow_failure=bool(d.get("allow_failure", False)),
        workflow=d.get("workflow"),
    )


def _dict_to_pipeline(d: dict[str, Any]) -> model.Pipeline:
    jobs = [_dict_to_job(j) for j in d.get("jobs", []) if isinstance(j, dict)]
    p = model.Pipeline(
        project=d.get("project", ""),
        short=d.get("short", ""),
        provider=d.get("provider", "mock"),
        project_ref=d.get("project_ref", ""),
        local_path=d.get("local_path"),
        branch=d.get("branch"),
        pipeline_id=d.get("pipeline_id"),
        status=d.get("status"),
        web_url=d.get("web_url"),
        jobs=jobs,
        fetch_error=d.get("fetch_error"),
        fetched_at=d.get("fetched_at"),
        head_sha=d.get("head_sha"),
    )
    if "tracked_ref" in d and d["tracked_ref"] is not None:
        p.tracked_ref = d["tracked_ref"]  # type: ignore[attr-defined]
    return p


@dataclass
class TickResult:
    pipelines: list[model.Pipeline]
    advanced: bool


class Engine:
    """Coordinates lock acquisition, poller execution, and snapshot fallback."""

    def __init__(
        self,
        poller: Poller,
        *,
        poll_interval: float | None = None,
        state_root: Path | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.poller = poller
        self.poll_interval = (
            float(poll_interval)
            if poll_interval is not None
            else float(poller.poll_interval)
        )
        if state_root is not None:
            self.state_root: Path | None = Path(state_root)
        elif poller.state_root is not None:
            self.state_root = Path(poller.state_root)
        elif poller.snapshot_path is not None:
            self.state_root = Path(poller.snapshot_path).parent
        else:
            self.state_root = None
        self.clock = clock if clock is not None else poller.clock
        self._has_lock = False

    def _snapshot_file(self) -> Path:
        if self.poller.snapshot_path is not None:
            return Path(self.poller.snapshot_path)
        if self.state_root is not None:
            return self.state_root / snapshot.FILENAME
        return snapshot.default_path()

    def _read_snapshot_pipelines(self, now: float) -> list[model.Pipeline]:
        snap_path = self._snapshot_file()
        try:
            raw = json.loads(snap_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, dict):
            return []
        pipes = [
            _dict_to_pipeline(p)
            for p in raw.get("pipelines", [])
            if isinstance(p, dict)
        ]
        updated_ts: float | None = None
        updated_str = raw.get("updated_at")
        if isinstance(updated_str, str):
            try:
                updated_ts = datetime.fromisoformat(updated_str).timestamp()
            except ValueError:
                updated_ts = now
        decisions_pending = store.list_decisions(
            status="pending", root=self.state_root
        )
        envs_raw = [
            e for e in raw.get("environments", []) if isinstance(e, dict)
        ]
        raw_prs = raw.get("prs", {})
        restored_prs: dict[tuple[str, str], flowmodel.PrState] = {}
        if isinstance(raw_prs, dict):
            for k_str, v_dict in raw_prs.items():
                if ":" in k_str and isinstance(v_dict, dict):
                    parts = k_str.split(":", 1)
                    try:
                        restored_prs[(parts[0], parts[1])] = flowmodel.from_json(flowmodel.PrState, v_dict)
                    except Exception:
                        pass
        with self.poller._lock:
            self.poller._latest = pipes
            self.poller._updated_at = updated_ts
            self.poller.decisions = decisions_pending
            self.poller.environments = envs_raw
            self.poller.prs = restored_prs
        self.poller.last_data = raw
        return pipes

    def tick(self) -> TickResult:
        global _active_holder
        now = self.clock()
        stale = snapshot.stale_after(self.poll_interval)

        holds = self._has_lock and (_active_holder is self)
        if holds and not store.refresh_engine_lock(root=self.state_root, now=now):
            holds = False  # another process took over a stale lock

        if not holds:
            if store.acquire_engine_lock(stale, root=self.state_root, now=now):
                self._has_lock = True
                _active_holder = self
            else:
                self._has_lock = False
                pipes = self._read_snapshot_pipelines(now)
                return TickResult(pipelines=pipes, advanced=False)

        pipes = self.poller.poll_once()
        return TickResult(pipelines=pipes, advanced=True)

    def close(self) -> None:
        global _active_holder
        if self._has_lock and (_active_holder is self):
            store.release_engine_lock(root=self.state_root)
            _active_holder = None
        self._has_lock = False
        self.poller.close()


def build(
    cfg_path: str | Path | None = None,
    *,
    mock: bool = False,
    interval: float | None = None,
    no_notify: bool = False,
    once: bool = False,
    notifier: notify.Notifier | None = None,
    notify_writer: Callable[[str], None] | None = None,
    client_factory: Callable[[str, str, str | None], Any] | None = None,
    forge_factory: Callable[[str, str, str | None], Any] = get_forge,
    state_root: Path | None = None,
    snapshot_path: Path | None = None,
    rules_file: Path | None = None,
    clock: Callable[[], float] = time.time,
    resolve_branch: Callable[[dict], str | None] = config.resolve_branch,
) -> Engine:
    """Build an Engine from config file or mock mode."""
    path = config.config_path(str(cfg_path) if cfg_path is not None else None)
    cfg: dict[str, Any] | None = None
    if not mock or path.exists():
        try:
            cfg = config.load(path)
        except FileNotFoundError:
            raise
        except config.ConfigError:
            if not mock:
                raise
            cfg = None

    secrets: list[str | None] = []
    forges: list[Any] | None = None
    if mock:
        mock_mod = _mock_module()
        if state_root is None and hasattr(mock_mod, "mock_state_dir"):
            state_root = mock_mod.mock_state_dir()
        if snapshot_path is None and state_root is not None:
            snapshot_path = Path(state_root) / snapshot.FILENAME
        start_ts = (
            mock_mod.resolve_mock_start(root=state_root)
            if hasattr(mock_mod, "resolve_mock_start")
            else clock()
        )
        world = mock_mod.MockWorld(start=start_ts, root=state_root)
        projects = [
            config.normalize_project(p, i) for i, p in enumerate(world.projects())
        ]
        clients: list[Any] = [MockClient(world, clock=clock) for _ in projects]
        forges = [
            world.forge() if hasattr(world, "forge") else None for _ in projects
        ]
        eff_interval = float(
            interval
            or (cfg["poll_interval_sec"] if cfg else config.DEFAULT_POLL_INTERVAL)
        )
        mode = cfg["notify"] if cfg else "auto"
    else:
        assert cfg is not None
        projects = cfg["projects"]
        if not projects:
            raise config.ConfigError(f"no projects configured in {path}")
        tokens = resolve_tokens(cfg)
        webhook_urls = [
            w["url"] for w in (cfg.get("webhooks", []) if cfg else []) if w.get("url")
        ]
        secrets = [t for t in tokens.values() if t] + webhook_urls
        get_cli = client_factory if client_factory is not None else _client_factory()
        clients = build_clients(cfg, tokens, get_cli)
        forges = build_forges(cfg, tokens, forge_factory=forge_factory)
        eff_interval = float(interval or cfg["poll_interval_sec"])
        mode = cfg["notify"]
        gl_url = cfg["gitlab"]["url"]
        gh_url = cfg["github"]["api_url"]

    if mock:
        tokens = {}
        gl_url = config.DEFAULT_GITLAB_URL
        gh_url = config.DEFAULT_GITHUB_API_URL
        webhook_urls = [
            w["url"] for w in (cfg.get("webhooks", []) if cfg else []) if w.get("url")
        ]
        secrets = webhook_urls

    if no_notify or once:
        mode = "off"

    sinks = [
        notify.WebhookSink(w["url"], w["format"], w["events"])
        for w in (cfg.get("webhooks", []) if cfg else [])
    ]
    if no_notify:
        sinks = []

    eff_notifier = notifier
    if eff_notifier is None and ((not once and notify_writer is not None) or sinks):
        eff_notifier = notify.Notifier(mode, notify_writer, sinks=sinks)
    elif eff_notifier is not None and sinks and not eff_notifier.sinks:
        eff_notifier.sinks.extend(sinks)

    poller = Poller(
        projects,
        clients,
        forges=forges,
        poll_interval=eff_interval,
        mock=mock,
        notifier=eff_notifier,
        secrets=secrets,
        snapshot_path=snapshot_path,
        clock=clock,
        resolve_branch=resolve_branch,
        state_root=state_root,
        rules_file=rules_file,
        tokens=tokens,
        gitlab_url=gl_url,
        github_api_url=gh_url,
    )
    return Engine(
        poller,
        poll_interval=eff_interval,
        state_root=state_root,
        clock=clock,
    )
