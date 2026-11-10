"""Yellow-light rules storage and specificity matcher.

Rules live in ~/.config/pipedog/rules.json (or alongside PIPEDOG_CONFIG).
Stdlib only.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import flowmodel
import store

_REQUIRED_RULE_KEYS: tuple[str, ...] = (
    "id",
    "action",
    "project",
    "transition",
    "job",
    "kind",
    "created_at",
)


def rules_path(
    env: Mapping[str, str] | None = None,
    *,
    mock: bool = False,
    root: Path | None = None,
) -> Path:
    """Return rules.json path next to PIPEDOG_CONFIG, or ~/.config/pipedog/rules.json.

    If mock is True, returns <state_dir>/mock-rules.json.
    """
    if mock:
        if root is not None:
            return Path(root) / "mock-rules.json"
        cfg_env = os.environ if env is None else env
        s_dir = cfg_env.get("PIPEDOG_STATE_DIR")
        if s_dir:
            return Path(s_dir).expanduser() / "mock-rules.json"
        return Path.home() / ".local" / "state" / "pipedog" / "mock-rules.json"

    env = os.environ if env is None else env
    cfg = env.get("PIPEDOG_CONFIG")
    if cfg:
        return Path(cfg).expanduser().parent / "rules.json"
    return Path.home() / ".config" / "pipedog" / "rules.json"


def _atomic_write(path: Path, data: Any) -> None:
    """Write JSON atomically: tempfile in same directory -> flush -> fsync -> replace."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
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


def load(path: Path | None = None) -> tuple[list[flowmodel.Rule], str | None]:
    """Load rules from path.

    Returns ([], None) if the file does not exist, or
    ([], "rules.json invalid: <reason>") if the file is unreadable or malformed.
    Never raises on invalid content.
    """
    target = Path(path) if path is not None else rules_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        return [], f"rules.json invalid: {reason}"

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        return [], f"rules.json invalid: line {exc.lineno} column {exc.colno}"

    if isinstance(raw, dict) and isinstance(raw.get("rules"), list):
        items = raw["rules"]
    elif isinstance(raw, list):
        items = raw
    else:
        return [], "rules.json invalid: root must be a JSON array"

    out: list[flowmodel.Rule] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            return [], f"rules.json invalid: entry {idx} is not an object"
        missing = [k for k in _REQUIRED_RULE_KEYS if k not in item]
        if missing:
            return [], f"rules.json invalid: entry {idx} missing {', '.join(missing)}"
        if item.get("action") not in ("allow", "ask"):
            return [], f"rules.json invalid: entry {idx} has invalid action"
        try:
            out.append(flowmodel.from_json(flowmodel.Rule, item))
        except (TypeError, ValueError) as exc:
            return [], f"rules.json invalid: entry {idx} ({exc})"
    return out, None


def save(path: Path, rules: list[flowmodel.Rule]) -> None:
    """Atomically save rules to path as a JSON list."""
    _atomic_write(Path(path), [flowmodel.to_json(r) for r in rules])


def _specificity(rule: flowmodel.Rule) -> int:
    """Count non-'*' match fields (a glob like 'lint*' counts as specific)."""
    return sum(
        1
        for val in (rule.project, rule.transition, rule.job, rule.kind)
        if val != "*"
    )


def _created_sort_key(created_at: str) -> tuple[int, float | str]:
    """Parse ISO 8601 timestamp when possible; fall back to raw string comparison."""
    try:
        dt = datetime.fromisoformat(created_at)
        return (1, dt.timestamp())
    except ValueError:
        return (0, created_at)


def _field_matches(pattern: str, value: str) -> bool:
    if pattern == "*":
        return True
    return fnmatch.fnmatchcase(value, pattern)


def match(
    rules: list[flowmodel.Rule],
    *,
    project: str,
    transition: str,
    job: str,
    kind: str,
) -> flowmodel.Rule | None:
    """Return the most specific matching rule (ties broken by newer created_at)."""
    candidates: list[tuple[int, tuple[int, float | str], int, flowmodel.Rule]] = []
    for idx, rule in enumerate(rules):
        if not _field_matches(rule.project, project):
            continue
        if not _field_matches(rule.transition, transition):
            continue
        if not _field_matches(rule.job, job):
            continue
        if not _field_matches(rule.kind, kind):
            continue
        candidates.append((_specificity(rule), _created_sort_key(rule.created_at), idx, rule))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return candidates[-1][3]


def _next_id(existing: list[flowmodel.Rule]) -> str:
    max_num = 0
    for r in existing:
        m = re.fullmatch(r"r(\d+)", r.id)
        if m:
            max_num = max(max_num, int(m.group(1)))
    return f"r{max_num + 1}"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def add(path: Path, rule_fields: Mapping[str, Any]) -> flowmodel.Rule:
    """Add a new rule with generated id ('r<n>') and local ISO created_at, then save."""
    target = Path(path)
    with store.state_lock(target.name, root=target.parent):
        return _add_locked(target, rule_fields)


def _add_locked(target: Path, rule_fields: Mapping[str, Any]) -> flowmodel.Rule:
    existing, _ = load(target)
    rid = str(rule_fields.get("id") or _next_id(existing))
    created_at = str(rule_fields.get("created_at") or _now_iso())
    rule = flowmodel.Rule(
        id=rid,
        action=rule_fields.get("action", "allow"),
        project=str(rule_fields.get("project", "*")),
        transition=str(rule_fields.get("transition", "*")),
        job=str(rule_fields.get("job", "*")),
        kind=str(rule_fields.get("kind", "*")),
        created_at=created_at,
        source_run=rule_fields.get("source_run"),
    )
    existing.append(rule)
    save(target, existing)
    return rule
