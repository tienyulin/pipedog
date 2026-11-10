"""Action executor for continuous deployment (environment chain after git flow).

Executes CdAction items returned by cdflow.step_env against the DeployAdapter,
deploystore, and store.
Stdlib only.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import cdflow
import config
import deploymodel
import deploystore
import flowmodel
import store
from deploy_adapters.base import DeployAdapter, DeployError
from deploymodel import (
    ApproveGate,
    DeploymentRecord,
    DeployState,
    EnvConfig,
    MarkStuck,
    PendingGate,
    RecordGood,
    RejectGate,
    StartVerify,
    TriggerDeploy,
    TriggerRollback,
    VerifyProgress,
)
from flowmodel import Ask, Decision, Emit, Notify, VoidDecisions


def _env_name_of(env_cfg: Mapping[str, Any] | EnvConfig | None, fallback: str = "") -> str:
    if env_cfg is None:
        return fallback
    if isinstance(env_cfg, Mapping):
        return str(env_cfg.get("name") or env_cfg.get("environment") or fallback)
    return str(getattr(env_cfg, "name", None) or getattr(env_cfg, "environment", None) or fallback)


def _mark_answered_decisions_executed(
    project_name: str,
    env_name: str,
    kinds: set[str],
    root: Path | None,
) -> None:
    for d in store.list_decisions(root=root):
        if (
            d.project == project_name
            and d.target == env_name
            and d.kind in kinds
            and d.status == "answered"
        ):
            d.status = "executed"
            store.put_decision(d, root=root)


def _ensure_stuck_decision(
    state: DeployState,
    reason: str,
    now: float,
    root: Path | None,
) -> Decision | None:
    existing = [
        d
        for d in store.list_decisions(root=root)
        if d.project == state.project
        and d.target == state.env
        and d.kind == "deploy_stuck"
        and d.status == "pending"
    ]
    if existing:
        return None
    sha = state.sha or ""
    dec = Decision(
        id=store.new_id("dec", now=now),
        kind="deploy_stuck",  # type: ignore[arg-type]
        project=state.project,
        source=state.env,
        target=state.env,
        pr=None,
        head_sha=sha,
        reasons=[
            {
                "summary": f"rollback failed on {state.env}: {reason}",
                "env": state.env,
                "sha": sha,
                "reason": reason,
            }
        ],
        created_at=now,
    )
    store.put_decision(dec, root=root)
    ask_key = f"ask:deploy_stuck:{sha}"
    if ask_key not in state.notified:
        state.notified.append(ask_key)
    return dec


def apply(
    actions: Sequence[cdflow.CdAction],
    *,
    adapter: DeployAdapter | None,
    project: dict[str, Any],
    env_cfg: Mapping[str, Any] | EnvConfig,
    state: DeployState | None = None,
    notify: Callable[[str], None] | None = None,
    root: Path | None = None,
    secrets: Sequence[str | None] = (),
    now: float | None = None,
    warnings: list[str] | None = None,
) -> list[str]:
    """Execute CD actions via adapter/deploystore/store/notify; return redacted log messages."""
    logs: list[str] = []
    ts = time.time() if now is None else float(now)
    proj_name = str(project.get("name") or "")
    env_name = _env_name_of(env_cfg)

    cur_state = state
    if cur_state is not None:
        deploystore.save_state(cur_state, root=root)
    elif proj_name and env_name:
        cur_state = deploystore.get_state(proj_name, env_name, root=root) or DeployState(
            project=proj_name,
            env=env_name,
        )

    def _clean(raw_msg: str) -> str:
        return config.redact(raw_msg, secrets) or raw_msg

    def _log(raw_msg: str) -> None:
        clean = _clean(raw_msg)
        store.log_action(clean, root=root)
        logs.append(clean)

    def _warn(raw_msg: str) -> None:
        clean = _clean(raw_msg)
        if warnings is not None:
            warnings.append(clean)
        _log(f"warn: {clean}")

    def _notify(raw_msg: str) -> None:
        if notify is not None:
            clean = _clean(raw_msg)
            try:
                notify(clean)
            except Exception:
                pass

    def _state_for(p_name: str, e_name: str) -> DeployState:
        nonlocal cur_state
        if cur_state is not None and cur_state.project == p_name and cur_state.env == e_name:
            return cur_state
        st = deploystore.get_state(p_name, e_name, root=root)
        if st is None:
            st = DeployState(project=p_name, env=e_name)
        cur_state = st
        return st

    for action in actions:
        if isinstance(action, TriggerDeploy):
            st = _state_for(action.project, action.env)
            _mark_answered_decisions_executed(
                action.project, action.env, {"deploy"}, root
            )
            if adapter is None:
                continue
            try:
                rec = adapter.deploy(project, env_cfg, action.sha)
            except Exception as exc:
                st.inflight_id = None
                st.inflight_kind = None
                st.phase = "idle"
                trig_key = f"triggered:{action.sha}"
                if trig_key in st.notified:
                    st.notified.remove(trig_key)
                deploystore.save_state(st, root=root)
                _warn(f"{action.project}/{action.env}: deploy failed: {exc}")
                continue

            st.sha = action.sha
            st.inflight_id = rec.id
            st.inflight_kind = "deploy"
            st.phase = "deploying"
            st.waiting_since = (rec.created_at or ts) if cdflow.is_placeholder_id(rec.id) else None
            deploystore.save_state(st, root=root)
            _log(f"deployed {action.sha[:7]} to {action.env} ({action.project})")

        elif isinstance(action, ApproveGate):
            if adapter is None:
                continue
            try:
                adapter.approve(
                    project,
                    env_cfg,
                    PendingGate(
                        id=action.gate_id,
                        env=action.env,
                        sha=(cur_state.sha if cur_state and cur_state.sha else ""),
                    ),
                    "approved via pipedog",
                )
            except Exception as exc:
                err_str = _clean(str(exc))
                if action.decision_id and "not an allowed approver" in err_str.lower():
                    d = store.get_decision(action.decision_id, root=root)
                    if d is not None:
                        d.status = "void"
                        d.note = "not an allowed approver"
                        d.answered_at = time.time()
                        store.put_decision(d, root=root)
                    _notify(
                        f"Pipedog: {action.project}/{action.env} not an allowed approver; please approve in platform UI"
                    )
                _warn(f"{action.project}/{action.env}: approve gate failed: {err_str}")
                continue

            if action.decision_id:
                d = store.get_decision(action.decision_id, root=root)
                if d is not None:
                    d.status = "executed"
                    store.put_decision(d, root=root)
            _log(f"approved {action.env} gate {action.gate_id} ({action.project})")

        elif isinstance(action, RejectGate):
            if adapter is None:
                continue
            try:
                adapter.reject(
                    project,
                    env_cfg,
                    PendingGate(
                        id=action.gate_id,
                        env=action.env,
                        sha=(cur_state.sha if cur_state and cur_state.sha else ""),
                    ),
                    "rejected via pipedog",
                )
            except Exception as exc:
                err_str = _clean(str(exc))
                if action.decision_id and "not an allowed approver" in err_str.lower():
                    d = store.get_decision(action.decision_id, root=root)
                    if d is not None:
                        d.status = "void"
                        d.note = "not an allowed approver"
                        d.answered_at = time.time()
                        store.put_decision(d, root=root)
                _warn(f"{action.project}/{action.env}: reject gate failed: {err_str}")
                continue

            if action.decision_id:
                d = store.get_decision(action.decision_id, root=root)
                if d is not None:
                    d.status = "executed"
                    store.put_decision(d, root=root)
            _log(f"rejected {action.env} gate {action.gate_id} ({action.project})")

        elif isinstance(action, TriggerRollback):
            st = _state_for(action.project, action.env)
            _mark_answered_decisions_executed(
                action.project, action.env, {"rollback", "deploy_stuck"}, root
            )
            if adapter is None:
                continue

            hist = deploystore.history(action.project, action.env, root=root)
            target_rec = next((r for r in hist if r.id == action.to_id), None)
            if target_rec is None:
                target_rec = deploystore.known_good(action.project, action.env, root=root)
            if target_rec is None:
                target_rec = DeploymentRecord(
                    id=action.to_id,
                    sha=st.verified_sha or "",
                    status="success",
                    run_id=action.to_id,
                    job_id=action.to_id,
                )

            try:
                rec = adapter.rollback(project, env_cfg, target_rec)
            except Exception as exc:
                clean_err = _clean(str(exc))
                st.inflight_id = None
                st.inflight_kind = None
                st.phase = "stuck"
                _ensure_stuck_decision(st, clean_err, ts, root)
                deploystore.save_state(st, root=root)
                _notify(
                    f"Pipedog: rollback failed on {action.project}/{action.env} ({clean_err})"
                )
                _warn(f"marked {action.project}/{action.env} stuck: {clean_err}")
                continue

            st.sha = target_rec.sha or st.sha
            st.inflight_id = rec.id
            st.inflight_kind = "rollback"
            st.phase = "rolling_back"
            st.waiting_since = (rec.created_at or ts) if cdflow.is_placeholder_id(rec.id) else None
            deploystore.save_state(st, root=root)
            target_label = (target_rec.sha or action.to_id)[:7]
            _log(f"rolled back {action.env} to {target_label} ({action.project})")

        elif isinstance(action, StartVerify):
            st = _state_for(action.project, action.env)
            st.sha = action.sha
            st.inflight_id = None
            st.inflight_kind = None
            st.phase = "verifying"
            if st.verify is None:
                st.verify = VerifyProgress(sha=action.sha, started_at=ts)
            deploystore.save_state(st, root=root)
            _log(f"started verify for {action.sha[:7]} on {action.env} ({action.project})")

        elif isinstance(action, RecordGood):
            st = _state_for(action.project, action.env)
            deploystore.record_good(action.project, action.env, action.record, root=root)
            st.sha = action.record.sha
            st.verified_sha = action.record.sha
            st.phase = "verified"
            st.verify = None
            deploystore.save_state(st, root=root)
            _log(
                f"verified {action.record.sha[:7]} on {action.env} ({action.project})"
            )

        elif isinstance(action, MarkStuck):
            st = _state_for(action.project, action.env)
            st.inflight_id = None
            st.inflight_kind = None
            st.phase = "stuck"
            deploystore.save_state(st, root=root)
            _notify(
                f"Pipedog: {action.project}/{action.env} stuck ({action.reason})"
            )
            _log(f"marked {action.env} stuck ({action.project}): {action.reason}")

        elif isinstance(action, Ask):
            store.put_decision(action.decision, root=root)
            _log(
                f"asked {action.decision.kind} decision {action.decision.id} "
                f"for {action.decision.project} {action.decision.source}→{action.decision.target}"
            )
            _notify(
                f"Pipedog: decision needed ({action.decision.kind}) "
                f"{action.decision.project} {action.decision.source}"
            )

        elif isinstance(action, Emit):
            store.put_event(action.event, root=root)
            _log(
                f"emitted {action.event.type} event {action.event.id} "
                f"for {action.event.project} {action.event.branch}"
            )

        elif isinstance(action, VoidDecisions):
            for d in store.list_decisions(root=root):
                if (
                    d.project == action.project
                    and d.target == action.source
                    and d.status in ("pending", "answered")
                ):
                    d.status = "void"
                    d.note = action.reason
                    d.answered_at = time.time()
                    store.put_decision(d, root=root)
            _log(f"voided decisions for {action.project} {action.source}: {action.reason}")

        elif isinstance(action, Notify):
            _notify(action.message)
            _log(f"notify: {action.message}")

    return logs


def resolve_existing(
    adapter: DeployAdapter | None,
    project: dict[str, Any],
    env_cfg: Mapping[str, Any] | EnvConfig,
    state: DeployState,
    candidate: str | None = None,
    now: float | None = None,
) -> DeploymentRecord | None:
    """Resolve an existing deployment for the current state.

    If inflight_id is a placeholder and adapter provides resolve_dispatch,
    call resolve_dispatch; otherwise fall back to find_deployment.
    """
    if adapter is None:
        return None

    if cdflow.is_placeholder_id(state.inflight_id) and hasattr(adapter, "resolve_dispatch"):
        dispatched_at = (
            state.waiting_since
            if state.waiting_since is not None
            else (time.time() if now is None else float(now))
        )
        target_sha = state.sha or candidate or ""
        rec = adapter.resolve_dispatch(project, env_cfg, dispatched_at, sha=target_sha)
        if rec is not None:
            return rec

    lookup_sha = (
        (state.sha or candidate)
        if state.inflight_id is not None
        else (
            candidate
            if candidate and candidate != state.verified_sha
            else None
        )
    )
    if lookup_sha and hasattr(adapter, "find_deployment"):
        return adapter.find_deployment(project, env_cfg, lookup_sha)
    return None


observe_existing = resolve_existing
find_existing = resolve_existing
get_existing_deployment = resolve_existing
find_or_resolve_deployment = resolve_existing

