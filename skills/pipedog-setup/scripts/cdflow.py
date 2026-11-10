"""Pure state machine for continuous deployment (environment chain after git flow).

No I/O is performed here; callers supply an EnvObservation and execute the
returned CdAction list via cd_exec.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Mapping, Union

import deploymodel
import store
import verify as verify_mod
from deploymodel import (
    ApproveGate,
    CheckResult,
    DeploymentRecord,
    DeployState,
    EnvConfig,
    EnvState,
    MarkStuck,
    PendingGate,
    RecordGood,
    RejectGate,
    StartVerify,
    TriggerDeploy,
    TriggerRollback,
    VerifyProgress,
)
from flowmodel import Ask, Decision, Emit, Event, Notify, VoidDecisions

CdAction = Union[
    TriggerDeploy,
    ApproveGate,
    RejectGate,
    TriggerRollback,
    StartVerify,
    RecordGood,
    MarkStuck,
    Ask,
    Emit,
    Notify,
    VoidDecisions,
]

PLATFORM_WAIT_TIMEOUT_SEC = 900.0


def is_placeholder_id(dep_id: str | None) -> bool:
    """True when inflight_id is a provisional dispatch/rerun placeholder rather than a platform ID."""
    if not dep_id:
        return False
    return str(dep_id).startswith(("dispatch-", "rerun-", "provisional-"))


def _sha_matches(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


@dataclass
class EnvObservation:
    """Snapshot of external state for a single (project, env) at one tick."""

    env_state: EnvState | None = None
    gate: PendingGate | None = None
    existing: DeploymentRecord | None = None
    check: CheckResult | None = None
    known_good: DeploymentRecord | None = None
    decisions: list[Decision] = field(default_factory=list)
    now: float = 0.0


def candidate_sha(
    env_index: int,
    *,
    branch_green_sha: str | None = None,
    prev_env: DeployState | Mapping[str, Any] | None = None,
) -> str | None:
    """Return the candidate SHA to deploy for env[env_index].

    Rule 1:
    - env[0]: latest green SHA on after_branch
    - env[i>0]: prev_env.verified_sha
    """
    if env_index <= 0:
        return branch_green_sha or None
    if prev_env is None:
        return None
    if isinstance(prev_env, Mapping):
        val = prev_env.get("verified_sha")
        return str(val) if val else None
    return prev_env.verified_sha or None


def _normalize_cfg(
    cfg: Mapping[str, Any] | EnvConfig,
    fallback_env: str,
) -> dict[str, Any]:
    if dataclasses.is_dataclass(cfg) and not isinstance(cfg, type):
        raw = dataclasses.asdict(cfg)
        has_explicit_trigger = True
        has_explicit_rollback = True
    else:
        raw = dict(cfg)
        has_explicit_trigger = "trigger" in raw and raw["trigger"] is not None
        has_explicit_rollback = "rollback" in raw and raw["rollback"] is not None

    name = str(raw.get("name") or fallback_env)
    env_label = str(raw.get("environment") or name)
    adapter = str(raw.get("adapter") or "github")

    if "production" in raw and raw["production"] is not None:
        is_prod = bool(raw["production"])
    else:
        is_prod = name.lower() in ("prod", "production") or env_label.lower() in (
            "prod",
            "production",
        )

    managed_by = raw.get("managed_by") or (
        "platform" if adapter in ("github", "gitlab") else "pd"
    )

    if has_explicit_trigger:
        trigger = str(raw["trigger"])
    else:
        trigger = "ask" if is_prod else "auto"

    if has_explicit_rollback:
        rollback = str(raw["rollback"])
    else:
        rollback = "ask" if is_prod else "auto"

    return {
        "name": name,
        "environment": env_label,
        "adapter": adapter,
        "production": is_prod,
        "managed_by": managed_by,
        "trigger": trigger,
        "rollback": rollback,
        "verify": raw.get("verify"),
    }


def _after_branch(project: Mapping[str, Any] | None) -> str:
    if not isinstance(project, Mapping):
        return "main"
    deploy_cfg = project.get("deploy")
    if isinstance(deploy_cfg, Mapping) and deploy_cfg.get("after_branch"):
        return str(deploy_cfg["after_branch"])
    flow_cfg = project.get("flow")
    if isinstance(flow_cfg, Mapping):
        chain = flow_cfg.get("chain")
        if isinstance(chain, list) and chain:
            return str(chain[-1])
    branch = project.get("branch")
    if isinstance(branch, str) and branch and branch != "auto":
        return branch
    return "main"


def _decision_source(project: Mapping[str, Any] | None, env_name: str) -> str:
    after_br = _after_branch(project)
    if not isinstance(project, Mapping):
        return after_br
    deploy_cfg = project.get("deploy")
    if isinstance(deploy_cfg, Mapping):
        envs = deploy_cfg.get("environments")
        if isinstance(envs, list):
            for idx, e in enumerate(envs):
                e_name = (
                    e.get("name")
                    if isinstance(e, Mapping)
                    else getattr(e, "name", None)
                )
                if e_name == env_name:
                    if idx == 0:
                        return after_br
                    prev = envs[idx - 1]
                    prev_name = (
                        prev.get("name")
                        if isinstance(prev, Mapping)
                        else getattr(prev, "name", None)
                    )
                    return str(prev_name or after_br)
    return after_br


def _notify_once(
    state: DeployState,
    actions: list[CdAction],
    key: str,
    message: str,
) -> None:
    if key not in state.notified:
        state.notified.append(key)
        actions.append(Notify(message=message))


def _env_decisions(
    obs: EnvObservation,
    project_name: str,
    env_name: str,
    kind: str | None = None,
) -> list[Decision]:
    out: list[Decision] = []
    for d in obs.decisions:
        if d.status not in ("pending", "answered"):
            continue
        if d.project and d.project != project_name:
            continue
        if d.target and d.target != env_name:
            continue
        if not d.target and d.source and d.source != env_name:
            continue
        if kind is not None and d.kind != kind:
            continue
        out.append(d)
    return out


def _make_decision(
    *,
    kind: str,
    state: DeployState,
    head_sha: str,
    reasons: list[dict[str, Any]],
    now: float,
    source: str | None = None,
) -> Decision:
    return Decision(
        id=store.new_id("dec", now=now),
        kind=kind,  # type: ignore[arg-type]
        project=state.project,
        source=source or state.env,
        target=state.env,
        pr=None,
        head_sha=head_sha,
        reasons=reasons,
        created_at=now,
    )


def _decision_attempt(dec: Decision) -> int | None:
    """Attempt counter recorded in a deploy_stuck decision's reasons, if any."""
    for r in dec.reasons or []:
        if isinstance(r, Mapping) and isinstance(r.get("attempt"), int):
            return int(r["attempt"])
    return None


def _late_deployment_matches(state: DeployState, obs: EnvObservation) -> bool:
    """True when obs.existing is the deployment the in-flight dispatch was waiting for."""
    ex = obs.existing
    if ex is None or state.inflight_id is None:
        return False
    if is_placeholder_id(state.inflight_id):
        return bool(ex.id) and not is_placeholder_id(ex.id)
    return ex.id == state.inflight_id or bool(
        state.sha and _sha_matches(ex.sha, state.sha)
    )


def _ask_dispatch_stuck(
    state: DeployState,
    obs: EnvObservation,
    actions: list[CdAction],
    stuck_sha: str,
) -> None:
    """Raise (once per attempt) the deploy_stuck decision for a dispatch timeout."""
    attempt = state.stuck_attempts
    key = f"ask:deploy_stuck:{stuck_sha}:{attempt}"
    if key in state.notified:
        return
    state.notified.append(key)
    verb = "rollback" if state.inflight_kind == "rollback" else "deploy"
    minutes = int(PLATFORM_WAIT_TIMEOUT_SEC // 60)
    dec = _make_decision(
        kind="deploy_stuck",
        state=state,
        head_sha=stuck_sha,
        reasons=[
            {
                "summary": (
                    f"{verb} {stuck_sha[:7]} to {state.env} dispatched but no "
                    f"deployment appeared after {minutes}m, retry?"
                ),
                "env": state.env,
                "sha": stuck_sha,
                "reason": "dispatch_timeout",
                "attempt": attempt,
            }
        ],
        now=obs.now,
    )
    actions.append(Ask(decision=dec))


def _handle_gate(
    state: DeployState,
    cfg_d: dict[str, Any],
    obs: EnvObservation,
    candidate: str | None,
    actions: list[CdAction],
    *,
    dec_source: str | None = None,
) -> None:
    """Rule 5: Gate detection, auto/ask approval, and voiding when gate disappears."""
    gate_decisions = _env_decisions(obs, state.project, state.env, kind="deploy_approval")

    if obs.gate is None:
        if any(d.status in ("pending", "answered") for d in gate_decisions):
            actions.append(
                VoidDecisions(
                    project=state.project,
                    source=state.env,
                    reason="approval gate no longer pending",
                )
            )
        return

    gate = obs.gate
    gate_sha = gate.sha or state.sha or candidate or ""

    if not cfg_d["production"] and cfg_d["trigger"] == "auto":
        auto_key = f"auto_approved:{gate.id}"
        if auto_key not in state.notified:
            state.notified.append(auto_key)
            actions.append(
                ApproveGate(
                    project=state.project,
                    env=state.env,
                    gate_id=gate.id,
                    decision_id=None,
                )
            )
        return

    if gate_decisions:
        latest = gate_decisions[-1]
        if latest.status == "answered":
            act_key = f"gate_acted:{gate.id}:{latest.id}"
            if act_key not in state.notified:
                state.notified.append(act_key)
                if latest.answer == "approve":
                    actions.append(
                        ApproveGate(
                            project=state.project,
                            env=state.env,
                            gate_id=gate.id,
                            decision_id=latest.id,
                        )
                    )
                elif latest.answer == "reject":
                    actions.append(
                        RejectGate(
                            project=state.project,
                            env=state.env,
                            gate_id=gate.id,
                            decision_id=latest.id,
                        )
                    )
        return

    ask_key = f"ask:deploy_approval:{gate.id}"
    if ask_key not in state.notified:
        state.notified.append(ask_key)
        dec = _make_decision(
            kind="deploy_approval",
            state=state,
            head_sha=gate_sha,
            reasons=[
                {
                    "summary": f"platform gate {gate.id} waiting for approval",
                    "env": state.env,
                    "gate_id": gate.id,
                    "sha": gate_sha,
                }
            ],
            now=obs.now,
            source=dec_source,
        )
        actions.append(Ask(decision=dec))


def _apply_rollback_rule(
    state: DeployState,
    cfg_d: dict[str, Any],
    obs: EnvObservation,
    failed_sha: str,
    actions: list[CdAction],
    *,
    dec_source: str | None = None,
    reason: str | None = None,
) -> None:
    """Rule 7: Rollback handling after deploy or verify failure."""
    if obs.known_good is None:
        _notify_once(
            state,
            actions,
            f"no_known_good:{failed_sha}",
            f"{state.project}/{state.env}: rollback unavailable (no known-good deployment)",
        )
        return

    good = obs.known_good
    rb_decisions = _env_decisions(obs, state.project, state.env, kind="rollback")
    if rb_decisions:
        latest = rb_decisions[-1]
        if latest.status == "answered":
            if latest.answer == "rollback":
                state.phase = "rolling_back"
                state.inflight_kind = "rollback"
                actions.append(
                    TriggerRollback(
                        project=state.project,
                        env=state.env,
                        to_id=good.id,
                    )
                )
            elif latest.answer == "skip":
                state.phase = "idle"
                if f"handled:{failed_sha}" not in state.notified:
                    state.notified.append(f"handled:{failed_sha}")
        return

    if cfg_d["rollback"] == "auto":
        rb_auto_key = f"rollback_auto:{failed_sha}:{good.id}"
        if rb_auto_key not in state.notified:
            state.notified.append(rb_auto_key)
            state.phase = "rolling_back"
            state.inflight_kind = "rollback"
            actions.append(
                TriggerRollback(
                    project=state.project,
                    env=state.env,
                    to_id=good.id,
                )
            )
        return

    ask_key = f"ask:rollback:{failed_sha}"
    if ask_key not in state.notified:
        state.notified.append(ask_key)
        short_reason = reason
        if not short_reason and state.verify and state.verify.last_detail:
            short_reason = state.verify.last_detail
        if not short_reason and obs.check and obs.check.detail:
            short_reason = obs.check.detail
        if not short_reason:
            short_reason = "check failed"
        short_reason = short_reason.strip().splitlines()[0].strip()
        if short_reason.endswith(";"):
            short_reason = short_reason[:-1].strip()

        if good and good.sha:
            good_target = f"roll back to {good.sha[:7]}"
        else:
            good_target = "no known-good deployment"
        rollback_summary = f"verify failed on {state.env}: {short_reason}; {good_target}"

        dec = _make_decision(
            kind="rollback",
            state=state,
            head_sha=failed_sha,
            reasons=[
                {
                    "summary": rollback_summary,
                    "env": state.env,
                    "failed_sha": failed_sha,
                    "target_sha": good.sha if good else None,
                    "to_id": good.id if good else None,
                }
            ],
            now=obs.now,
            source=dec_source,
        )
        actions.append(Ask(decision=dec))


def step_env(
    state: DeployState,
    cfg: Mapping[str, Any] | EnvConfig,
    obs: EnvObservation,
    *,
    candidate: str | None,
    project: dict[str, Any],
) -> tuple[DeployState, list[CdAction]]:
    """Pure state machine step for a single environment."""
    new_state = dataclasses.replace(
        state,
        notified=list(state.notified),
        verify=dataclasses.replace(state.verify) if state.verify is not None else None,
    )
    actions: list[CdAction] = []
    cfg_d = _normalize_cfg(cfg, new_state.env)
    branch = _after_branch(project)
    dec_source = _decision_source(project, new_state.env)

    # Legacy placeholder in-flight without dispatch timestamp and unresolvable via obs.existing:
    has_dispatch_time = (
        new_state.waiting_since is not None
        or getattr(new_state, "dispatched_at", None) is not None
        or getattr(new_state, "created_at", None) is not None
    )
    if (
        is_placeholder_id(new_state.inflight_id)
        and not has_dispatch_time
        and (
            obs.existing is None
            or not obs.existing.id
            or is_placeholder_id(obs.existing.id)
        )
    ):
        legacy_sha = new_state.sha
        new_state.inflight_id = None
        new_state.inflight_kind = None
        new_state.phase = "idle"
        new_state.sha = None
        new_state.verify = None
        new_state.waiting_since = None

        to_clear = {legacy_sha, candidate} - {None, ""}
        new_state.notified = [
            k
            for k in new_state.notified
            if not any(
                k == f"ask:deploy:{s}" or k == f"triggered:{s}" for s in to_clear
            )
        ]

        _notify_once(
            new_state,
            actions,
            f"cleared_legacy:{new_state.env}",
            f"cleared legacy in-flight deploy on {new_state.env}; re-evaluating",
        )

    # Rule 5: Always evaluate pending gate / disappeared gate first.
    _handle_gate(new_state, cfg_d, obs, candidate, actions, dec_source=dec_source)

    # A deployment that shows up late resolves a dispatch_timeout stuck without asking.
    if (
        new_state.phase == "stuck"
        and new_state.stuck_reason == "dispatch_timeout"
        and _late_deployment_matches(new_state, obs)
    ):
        new_state.stuck_reason = None
        new_state.phase = (
            "rolling_back" if new_state.inflight_kind == "rollback" else "deploying"
        )
        # Fall through to the normal reconcile path below.

    # Rule 8 (Review Focus 3): Handle stuck state.
    if new_state.phase == "stuck":
        stuck_sha = new_state.sha or candidate or ""
        stuck_reason = new_state.stuck_reason or "rollback_failed"
        stuck_decisions = _env_decisions(
            obs, new_state.project, new_state.env, kind="deploy_stuck"
        )
        if stuck_reason == "dispatch_timeout":
            # Ignore decisions raised for earlier attempts.
            stuck_decisions = [
                d
                for d in stuck_decisions
                if _decision_attempt(d) in (None, new_state.stuck_attempts)
            ]
        if stuck_decisions:
            latest = stuck_decisions[-1]
            if latest.status == "answered":
                if latest.answer == "retry" and stuck_reason == "dispatch_timeout":
                    if new_state.inflight_kind == "rollback":
                        if obs.known_good is not None:
                            new_state.phase = "rolling_back"
                            new_state.stuck_reason = None
                            new_state.waiting_since = obs.now
                            actions.append(
                                TriggerRollback(
                                    project=new_state.project,
                                    env=new_state.env,
                                    to_id=obs.known_good.id,
                                )
                            )
                        else:
                            _notify_once(
                                new_state,
                                actions,
                                f"no_known_good:{stuck_sha}",
                                f"{new_state.project}/{new_state.env}: rollback unavailable (no known-good deployment)",
                            )
                    elif stuck_sha:
                        new_state.phase = "deploying"
                        new_state.inflight_kind = "deploy"
                        new_state.stuck_reason = None
                        new_state.waiting_since = obs.now
                        actions.append(
                            TriggerDeploy(
                                project=new_state.project,
                                env=new_state.env,
                                sha=stuck_sha,
                            )
                        )
                elif latest.answer == "retry":
                    if obs.known_good is not None:
                        new_state.phase = "rolling_back"
                        new_state.inflight_kind = "rollback"
                        actions.append(
                            TriggerRollback(
                                project=new_state.project,
                                env=new_state.env,
                                to_id=obs.known_good.id,
                            )
                        )
                    else:
                        _notify_once(
                            new_state,
                            actions,
                            f"no_known_good:{stuck_sha}",
                            f"{new_state.project}/{new_state.env}: rollback unavailable (no known-good deployment)",
                        )
                elif latest.answer == "skip":
                    new_state.phase = "idle"
                    new_state.inflight_id = None
                    new_state.inflight_kind = None
                    new_state.stuck_reason = None
                    if stuck_reason == "dispatch_timeout":
                        new_state.waiting_since = None
                    if stuck_sha and f"handled:{stuck_sha}" not in new_state.notified:
                        new_state.notified.append(f"handled:{stuck_sha}")
            return new_state, actions

        if stuck_reason == "dispatch_timeout":
            _ask_dispatch_stuck(new_state, obs, actions, stuck_sha)
            return new_state, actions

        ask_stuck_key = f"ask:deploy_stuck:{stuck_sha}"
        if ask_stuck_key not in new_state.notified:
            new_state.notified.append(ask_stuck_key)
            dec = _make_decision(
                kind="deploy_stuck",
                state=new_state,
                head_sha=stuck_sha,
                reasons=[
                    {
                        "summary": f"rollback failed on {new_state.env}, retry?",
                        "env": new_state.env,
                        "sha": stuck_sha,
                        "reason": stuck_reason,
                    }
                ],
                now=obs.now,
            )
            actions.append(Ask(decision=dec))
        return new_state, actions

    # Rule 2, 9, 10 (Review Focus 1): Reconcile in-flight deployment.
    if new_state.inflight_id is not None or new_state.phase in (
        "deploying",
        "rolling_back",
    ):
        eff_env_state: EnvState | None = obs.env_state

        if is_placeholder_id(new_state.inflight_id):
            target_sha = (
                new_state.sha
                or (
                    obs.known_good.sha
                    if new_state.inflight_kind == "rollback" and obs.known_good
                    else None
                )
                or candidate
                or ""
            )
            if (
                obs.existing is not None
                and obs.existing.id
                and not is_placeholder_id(obs.existing.id)
            ):
                new_state.inflight_id = obs.existing.id
                new_state.waiting_since = None
                eff_env_state = EnvState(
                    env=new_state.env,
                    sha=target_sha or obs.existing.sha or None,
                    status=obs.existing.status,
                    deployed_at=obs.existing.created_at,
                    web_url=obs.existing.web_url,
                    deployment_id=obs.existing.id,
                )
            else:
                new_state.phase = (
                    "rolling_back"
                    if new_state.inflight_kind == "rollback"
                    else "deploying"
                )
                if new_state.waiting_since is None:
                    new_state.waiting_since = obs.now
                elif (obs.now - new_state.waiting_since) >= PLATFORM_WAIT_TIMEOUT_SEC:
                    _notify_once(
                        new_state,
                        actions,
                        f"dispatch_timeout:{target_sha or new_state.inflight_id}",
                        f"{new_state.project}/{new_state.env}: deploy dispatched but no deployment appeared",
                    )
                    # Escalate: keep inflight_id/inflight_kind so a late deployment can
                    # still resolve, but surface a decision instead of waiting forever.
                    new_state.phase = "stuck"
                    new_state.stuck_reason = "dispatch_timeout"
                    new_state.stuck_attempts += 1
                    _ask_dispatch_stuck(
                        new_state,
                        obs,
                        actions,
                        new_state.sha or candidate or "",
                    )
                return new_state, actions
        elif (
            obs.existing is not None
            and (
                (new_state.inflight_id and obs.existing.id == new_state.inflight_id)
                or (new_state.sha and _sha_matches(obs.existing.sha, new_state.sha))
            )
            and (
                obs.env_state is None
                or (
                    new_state.inflight_id
                    and obs.env_state.deployment_id != new_state.inflight_id
                )
                or (
                    new_state.sha
                    and obs.env_state.sha
                    and not _sha_matches(obs.env_state.sha, new_state.sha)
                )
            )
        ):
            eff_env_state = EnvState(
                env=new_state.env,
                sha=new_state.sha or obs.existing.sha,
                status=obs.existing.status,
                deployed_at=obs.existing.created_at,
                web_url=obs.existing.web_url,
                deployment_id=obs.existing.id,
            )

        if (
            eff_env_state is None
            or eff_env_state.status
            in (
                "running",
                "pending",
                "waiting_approval",
                "unknown",
                "none",
            )
            or (
                new_state.inflight_kind != "rollback"
                and eff_env_state.sha is not None
                and new_state.sha is not None
                and not _sha_matches(eff_env_state.sha, new_state.sha)
                and (
                    new_state.inflight_id is None
                    or eff_env_state.deployment_id != new_state.inflight_id
                )
            )
        ):
            if (
                new_state.inflight_id is None
                and eff_env_state is not None
                and eff_env_state.deployment_id
                and (
                    not eff_env_state.sha
                    or _sha_matches(eff_env_state.sha, new_state.sha)
                )
            ):
                new_state.inflight_id = eff_env_state.deployment_id
            new_state.phase = (
                "rolling_back" if new_state.inflight_kind == "rollback" else "deploying"
            )
            return new_state, actions

        if eff_env_state.status == "success":
            if new_state.inflight_kind == "rollback":
                rolled_sha = (
                    eff_env_state.sha
                    or (obs.known_good.sha if obs.known_good else None)
                    or new_state.sha
                )
                new_state.inflight_id = None
                new_state.inflight_kind = None
                new_state.waiting_since = None
                new_state.phase = "verified"
                new_state.sha = rolled_sha
                new_state.verified_sha = rolled_sha
                return new_state, actions

            deployed_sha = (
                new_state.sha or eff_env_state.sha or candidate or ""
            )
            new_state.sha = deployed_sha
            new_state.inflight_id = None
            new_state.inflight_kind = None
            new_state.waiting_since = None
            new_state.phase = "verifying"
            new_state.verify = VerifyProgress(sha=deployed_sha, started_at=obs.now)
            actions.append(
                StartVerify(
                    project=new_state.project,
                    env=new_state.env,
                    sha=deployed_sha,
                )
            )
            return new_state, actions

        if eff_env_state.status == "failed":
            failed_sha = new_state.sha or eff_env_state.sha or candidate or ""
            if new_state.inflight_kind == "rollback" or new_state.phase == "rolling_back":
                # Rule 8 (Review Focus 3): Rollback deployment failed -> stuck.
                new_state.inflight_id = None
                new_state.inflight_kind = None
                new_state.waiting_since = None
                new_state.phase = "stuck"
                new_state.stuck_reason = "rollback_failed"
                actions.append(
                    MarkStuck(
                        project=new_state.project,
                        env=new_state.env,
                        reason="rollback deployment failed",
                    )
                )
                ask_stuck_key = f"ask:deploy_stuck:{failed_sha}"
                if ask_stuck_key not in new_state.notified:
                    new_state.notified.append(ask_stuck_key)
                    dec = _make_decision(
                        kind="deploy_stuck",
                        state=new_state,
                        head_sha=failed_sha,
                        reasons=[
                            {
                                "summary": f"rollback failed on {new_state.env}, retry?",
                                "env": new_state.env,
                                "sha": failed_sha,
                                "reason": "rollback_failed",
                            }
                        ],
                        now=obs.now,
                    )
                    actions.append(Ask(decision=dec))
                return new_state, actions

            # Normal deployment failed -> Emit(deploy_failed) + rollback rule.
            new_state.sha = failed_sha
            new_state.inflight_id = None
            new_state.inflight_kind = None
            new_state.waiting_since = None
            new_state.phase = "failed"
            if failed_sha and f"handled:{failed_sha}" not in new_state.notified:
                new_state.notified.append(f"handled:{failed_sha}")

            ev_key = f"emit:deploy_failed:{failed_sha}"
            if ev_key not in new_state.notified:
                new_state.notified.append(ev_key)
                ev = Event(
                    id=store.new_id("ev", now=obs.now),
                    type="deploy_failed",  # type: ignore[arg-type]
                    project=new_state.project,
                    branch=branch,
                    local_path=project.get("local_path"),
                    pr=None,
                    head_sha=failed_sha,
                    payload={
                        "env": new_state.env,
                        "sha": failed_sha,
                        "deployment_id": eff_env_state.deployment_id,
                        "web_url": eff_env_state.web_url,
                    },
                    created_at=obs.now,
                )
                actions.append(Emit(event=ev))

            _apply_rollback_rule(
                new_state, cfg_d, obs, failed_sha, actions, dec_source=dec_source, reason="deploy failed"
            )
            return new_state, actions

    # Rule 6 & Rule 9: Handle ongoing verification (do not cancel if candidate changes).
    if new_state.phase == "verifying" or new_state.verify is not None:
        ver_sha = (
            (new_state.verify.sha if new_state.verify else None)
            or new_state.sha
            or (obs.env_state.sha if obs.env_state else None)
            or candidate
            or ""
        )
        if new_state.verify is None:
            new_state.verify = VerifyProgress(sha=ver_sha, started_at=obs.now)

        verify_cfg = cfg_d.get("verify")
        if obs.check is not None:
            new_state.verify, outcome = verify_mod.advance(
                new_state.verify,
                obs.check,
                now=obs.now,
                cfg=verify_cfg,
            )
        elif not verify_cfg:
            outcome = "passed"
        else:
            outcome = "running"

        if outcome == "running":
            new_state.phase = "verifying"
            return new_state, actions

        if outcome == "passed":
            rec_id = (
                (obs.env_state.deployment_id if obs.env_state and obs.env_state.deployment_id else None)
                or (obs.existing.id if obs.existing and obs.existing.id else None)
                or f"dep-{ver_sha[:7]}"
            )
            rec = DeploymentRecord(
                id=rec_id,
                sha=ver_sha,
                status="success",
                created_at=(
                    obs.env_state.deployed_at
                    if obs.env_state and obs.env_state.deployed_at is not None
                    else obs.now
                ),
                web_url=obs.env_state.web_url if obs.env_state else None,
                run_id=obs.existing.run_id if obs.existing else None,
                job_id=obs.existing.job_id if obs.existing else None,
            )
            new_state.sha = ver_sha
            new_state.verified_sha = ver_sha
            new_state.phase = "verified"
            new_state.verify = None
            actions.append(
                RecordGood(
                    project=new_state.project,
                    env=new_state.env,
                    record=rec,
                )
            )
            return new_state, actions

        # outcome == "failed"
        detail = new_state.verify.last_detail or (obs.check.detail if obs.check else "")
        new_state.sha = ver_sha
        new_state.phase = "failed"
        new_state.verify = None
        if ver_sha and f"handled:{ver_sha}" not in new_state.notified:
            new_state.notified.append(f"handled:{ver_sha}")

        ev_key = f"emit:verify_failed:{ver_sha}"
        if ev_key not in new_state.notified:
            new_state.notified.append(ev_key)
            ev = Event(
                id=store.new_id("ev", now=obs.now),
                type="verify_failed",  # type: ignore[arg-type]
                project=new_state.project,
                branch=branch,
                local_path=project.get("local_path"),
                pr=None,
                head_sha=ver_sha,
                payload={
                    "env": new_state.env,
                    "sha": ver_sha,
                    "detail": detail,
                },
                created_at=obs.now,
            )
            actions.append(Emit(event=ev))

        _apply_rollback_rule(
            new_state, cfg_d, obs, ver_sha, actions, dec_source=dec_source, reason=detail
        )
        return new_state, actions

    # If in failed phase and candidate has not changed, process pending/answered rollback decision.
    if new_state.phase == "failed" and (candidate is None or candidate == new_state.sha):
        _apply_rollback_rule(
            new_state, cfg_d, obs, new_state.sha or "", actions, dec_source=dec_source
        )
        return new_state, actions

    if not candidate:
        return new_state, actions

    # Rule 3: No in-flight, candidate matches verified_sha -> no-op.
    if new_state.verified_sha is not None and candidate == new_state.verified_sha:
        return new_state, actions

    # If this candidate already failed or was skipped, do not re-trigger.
    if f"handled:{candidate}" in new_state.notified:
        return new_state, actions

    # Void stale deploy/rollback decisions if candidate moved to a new SHA.
    stale_decisions = [
        d
        for d in _env_decisions(obs, new_state.project, new_state.env)
        if d.kind in ("deploy", "rollback")
        and d.head_sha
        and d.head_sha != candidate
        and f"triggered:{d.head_sha}" not in new_state.notified
        and f"handled:{d.head_sha}" not in new_state.notified
    ]
    if stale_decisions:
        actions.append(
            VoidDecisions(
                project=new_state.project,
                source=new_state.env,
                reason="head moved",
            )
        )

    # Rule 4: New candidate SHA.
    # 4a. Existing deployment found on platform -> adopt as in-flight without TriggerDeploy.
    # Never adopt the deployment we already verified here: a workflow_dispatch deploy is
    # recorded on the ref HEAD at dispatch time, so its sha can equal a newer candidate.
    already_verified = (
        obs.existing is not None
        and obs.known_good is not None
        and bool(obs.existing.id)
        and obs.existing.id == obs.known_good.id
    )
    if obs.existing is not None and not already_verified and (
        not obs.existing.sha or obs.existing.sha == candidate
    ):
        new_state.sha = candidate
        new_state.inflight_id = obs.existing.id
        new_state.inflight_kind = "deploy"
        new_state.phase = "deploying"
        new_state.waiting_since = None
        return new_state, actions

    # 4b. managed_by == "pd"
    if cfg_d["managed_by"] == "pd":
        deploy_decisions = [
            d
            for d in _env_decisions(obs, new_state.project, new_state.env, kind="deploy")
            if not d.head_sha or d.head_sha == candidate
        ]
        if deploy_decisions:
            latest = deploy_decisions[-1]
            if latest.status == "pending":
                new_state.phase = "idle"
            elif latest.status == "answered":
                if latest.answer == "deploy":
                    trig_key = f"triggered:{candidate}"
                    if trig_key not in new_state.notified:
                        new_state.notified.append(trig_key)
                        new_state.sha = candidate
                        new_state.phase = "deploying"
                        new_state.inflight_kind = "deploy"
                        actions.append(
                            TriggerDeploy(
                                project=new_state.project,
                                env=new_state.env,
                                sha=candidate,
                            )
                        )
                elif latest.answer == "skip":
                    new_state.sha = candidate
                    new_state.phase = "idle"
                    if f"handled:{candidate}" not in new_state.notified:
                        new_state.notified.append(f"handled:{candidate}")
            return new_state, actions

        if cfg_d["trigger"] == "auto":
            trig_key = f"triggered:{candidate}"
            if trig_key not in new_state.notified:
                new_state.notified.append(trig_key)
                new_state.sha = candidate
                new_state.phase = "deploying"
                new_state.inflight_kind = "deploy"
                actions.append(
                    TriggerDeploy(
                        project=new_state.project,
                        env=new_state.env,
                        sha=candidate,
                    )
                )
            return new_state, actions

        ask_key = f"ask:deploy:{candidate}"
        if ask_key not in new_state.notified:
            new_state.notified.append(ask_key)
            new_state.phase = "idle"
            envs = (
                project.get("deploy", {}).get("environments", [])
                if isinstance(project, Mapping) and isinstance(project.get("deploy"), Mapping)
                else []
            )
            env_names = [
                e.get("name") if isinstance(e, Mapping) else getattr(e, "name", None)
                for e in envs
            ]
            is_first_env = (bool(env_names) and env_names[0] == new_state.env) or (dec_source == branch)
            if is_first_env:
                deploy_summary = f"CI green on {branch} at {candidate[:7]}"
            else:
                deploy_summary = f"{dec_source} verified {candidate[:7]}"
            dec = _make_decision(
                kind="deploy",
                state=new_state,
                head_sha=candidate,
                reasons=[
                    {
                        "summary": deploy_summary,
                        "env": new_state.env,
                        "sha": candidate,
                    }
                ],
                now=obs.now,
                source=dec_source,
            )
            actions.append(Ask(decision=dec))
        return new_state, actions

    # 4c. managed_by == "platform": wait up to 900s, then Notify once.
    if new_state.sha != candidate or new_state.waiting_since is None:
        new_state.sha = candidate
        new_state.phase = "idle"
        new_state.waiting_since = obs.now
        return new_state, actions

    if (obs.now - new_state.waiting_since) >= PLATFORM_WAIT_TIMEOUT_SEC:
        _notify_once(
            new_state,
            actions,
            f"platform_timeout:{candidate}",
            f"{new_state.project}/{new_state.env}: platform did not deploy {candidate[:7]}",
        )

    return new_state, actions
