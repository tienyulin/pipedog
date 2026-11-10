"""active_status.json snapshot for agents (docs/DESIGN.md section 7).

The snapshot is the only interface between the running monitor and an agent
reading it via SKILL.md. It is written atomically so readers never observe a
half-written file.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import flowmodel
import model

SCHEMA_VERSION = 2
FILENAME = "active_status.json"


def state_dir() -> Path:
    """PIPEDOG_STATE_DIR or ~/.local/state/pipedog."""
    env = os.environ.get("PIPEDOG_STATE_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "state" / "pipedog"


def default_path() -> Path:
    return state_dir() / FILENAME


def stale_after(poll_interval: float) -> float:
    return max(60.0, float(poll_interval) * 6)


def _iso_local(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _num(x: float) -> int | float:
    """Render whole floats as ints in JSON (5.0 -> 5)."""
    return int(x) if float(x).is_integer() else float(x)


def _failed_entry(p: model.Pipeline, leaf: model.Job, parent: str | None) -> dict[str, Any]:
    err = leaf.error
    return {
        "repo": p.project,
        "provider": p.provider,
        "local_path": p.local_path,
        "branch": p.branch,
        "pipeline_id": p.pipeline_id,
        "web_url": p.web_url,
        "job_name": leaf.name,
        "parent_job": parent,
        "job_id": leaf.job_id,
        "job_web_url": leaf.web_url,
        "file": err.file if err else None,
        "line": err.line if err else None,
        "error_summary": err.error_summary if err else None,
        "log_tail": err.log_tail if err else "",
    }


def failed_leaves(p: model.Pipeline) -> list[tuple[model.Job, str | None]]:
    """(failed leaf, parent group name or None) for every non-allow_failure failed leaf."""
    out: list[tuple[model.Job, str | None]] = []
    for job in p.jobs:
        if job.sub_jobs:
            out.extend(
                (leaf, job.name)
                for leaf in job.sub_jobs
                if leaf.status == "failed" and not leaf.allow_failure
            )
        elif job.status == "failed" and not job.allow_failure:
            out.append((job, None))
    return out


def yellow_leaves(p: model.Pipeline) -> list[tuple[model.Job, str | None]]:
    """(leaf, parent group name or None) for every yellow-light leaf."""
    out: list[tuple[model.Job, str | None]] = []
    for job in p.jobs:
        if job.sub_jobs:
            for leaf in job.sub_jobs:
                raw = (leaf.raw_status or "").lower()
                if (leaf.status == "failed" and leaf.allow_failure) or raw in ("neutral", "action_required"):
                    out.append((leaf, job.name))
        else:
            raw = (job.raw_status or "").lower()
            if (job.status == "failed" and job.allow_failure) or raw in ("neutral", "action_required"):
                out.append((job, None))
    return out


def pipeline_has_failure(p: model.Pipeline) -> bool:
    """A pipeline counts as failing if it has any non-allow_failure failed leaf (or failed with no jobs).

    fetch errors and allow_failure jobs (yellow light) do not count as failures.
    """
    if p.fetch_error:
        return False
    leaves = failed_leaves(p)
    if leaves:
        return True
    if p.jobs:
        return False
    return p.status == "failed"


def _format_tracked_ref(raw_ref: Any) -> dict[str, Any] | None:
    if raw_ref is None:
        return None
    if isinstance(raw_ref, flowmodel.TrackedRef):
        return {
            "branch": raw_ref.branch,
            "target": raw_ref.target,
            "pr": raw_ref.pr,
            "owned": bool(raw_ref.owned),
        }
    if isinstance(raw_ref, dict):
        return {
            "branch": raw_ref.get("branch"),
            "target": raw_ref.get("target"),
            "pr": raw_ref.get("pr"),
            "owned": bool(raw_ref.get("owned", False)),
        }
    return None


def _pipeline_dict(p: model.Pipeline) -> dict[str, Any]:
    d = p.to_dict()
    d["tracked_ref"] = _format_tracked_ref(getattr(p, "tracked_ref", None))
    return d


def _format_environment(raw_env: Any) -> dict[str, Any]:
    if dataclasses.is_dataclass(raw_env) and not isinstance(raw_env, type):
        d = dataclasses.asdict(raw_env)
    elif isinstance(raw_env, dict):
        d = dict(raw_env)
    else:
        d = {}
    out = {
        "project": d.get("project"),
        "env": d.get("env"),
        "sha": d.get("sha"),
        "status": d.get("status", "none"),
        "verified": bool(d.get("verified", False)),
        "verified_sha": d.get("verified_sha"),
        "deployed_at": d.get("deployed_at"),
        "web_url": d.get("web_url"),
        "production": bool(d.get("production", False)),
    }
    if "phase" in d:
        out["phase"] = d["phase"]
    return out


def build(
    pipelines: list[model.Pipeline],
    *,
    now: float,
    poll_interval: float,
    mock: bool,
    decisions_pending: Sequence[flowmodel.Decision | dict[str, Any]] = (),
    events_unacked: int = 0,
    environments: Sequence[dict[str, Any] | Any] = (),
    prs: Mapping[Any, Any] | None = None,
) -> dict[str, Any]:
    failed_jobs = [
        _failed_entry(p, leaf, parent)
        for p in pipelines
        if not p.fetch_error
        for leaf, parent in failed_leaves(p)
    ]
    serialized_decisions = [
        flowmodel.to_json(d) if dataclasses.is_dataclass(d) else dict(d)
        for d in decisions_pending
    ]
    serialized_environments = [_format_environment(e) for e in environments]
    serialized_prs: dict[str, Any] = {}
    if prs:
        if isinstance(prs, dict):
            for k, v in prs.items():
                if isinstance(k, tuple) and len(k) == 2:
                    k_str = f"{k[0]}:{k[1]}"
                else:
                    k_str = str(k)
                serialized_prs[k_str] = (
                    flowmodel.to_json(v) if dataclasses.is_dataclass(v) else dict(v)
                )
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _iso_local(now),
        "monitor_pid": os.getpid(),
        "poll_interval_sec": _num(poll_interval),
        "stale_after_sec": _num(stale_after(poll_interval)),
        "mock": bool(mock),
        "has_failure": any(pipeline_has_failure(p) for p in pipelines),
        "failed_jobs": failed_jobs,
        "decisions_pending": serialized_decisions,
        "events_unacked": int(events_unacked),
        "pipelines": [_pipeline_dict(p) for p in pipelines],
        "environments": serialized_environments,
        "prs": serialized_prs,
    }


def write(data: dict[str, Any], path: Path | None = None) -> Path:
    """Atomic write: tempfile in same dir -> flush -> fsync -> os.replace."""
    target = Path(path) if path is not None else default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target
