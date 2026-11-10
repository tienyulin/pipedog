"""File-backed state store for flow decisions, events, owned refs, comments, and locks.

All writes are atomic (same-directory tempfile -> flush -> fsync -> replace).
Stdlib only.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

try:  # POSIX only; on Windows state_lock becomes a no-op
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

import flowmodel
import snapshot


class StoreError(Exception):
    """Invalid state store operation."""


def _root(root: Path | None = None) -> Path:
    return Path(root) if root is not None else snapshot.state_dir()


LOCK_TIMEOUT = 10.0
LOCK_POLL = 0.05


@contextmanager
def state_lock(name: str, root: Path | None = None) -> Iterator[None]:
    """Exclusive inter-process lock on <state_dir>/locks/<name>.lock (flock).

    Blocks up to LOCK_TIMEOUT seconds, then raises StoreError. No-op without fcntl.
    Not re-entrant: do not nest the same name.
    """
    if fcntl is None:
        yield
        return
    lock_dir = _root(root) / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_dir / f"{name}.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise StoreError(f"timed out waiting for lock {name}") from None
                time.sleep(LOCK_POLL)
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)


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


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _iso_local(ts: float | None = None) -> str:
    now = time.time() if ts is None else ts
    return datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds")


def new_id(prefix: str, *, now: float | None = None) -> str:
    """Generate '<prefix>-<YYYYmmddHHMMSS>-<4 hex>'."""
    ts = time.time() if now is None else now
    stamp = datetime.fromtimestamp(ts).astimezone().strftime("%Y%m%d%H%M%S")
    return f"{prefix}-{stamp}-{secrets.token_hex(2)}"


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def _decisions_dir(root: Path | None = None) -> Path:
    return _root(root) / "decisions"


def _decision_path(decision_id: str, root: Path | None = None) -> Path:
    return _decisions_dir(root) / f"{decision_id}.json"


def put_decision(d: flowmodel.Decision, root: Path | None = None) -> None:
    """Atomically write a decision file."""
    _atomic_write(_decision_path(d.id, root), flowmodel.to_json(d))


def get_decision(decision_id: str, root: Path | None = None) -> flowmodel.Decision | None:
    """Return a decision by id, or None if not found or unreadable."""
    p = _decision_path(decision_id, root)
    try:
        raw = _read_json(p)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return flowmodel.from_json(flowmodel.Decision, raw)
    except (TypeError, ValueError):
        return None


def list_decisions(
    status: str | None = None,
    root: Path | None = None,
) -> list[flowmodel.Decision]:
    """List decisions sorted by created_at (and id), optionally filtered by status."""
    ddir = _decisions_dir(root)
    if not ddir.is_dir():
        return []
    out: list[flowmodel.Decision] = []
    for p in ddir.iterdir():
        if p.name.startswith(".") or p.suffix != ".json":
            continue
        try:
            raw = _read_json(p)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        try:
            d = flowmodel.from_json(flowmodel.Decision, raw)
        except (TypeError, ValueError):
            continue
        if status is not None and d.status != status:
            continue
        out.append(d)
    out.sort(key=lambda item: (item.created_at, item.id))
    return out


def answer_decision(
    decision_id: str,
    answer: str,
    remember: bool = False,
    root: Path | None = None,
    answered_by: str = "cli",
    now: float | None = None,
) -> flowmodel.Decision:
    """Record user's answer on a pending decision; raise StoreError if not pending."""
    with state_lock(f"decision-{decision_id}", root=root):
        d = get_decision(decision_id, root=root)
        if d is None:
            raise StoreError(f"decision {decision_id} not found")
        if d.status != "pending":
            raise StoreError(f"decision {decision_id} is {d.status}")
        d.status = "answered"
        d.answer = answer
        d.remember = bool(remember)
        d.answered_by = answered_by
        d.answered_at = time.time() if now is None else float(now)
        put_decision(d, root=root)
        return d


def list_decision_history(
    project: str | None = None,
    limit: int = 20,
    root: Path | None = None,
) -> list[flowmodel.Decision]:
    """List non-pending decisions newest first by answered_at (fallback to created_at)."""
    decisions = [d for d in list_decisions(root=root) if d.status != "pending"]
    if project:
        decisions = [d for d in decisions if d.project == project]
    decisions.sort(
        key=lambda d: (
            d.answered_at if d.answered_at is not None else d.created_at,
            d.created_at,
            d.id,
        ),
        reverse=True,
    )
    if limit is not None:
        decisions = decisions[: max(0, limit)]
    return decisions


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def _events_dir(root: Path | None = None) -> Path:
    return _root(root) / "events"


def _event_path(event_id: str, root: Path | None = None) -> Path:
    return _events_dir(root) / f"{event_id}.json"


def put_event(e: flowmodel.Event, root: Path | None = None) -> None:
    """Atomically write an event file."""
    _atomic_write(_event_path(e.id, root), flowmodel.to_json(e))


def get_event(event_id: str, root: Path | None = None) -> flowmodel.Event | None:
    """Return an event by id, or None if not found."""
    p = _event_path(event_id, root)
    try:
        raw = _read_json(p)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return flowmodel.from_json(flowmodel.Event, raw)
    except (TypeError, ValueError):
        return None


def list_events(
    unacked_only: bool = False,
    root: Path | None = None,
) -> list[flowmodel.Event]:
    """List events sorted by created_at (and id)."""
    edir = _events_dir(root)
    if not edir.is_dir():
        return []
    out: list[flowmodel.Event] = []
    for p in edir.iterdir():
        if p.name.startswith(".") or p.suffix != ".json":
            continue
        try:
            raw = _read_json(p)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        try:
            e = flowmodel.from_json(flowmodel.Event, raw)
        except (TypeError, ValueError):
            continue
        if unacked_only and e.acked_at is not None:
            continue
        out.append(e)
    out.sort(key=lambda item: (item.created_at, item.id))
    return out


def oldest_unacked(root: Path | None = None) -> flowmodel.Event | None:
    """Return the oldest unacknowledged event, or None."""
    unacked = list_events(unacked_only=True, root=root)
    return unacked[0] if unacked else None


def ack_event(
    event_id: str,
    *,
    handled: list[str] | None = None,
    pushed: str | None = None,
    root: Path | None = None,
    now: float | None = None,
) -> flowmodel.Event:
    """Mark an event as acknowledged and record handled comment ids as seen."""
    e = get_event(event_id, root=root)
    if e is None:
        raise StoreError(f"event {event_id} not found")
    e.acked_at = time.time() if now is None else float(now)
    payload = dict(e.payload) if isinstance(e.payload, dict) else {}
    if pushed is not None:
        payload["pushed"] = pushed
    if handled:
        payload["handled"] = list(handled)
        if e.pr is not None:
            mark_seen(e.project, e.pr, list(handled), root=root)
    e.payload = payload
    put_event(e, root=root)
    return e


# ---------------------------------------------------------------------------
# Owned refs
# ---------------------------------------------------------------------------


def _owned_path(root: Path | None = None) -> Path:
    return _root(root) / "owned.json"


def load_owned(root: Path | None = None) -> list[flowmodel.TrackedRef]:
    """Load owned TrackedRefs from owned.json."""
    p = _owned_path(root)
    try:
        raw = _read_json(p)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    out: list[flowmodel.TrackedRef] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            out.append(flowmodel.from_json(flowmodel.TrackedRef, item))
        except (TypeError, ValueError):
            continue
    return out


def save_owned(refs: Iterable[flowmodel.TrackedRef], root: Path | None = None) -> None:
    """Atomically save owned TrackedRefs to owned.json."""
    _atomic_write(_owned_path(root), [flowmodel.to_json(r) for r in refs])


def update_owned(
    fn: Callable[[list[flowmodel.TrackedRef]], Iterable[flowmodel.TrackedRef] | None],
    root: Path | None = None,
) -> None:
    """Load owned refs, apply fn, save result under one lock. fn returning None skips the save."""
    with state_lock("owned.json", root=root):
        result = fn(load_owned(root=root))
        if result is not None:
            save_owned(result, root=root)


# ---------------------------------------------------------------------------
# Comments seen
# ---------------------------------------------------------------------------


def _comments_seen_path(root: Path | None = None) -> Path:
    return _root(root) / "comments_seen.json"


def _comment_key(project: str, pr: int) -> str:
    return f"{project}#{pr}"


def _load_comments_map(root: Path | None = None) -> dict[str, list[str]]:
    p = _comments_seen_path(root)
    try:
        raw = _read_json(p)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, list):
            out[k] = [str(x) for x in v]
    return out


def seen_comments(project: str, pr: int, root: Path | None = None) -> set[str]:
    """Return the set of comment ids already handled for (project, pr)."""
    data = _load_comments_map(root)
    return set(data.get(_comment_key(project, pr), []))


def mark_seen(
    project: str,
    pr: int,
    ids: Iterable[str],
    root: Path | None = None,
) -> None:
    """Mark comment ids as seen for (project, pr)."""
    id_list = [str(cid) for cid in ids]
    with state_lock("comments_seen.json", root=root):
        data = _load_comments_map(root)
        key = _comment_key(project, pr)
        merged = set(data.get(key, []))
        merged.update(id_list)
        data[key] = sorted(merged)
        _atomic_write(_comments_seen_path(root), data)


# ---------------------------------------------------------------------------
# Wait lock
# ---------------------------------------------------------------------------


def _wait_lock_path(root: Path | None = None) -> Path:
    return _events_dir(root) / ".lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _read_lock_pid(lock_path: Path) -> int | None:
    try:
        text = lock_path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and isinstance(parsed.get("pid"), int):
            return parsed["pid"]
        if isinstance(parsed, int):
            return parsed
    except json.JSONDecodeError:
        pass
    try:
        return int(text)
    except ValueError:
        return None


def has_wait_lock(root: Path | None = None) -> bool:
    """Return True if an active wait lock held by a live process exists."""
    pid = _read_lock_pid(_wait_lock_path(root))
    return pid is not None and _pid_alive(pid)


def acquire_wait_lock(root: Path | None = None, *, pid: int | None = None) -> bool:
    """Acquire exclusive wait lock in events/.lock; return False if held by live pid."""
    lock_path = _wait_lock_path(root)
    with state_lock("wait", root=root):
        existing_pid = _read_lock_pid(lock_path)
        if existing_pid is not None and _pid_alive(existing_pid):
            return False
        my_pid = os.getpid() if pid is None else int(pid)
        _atomic_write(lock_path, {"pid": my_pid, "created_at": time.time()})
        return True


def release_wait_lock(root: Path | None = None) -> None:
    """Release events/.lock if present."""
    lock_path = _wait_lock_path(root)
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Engine lock
# ---------------------------------------------------------------------------


def _engine_lock_path(root: Path | None = None) -> Path:
    return _root(root) / "engine.lock"


def _read_engine_lock(lock_path: Path) -> tuple[int | None, float | None]:
    try:
        text = lock_path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None, None
    if not text:
        return None, None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    raw_pid = parsed.get("pid")
    pid = raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) else None
    raw_ts = parsed.get("updated_at")
    if raw_ts is None:
        raw_ts = parsed.get("ts", parsed.get("created_at"))
    ts: float | None = None
    if isinstance(raw_ts, (int, float)) and not isinstance(raw_ts, bool):
        ts = float(raw_ts)
    elif isinstance(raw_ts, str):
        try:
            ts = datetime.fromisoformat(raw_ts).timestamp()
        except ValueError:
            try:
                ts = float(raw_ts)
            except ValueError:
                ts = None
    return pid, ts


def acquire_engine_lock(
    stale_after: float,
    root: Path | None = None,
    *,
    pid: int | None = None,
    now: float | None = None,
) -> bool:
    """Acquire engine.lock if unheld, held by dead pid, or older than stale_after."""
    lock_path = _engine_lock_path(root)
    current = time.time() if now is None else float(now)
    with state_lock("engine", root=root):
        existing_pid, updated_at = _read_engine_lock(lock_path)
        if existing_pid is not None and _pid_alive(existing_pid):
            if updated_at is not None and (current - updated_at) <= float(stale_after):
                return False
        my_pid = os.getpid() if pid is None else int(pid)
        _atomic_write(lock_path, {"pid": my_pid, "updated_at": current})
        return True


def refresh_engine_lock(
    root: Path | None = None,
    *,
    pid: int | None = None,
    now: float | None = None,
) -> bool:
    """Refresh engine.lock timestamp if this pid still holds it; False if taken over."""
    lock_path = _engine_lock_path(root)
    current = time.time() if now is None else float(now)
    my_pid = os.getpid() if pid is None else int(pid)
    with state_lock("engine", root=root):
        existing_pid, _ = _read_engine_lock(lock_path)
        if existing_pid is not None and existing_pid != my_pid:
            return False
        _atomic_write(lock_path, {"pid": my_pid, "updated_at": current})
        return True


def release_engine_lock(root: Path | None = None) -> None:
    """Release engine.lock if present."""
    lock_path = _engine_lock_path(root)
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Actions log, daemon liveness, and GC
# ---------------------------------------------------------------------------


def log_action(
    text: str,
    root: Path | None = None,
    *,
    now: float | None = None,
) -> None:
    """Append a timestamped line to actions.log."""
    log_path = _root(root) / "actions.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    clean = " ".join(str(text).splitlines()).strip()
    line = f"{_iso_local(now)} {clean}\n"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()


def daemon_alive(
    max_age: float,
    root: Path | None = None,
    *,
    now: float | None = None,
) -> bool:
    """Return True if active_status.json is fresh (<= max_age) and monitor_pid is alive."""
    snap_path = _root(root) / snapshot.FILENAME
    try:
        raw = _read_json(snap_path)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    if not isinstance(raw, dict):
        return False
    updated_at = raw.get("updated_at")
    monitor_pid = raw.get("monitor_pid")
    if not isinstance(updated_at, str) or not isinstance(monitor_pid, int):
        return False
    try:
        updated_ts = datetime.fromisoformat(updated_at).timestamp()
    except ValueError:
        return False
    current = time.time() if now is None else float(now)
    if (current - updated_ts) > float(max_age):
        return False
    return _pid_alive(monitor_pid)


def gc(
    older_than_days: float = 7,
    root: Path | None = None,
    *,
    now: float | None = None,
) -> int:
    """Remove executed/void decisions and acked events older than older_than_days."""
    current = time.time() if now is None else float(now)
    cutoff = current - float(older_than_days) * 86400.0
    removed = 0

    for d in list_decisions(root=root):
        if d.status in ("executed", "void") and d.created_at < cutoff:
            try:
                _decision_path(d.id, root).unlink()
                removed += 1
            except FileNotFoundError:
                pass

    for e in list_events(root=root):
        if e.acked_at is not None and (e.acked_at < cutoff or e.created_at < cutoff):
            try:
                _event_path(e.id, root).unlink()
                removed += 1
            except FileNotFoundError:
                pass

    return removed
