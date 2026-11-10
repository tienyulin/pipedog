"""State store for deployment states and deployment history.

Persists:
- <state_dir>/deploys.json: per-(project, env) DeployState
- <state_dir>/deploy_history.json: per-(project, env) known-good DeploymentRecord list (capped at 20)

Atomic writes use store._atomic_write.
Stdlib only.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from deploymodel import DeployState, DeploymentRecord, VerifyProgress, state_key
import snapshot
from store import _atomic_write, state_lock

HISTORY_CAP = 20


def _root(root: Path | None = None) -> Path:
    return Path(root) if root is not None else snapshot.state_dir()


def _deploys_path(root: Path | None = None) -> Path:
    return _root(root) / "deploys.json"


def _history_path(root: Path | None = None) -> Path:
    return _root(root) / "deploy_history.json"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_verify(raw: Any) -> VerifyProgress | None:
    if isinstance(raw, VerifyProgress):
        return raw
    if not isinstance(raw, dict):
        return None
    valid_fields = {f.name for f in dataclasses.fields(VerifyProgress)}
    return VerifyProgress(**{k: v for k, v in raw.items() if k in valid_fields})


def _parse_deploy_state(raw: Any) -> DeployState | None:
    if isinstance(raw, DeployState):
        return raw
    if not isinstance(raw, dict):
        return None
    data = dict(raw)
    if "verify" in data:
        data["verify"] = _parse_verify(data["verify"])
    valid_fields = {f.name for f in dataclasses.fields(DeployState)}
    return DeployState(**{k: v for k, v in data.items() if k in valid_fields})


def _parse_deployment_record(raw: Any) -> DeploymentRecord | None:
    if isinstance(raw, DeploymentRecord):
        return raw
    if not isinstance(raw, dict):
        return None
    valid_fields = {f.name for f in dataclasses.fields(DeploymentRecord)}
    return DeploymentRecord(**{k: v for k, v in raw.items() if k in valid_fields})


# ---------------------------------------------------------------------------
# Deploy states (deploys.json)
# ---------------------------------------------------------------------------


def load_states(root: Path | None = None) -> dict[str, DeployState]:
    """Load all DeployState mappings, keyed by '<project>/<env>'."""
    p = _deploys_path(root)
    try:
        raw = _read_json(p)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    states: dict[str, DeployState] = {}
    for k, v in raw.items():
        st = _parse_deploy_state(v)
        if st is not None:
            states[k] = st
    return states


def get_state(project: str, env: str, root: Path | None = None) -> DeployState | None:
    """Return DeployState for (project, env), or None if not found."""
    return load_states(root=root).get(state_key(project, env))


def save_state(state: DeployState, root: Path | None = None) -> None:
    """Atomically persist a DeployState for (state.project, state.env)."""
    key = state_key(state.project, state.env)

    def _set(states: dict[str, DeployState]) -> dict[str, DeployState]:
        states[key] = state
        return states

    update_state(_set, root=root)


def update_state(
    fn: Callable[[dict[str, DeployState]], dict[str, DeployState] | None],
    root: Path | None = None,
) -> None:
    """Load all deploy states, apply fn, save result under one lock. None skips the save."""
    with state_lock("deploys.json", root=root):
        result = fn(load_states(root=root))
        if result is not None:
            data = {k: dataclasses.asdict(v) for k, v in sorted(result.items())}
            _atomic_write(_deploys_path(root), data)


# ---------------------------------------------------------------------------
# Deployment history / known-good (deploy_history.json)
# ---------------------------------------------------------------------------


def _load_history_dict(root: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    p = _history_path(root)
    try:
        raw = _read_json(p)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw


def history(project: str, env: str, root: Path | None = None) -> list[DeploymentRecord]:
    """Return deployment history for (project, env), newest first."""
    raw_dict = _load_history_dict(root=root)
    key = state_key(project, env)
    raw_list = raw_dict.get(key, [])
    if not isinstance(raw_list, list):
        return []
    records: list[DeploymentRecord] = []
    for item in raw_list:
        rec = _parse_deployment_record(item)
        if rec is not None:
            records.append(rec)
    return records


def known_good(project: str, env: str, root: Path | None = None) -> DeploymentRecord | None:
    """Return the most recent known-good DeploymentRecord, or None."""
    records = history(project, env, root=root)
    return records[0] if records else None


def record_good(
    project: str, env: str, rec: DeploymentRecord, root: Path | None = None
) -> None:
    """Record a verified deployment for (project, env), retaining the latest 20."""
    raw_dict = _load_history_dict(root=root)
    key = state_key(project, env)
    current_recs = history(project, env, root=root)
    filtered = [r for r in current_recs if r.id != rec.id]
    new_recs = [rec, *filtered][:HISTORY_CAP]
    raw_dict[key] = [dataclasses.asdict(r) for r in new_recs]
    _atomic_write(_history_path(root), raw_dict)
