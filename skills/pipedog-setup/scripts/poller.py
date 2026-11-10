"""Parallel pipeline poller for pipedog engine."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import config
import deploy_adapters
import flow
import flow_exec
import flowmodel
import model
import notify
import rules as rules_mod
import snapshot
import store
from engine_clients import _mock_module, error_pipeline
from poller_cd import CdPollerMixin

MAX_FETCH_WORKERS = 8
UNACKED_EVENT_NOTIFY_SEC = 600.0


class Poller(CdPollerMixin):
    """Fetches every project in parallel; keeps the latest result for rendering."""

    def __init__(
        self,
        projects: list[dict],
        clients: list[Any],
        *,
        poll_interval: float,
        mock: bool,
        notifier: notify.Notifier | None = None,
        secrets: Sequence[str | None] = (),
        snapshot_path: Path | None = None,
        clock: Callable[[], float] = time.time,
        resolve_branch: Callable[[dict], str | None] = config.resolve_branch,
        forges: Sequence[Any] | None = None,
        state_root: Path | None = None,
        rules_file: Path | None = None,
        adapter_factory: Callable[..., Any] | None = None,
        tokens: Mapping[str, str | None] | None = None,
        gitlab_url: str | None = None,
        github_api_url: str | None = None,
    ):
        if len(projects) != len(clients):
            raise ValueError("projects and clients must have the same length")
        self.projects = projects
        self.clients = clients
        self.forges = list(forges) if forges is not None else [None] * len(projects)
        self.poll_interval = poll_interval
        self.mock = mock
        self.notifier = notifier
        self.secrets = [s for s in secrets if s]
        if mock and state_root is None:
            mock_mod = _mock_module()
            if hasattr(mock_mod, "mock_state_dir"):
                state_root = mock_mod.mock_state_dir()
        if mock and snapshot_path is None and state_root is not None:
            snapshot_path = Path(state_root) / snapshot.FILENAME
        self.snapshot_path = snapshot_path
        self.state_root = state_root
        self.adapter_factory = adapter_factory or deploy_adapters.get
        self.tokens = dict(tokens) if tokens is not None else {}
        self.gitlab_url = gitlab_url or config.DEFAULT_GITLAB_URL
        self.github_api_url = github_api_url or config.DEFAULT_GITHUB_API_URL
        if rules_file is not None:
            self.rules_file = Path(rules_file)
        elif mock:
            state_dir = (
                self.state_root
                if self.state_root is not None
                else _mock_module().mock_state_dir()
            )
            self.rules_file = rules_mod.rules_path(mock=True, root=state_dir)
        else:
            self.rules_file = rules_mod.rules_path()
        self.clock = clock
        self.resolve_branch = resolve_branch
        self.last_error: str | None = None
        self.last_data: dict[str, Any] | None = None
        self.decisions: list[flowmodel.Decision] = []
        self.warnings: list[str] = []
        self.environments: list[dict[str, Any]] = []
        self.prs: dict[tuple[str, str], flowmodel.PrState] = {}
        self._ref_state: dict[tuple[str, str], flowmodel.TrackedRef] = {}
        self._latest: list[model.Pipeline] = []
        self._updated_at: float | None = None
        self._lock = threading.Lock()
        workers = max(1, min(MAX_FETCH_WORKERS, len(projects)))
        self._pool = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="pipedog-fetch"
        )

    def view(self) -> tuple[list[model.Pipeline], float | None]:
        with self._lock:
            return list(self._latest), self._updated_at

    def _redact(self, text: str | None) -> str | None:
        return config.redact(text, self.secrets)

    def _scrub(self, p: model.Pipeline) -> model.Pipeline:
        p.fetch_error = self._redact(p.fetch_error)
        for job in p.jobs:
            for leaf in [job, *job.sub_jobs]:
                if leaf.error is not None:
                    leaf.error.error_summary = (
                        self._redact(leaf.error.error_summary) or ""
                    )
                    leaf.error.log_tail = self._redact(leaf.error.log_tail) or ""
        return p

    def _deliver_notification(self, msg: str) -> None:
        if self.notifier is not None:
            clean = self._redact(msg) or msg
            try:
                self.notifier._deliver(clean)
            except Exception:  # noqa: BLE001
                pass

    def _fetch_branch(
        self, project: dict, client: Any, branch: str | None
    ) -> model.Pipeline:
        try:
            p = client.fetch(project, branch)
        except Exception as exc:  # noqa: BLE001 - fetch must never take the monitor down
            p = error_pipeline(
                project, branch, f"{type(exc).__name__}: {exc}", self.clock()
            )
        if not isinstance(p, model.Pipeline):
            p = error_pipeline(
                project, branch, "provider returned no pipeline", self.clock()
            )
        if p.fetched_at is None:
            p.fetched_at = self.clock()
        return self._scrub(p)

    def _fetch_one(self, project: dict, client: Any) -> model.Pipeline:
        try:
            branch = self.resolve_branch(project)
        except Exception:  # noqa: BLE001
            branch = None
        return self._fetch_branch(project, client, branch)

    def _poll_project(
        self,
        project: dict,
        client: Any,
        forge: Any,
        loaded_rules: list[flowmodel.Rule],
        now: float,
    ) -> tuple[list[model.Pipeline], list[str], list[dict[str, Any]], dict[tuple[str, str], flowmodel.PrState]]:
        pipelines: list[model.Pipeline] = []
        proj_warnings: list[str] = []
        proj_prs: dict[tuple[str, str], flowmodel.PrState] = {}
        proj_name = str(project.get("name") or "")

        if not project.get("flow"):
            p = self._fetch_one(project, client)
            pipelines = [p]
            if forge is not None and p.branch:
                try:
                    pr_found = forge.find_open_pr(project, p.branch, "main")
                    if pr_found:
                        proj_prs[(proj_name, p.branch)] = pr_found
                except Exception:
                    pass
        else:
            flow_cfg = project["flow"]
            chain = list(flow_cfg.get("chain") or [])
            refs = self._tracked_refs_for_project(project)
            if not refs:
                p = self._fetch_one(project, client)
                pipelines = [p]
                if forge is not None and p.branch:
                    try:
                        pr_found = forge.find_open_pr(project, p.branch, "main")
                        if pr_found:
                            proj_prs[(proj_name, p.branch)] = pr_found
                    except Exception:
                        pass
            else:
                for ref in refs:
                    p = self._fetch_branch(project, client, ref.branch)
                    p.tracked_ref = ref  # type: ignore[attr-defined]
                    pipelines.append(p)

                    if p.fetch_error:
                        continue

                    try:
                        pr: flowmodel.PrState | None = None
                        if forge is not None:
                            if ref.pr is not None:
                                pr = forge.get_pr(project, ref.pr)
                            else:
                                tgt = ref.target or flow.target_for(flow_cfg, ref.branch)
                                if tgt:
                                    pr = forge.find_open_pr(project, ref.branch, tgt)
                        if pr is not None and ref.branch:
                            proj_prs[(proj_name, ref.branch)] = pr

                        existing_decisions = [
                            d
                            for d in store.list_decisions(root=self.state_root)
                            if d.project == proj_name and d.source == ref.branch
                        ]
                        seen = (
                            store.seen_comments(
                                proj_name, pr.number, root=self.state_root
                            )
                            if pr is not None
                            else set()
                        )
                        obs = flow.Observation(
                            pipeline=p,
                            pr=pr,
                            existing_decisions=existing_decisions,
                            rules=loaded_rules,
                            now=now,
                            seen_comments=seen,
                        )
                        new_ref, actions = flow.step(ref, obs, flow_cfg, project)
                        flow_exec.apply(
                            actions,
                            forge=forge,
                            project=project,
                            notify=self._deliver_notification,
                            root=self.state_root,
                            secrets=self.secrets,
                        )

                        def _merge_owned(
                            owned_now: list[flowmodel.TrackedRef],
                            _p: str = proj_name,
                            _nr: flowmodel.TrackedRef = new_ref,
                        ) -> list[flowmodel.TrackedRef] | None:
                            for idx_o, o_item in enumerate(owned_now):
                                if o_item.project == _p and o_item.branch == _nr.branch:
                                    _nr.owned = True
                                    if o_item.pr is not None:
                                        _nr.pr = o_item.pr
                                    if o_item.target:
                                        _nr.target = o_item.target
                                    owned_now[idx_o] = _nr
                                    return owned_now
                            return None

                        store.update_owned(_merge_owned, root=self.state_root)

                        self._ref_state[(proj_name, new_ref.branch)] = new_ref
                        p.tracked_ref = new_ref  # type: ignore[attr-defined]
                    except Exception as exc:  # noqa: BLE001 - isolate flow/forge errors to this ref
                        msg = (
                            self._redact(f"{proj_name} ({ref.branch}): {exc}")
                            or f"{proj_name}: flow error"
                        )
                        proj_warnings.append(msg)

        proj_envs: list[dict[str, Any]] = []
        if project.get("deploy"):
            proj_envs, cd_warns = self._poll_cd_for_project(
                project, client, pipelines, now
            )
            proj_warnings.extend(cd_warns)

        return pipelines, proj_warnings, proj_envs, proj_prs

    def _check_overdue_events(self, now: float) -> None:
        if store.has_wait_lock(root=self.state_root):
            return
        unacked = store.list_events(unacked_only=True, root=self.state_root)
        overdue = [
            e
            for e in unacked
            if (now - e.created_at) >= UNACKED_EVENT_NOTIFY_SEC
            and e.notified_at is None
        ]
        if not overdue:
            return
        for e in overdue:
            e.notified_at = now
            store.put_event(e, root=self.state_root)
        self._deliver_notification(
            f"Pipedog: {len(overdue)} event(s) waiting for an agent"
        )

    def _cleanup_stale_pending_decisions(
        self,
        known_prs: dict[tuple[str, str], flowmodel.PrState] | None = None,
    ) -> None:
        pending = store.list_decisions(status="pending", root=self.state_root)
        if not pending:
            return

        proj_map: dict[str, tuple[dict, Any]] = {}
        for p, f in zip(self.projects, self.forges):
            p_name = str(p.get("name") or "")
            proj_map[p_name] = (p, f)

        tracked_refs: set[tuple[str, str]] = set()
        for p in self.projects:
            p_name = str(p.get("name") or "")
            for ref in self._tracked_refs_for_project(p):
                tracked_refs.add((p_name, ref.branch))

        forge_pr_cache: dict[tuple[str, int], Any] = {}
        if known_prs:
            for (p_name, _), pr_obj in known_prs.items():
                if pr_obj and getattr(pr_obj, "number", None) is not None:
                    forge_pr_cache[(p_name, pr_obj.number)] = pr_obj

        for d in pending:
            if d.pr is None:
                continue

            # 1. Untracked source ref -> void
            if (d.project, d.source) not in tracked_refs:
                d.status = "void"
                d.note = "ref untracked"
                d.answered_at = self.clock()
                store.put_decision(d, root=self.state_root)
                continue

            proj_info = proj_map.get(d.project)
            if not proj_info:
                d.status = "void"
                d.note = "ref untracked"
                d.answered_at = self.clock()
                store.put_decision(d, root=self.state_root)
                continue

            project_dict, forge = proj_info
            if forge is None:
                continue

            # 2. Look up PR on forge (rate-limited to once per decision/PR per tick)
            cache_key = (d.project, d.pr)
            if cache_key not in forge_pr_cache:
                try:
                    pr = forge.get_pr(project_dict, d.pr)
                    forge_pr_cache[cache_key] = pr
                except Exception:
                    forge_pr_cache[cache_key] = "error"

            pr_res = forge_pr_cache.get(cache_key)
            if pr_res == "error" or pr_res is None:
                # Skip if forge errors; leave pending
                continue

            if getattr(pr_res, "state", None) in ("merged", "closed"):
                d.status = "void"
                d.note = f"PR #{d.pr} {pr_res.state}"
                d.answered_at = self.clock()
                store.put_decision(d, root=self.state_root)

    def poll_once(self) -> list[model.Pipeline]:
        now = self.clock()
        loaded_rules, rules_err = rules_mod.load(self.rules_file)
        poll_warnings: list[str] = []
        if rules_err:
            poll_warnings.append(rules_err)

        futures = [
            self._pool.submit(
                self._poll_project, proj, client, forge, loaded_rules, now
            )
            for proj, client, forge in zip(self.projects, self.clients, self.forges)
        ]
        pipelines: list[model.Pipeline] = []
        all_envs: list[dict[str, Any]] = []
        all_prs: dict[tuple[str, str], flowmodel.PrState] = {}
        for f in futures:
            proj_pipes, proj_warns, proj_envs, proj_prs = f.result()
            pipelines.extend(proj_pipes)
            poll_warnings.extend(proj_warns)
            all_envs.extend(proj_envs)
            all_prs.update(proj_prs)

        now = self.clock()
        try:
            self._check_overdue_events(now)
        except Exception:  # noqa: BLE001
            pass

        try:
            self._cleanup_stale_pending_decisions(all_prs)
        except Exception:  # noqa: BLE001
            pass

        decisions_pending = store.list_decisions(status="pending", root=self.state_root)
        events_unacked = len(store.list_events(unacked_only=True, root=self.state_root))

        with self._lock:
            self._latest = pipelines
            self._updated_at = now
            self.decisions = decisions_pending
            self.warnings = poll_warnings
            self.environments = all_envs
            self.prs = all_prs

        data = snapshot.build(
            pipelines,
            now=now,
            poll_interval=self.poll_interval,
            mock=self.mock,
            decisions_pending=decisions_pending,
            events_unacked=events_unacked,
            environments=all_envs,
            prs=all_prs,
        )
        self.last_data = data
        try:
            snapshot.write(data, self.snapshot_path)
            self.last_error = None
        except OSError as exc:
            self.last_error = self._redact(
                f"snapshot write failed: {exc.strerror or type(exc).__name__}"
            )
        if self.notifier is not None:
            try:
                self.notifier.observe(pipelines)
            except Exception:  # noqa: BLE001 - notifications are best effort
                pass
        return pipelines

    def close(self) -> None:
        try:
            self._pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            self._pool.shutdown(wait=False)
