"""Data types for continuous deployment (environment chain after the git flow).

Contract shared by config / deploy_adapters / verify / deploystore / cdflow / cd_exec.
Stdlib only. See docs/superpowers/specs/2026-11-08-continuous-deployment-design.md.

New decision kinds (stored with flowmodel.Decision): "deploy", "deploy_approval",
"rollback", "deploy_stuck". New event types (flowmodel.Event): "deploy_failed",
"verify_failed".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

EnvStatus = Literal["success", "failed", "running", "pending", "waiting_approval", "none", "unknown"]


@dataclass
class EnvConfig:
    name: str                                   # pd-facing name, e.g. "staging"
    adapter: str                                # "github" | "gitlab" | "command" | custom
    environment: str | None = None              # platform environment name (defaults to name)
    production: bool = False
    managed_by: Literal["platform", "pd"] = "platform"
    trigger: Literal["auto", "ask"] = "auto"
    rollback: Literal["auto", "ask"] = "auto"
    verify: dict[str, Any] | None = None        # url/expect_status/command/metric/duration_sec/interval_sec/fail_after
    github: dict[str, Any] | None = None        # {"workflow", "ref", "inputs"}
    gitlab: dict[str, Any] | None = None        # {"job"}
    command: dict[str, Any] | None = None       # {"status", "deploy", "rollback", "history", "parse", "timeout_sec"}


@dataclass
class DeploymentRecord:
    id: str                                     # platform deployment id (or command-provided id)
    sha: str
    status: EnvStatus
    created_at: float | None = None
    web_url: str | None = None
    run_id: str | None = None                   # GitHub workflow run that performed it (for rerun rollback)
    job_id: str | None = None                   # GitLab job that performed it (for retry rollback)


@dataclass
class EnvState:
    env: str
    sha: str | None
    status: EnvStatus
    deployed_at: float | None = None
    web_url: str | None = None
    deployment_id: str | None = None


@dataclass
class PendingGate:
    id: str                                     # GitHub: run_id ; GitLab: deployment id
    env: str
    sha: str
    environment_ids: list[int] = field(default_factory=list)  # GitHub pending_deployments env ids
    web_url: str | None = None


@dataclass
class CheckResult:
    ok: bool
    detail: str = ""


@dataclass
class VerifyProgress:
    sha: str
    started_at: float
    last_check_at: float | None = None
    consecutive_failures: int = 0
    checks: int = 0
    last_detail: str = ""


@dataclass
class DeployState:
    """Persisted per (project, env) in <state_dir>/deploys.json."""

    project: str
    env: str
    sha: str | None = None                      # sha currently being handled
    inflight_id: str | None = None              # deployment id being deployed / rolled back
    inflight_kind: Literal["deploy", "rollback"] | None = None
    phase: Literal["idle", "deploying", "verifying", "verified", "failed", "rolling_back", "stuck"] = "idle"
    verified_sha: str | None = None             # last sha that passed verification here
    verify: VerifyProgress | None = None
    waiting_since: float | None = None          # platform-managed: when we started waiting for a deployment
    notified: list[str] = field(default_factory=list)
    stuck_reason: str | None = None             # "dispatch_timeout" | "rollback_failed" while phase == "stuck"
    stuck_attempts: int = 0                     # number of dispatch_timeout stuck decisions raised so far


# ---- CD actions returned by cdflow.step_env (executed by cd_exec.apply) ----

@dataclass
class TriggerDeploy:
    project: str
    env: str
    sha: str


@dataclass
class ApproveGate:
    project: str
    env: str
    gate_id: str
    decision_id: str | None = None


@dataclass
class RejectGate:
    project: str
    env: str
    gate_id: str
    decision_id: str | None = None


@dataclass
class TriggerRollback:
    project: str
    env: str
    to_id: str                                  # DeploymentRecord.id of known-good


@dataclass
class StartVerify:
    project: str
    env: str
    sha: str


@dataclass
class RecordGood:
    project: str
    env: str
    record: DeploymentRecord


@dataclass
class MarkStuck:
    project: str
    env: str
    reason: str


def state_key(project: str, env: str) -> str:
    return f"{project}/{env}"
