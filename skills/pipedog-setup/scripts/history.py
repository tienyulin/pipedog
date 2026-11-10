"""Local CI history recording and queries.

Persists pipeline outcomes to a compact JSONL file in the state directory.
Allows tracking failure rates, flaky jobs, and recurring errors.
Standard library only.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import model
import snapshot

FILE = "history.jsonl"
MAX_LINES, TRIM_TO = 6000, 5000


def _root(root: Path | None = None) -> Path:
    return Path(root) if root is not None else snapshot.state_dir()


def _path(root: Path | None = None) -> Path:
    return _root(root) / FILE


def _atomic_write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _append_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def record(
    pipelines: list[model.Pipeline],
    root: Path | None = None,
    now: float | None = None,
) -> int:
    """Record passed, failed, and skipped pipeline runs to history.jsonl.

    Never raises (catches OSError and returns 0).
    """
    try:
        current_now = time.time() if now is None else float(now)
        target = _path(root)

        existing_lines: list[str] = []
        existing_keys: set[tuple[Any, str, Any, Any]] = set()

        if target.exists():
            try:
                with target.open("r", encoding="utf-8") as fh:
                    existing_lines = fh.readlines()
            except OSError:
                return 0

            for line in existing_lines:
                clean_line = line.strip()
                if not clean_line:
                    continue
                try:
                    rec = json.loads(clean_line)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                pid = rec.get("pipeline_id")
                pid_str = str(pid) if pid is not None else "None"
                existing_keys.add(
                    (rec.get("project"), pid_str, rec.get("sha"), rec.get("status"))
                )

        new_lines: list[str] = []
        for p in pipelines:
            if p.fetch_error is not None:
                continue
            if p.pipeline_id is None:
                continue
            if p.status not in ("passed", "failed", "skipped"):
                continue

            dedup_key = (p.project, str(p.pipeline_id), p.head_sha, p.status)
            if dedup_key in existing_keys:
                continue
            existing_keys.add(dedup_key)

            leaves: list[model.Job] = []
            for job in p.jobs:
                leaves.extend(job.leaves())

            starts = [
                leaf.started_at for leaf in leaves if leaf.started_at is not None
            ]
            finishes = [
                leaf.finished_at for leaf in leaves if leaf.finished_at is not None
            ]

            failed_entries: list[dict[str, Any]] = [
                {
                    "name": leaf.name,
                    "summary": (
                        leaf.error.error_summary if leaf.error else None
                    ),
                }
                for leaf in leaves
                if leaf.status == "failed" and not leaf.allow_failure
            ]

            record_dict = {
                "v": 1,
                "at": current_now,
                "project": p.project,
                "provider": p.provider,
                "branch": p.branch,
                "pipeline_id": p.pipeline_id,
                "sha": p.head_sha,
                "status": p.status,
                "started_at": min(starts) if starts else None,
                "finished_at": max(finishes) if finishes else None,
                "web_url": p.web_url,
                "jobs": [
                    {"name": leaf.name, "status": leaf.status} for leaf in leaves
                ],
                "failed": failed_entries,
            }

            serialized = (
                json.dumps(record_dict, sort_keys=True, separators=(",", ":"))
                + "\n"
            )
            new_lines.append(serialized)

        if not new_lines and len(existing_lines) <= MAX_LINES:
            return 0

        total_lines = len(existing_lines) + len(new_lines)
        if total_lines > MAX_LINES:
            all_lines = existing_lines + new_lines
            kept_lines = all_lines[-TRIM_TO:]
            _atomic_write_lines(target, kept_lines)
        elif new_lines:
            _append_lines(target, new_lines)

        return len(new_lines)
    except OSError:
        return 0


def load(root: Path | None = None, since: float | None = None) -> list[dict]:
    """Load records from history.jsonl, oldest first.

    Skips malformed lines and records whose v != 1.
    Keeps records with at >= since when given.
    Missing file returns [].
    """
    target = _path(root)
    try:
        with target.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except (FileNotFoundError, OSError):
        return []

    records: list[dict] = []
    for line in lines:
        clean = line.strip()
        if not clean:
            continue
        try:
            rec = json.loads(clean)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("v") != 1:
            continue
        at = rec.get("at")
        if since is not None:
            if at is None or not isinstance(at, (int, float)) or at < since:
                continue
        records.append(rec)

    records.sort(
        key=lambda r: float(r["at"]) if isinstance(r.get("at"), (int, float)) else 0.0
    )
    return records


def _job_failed(rec: dict[str, Any], job_name: str) -> bool:
    for j in rec.get("jobs", []):
        if isinstance(j, dict) and j.get("name") == job_name and j.get("status") == "failed":
            return True
    for f in rec.get("failed", []):
        if isinstance(f, dict) and f.get("name") == job_name:
            return True
    return False


def _job_passed(rec: dict[str, Any], job_name: str) -> bool:
    for j in rec.get("jobs", []):
        if isinstance(j, dict) and j.get("name") == job_name and j.get("status") == "passed":
            return True
    return False


def flaky_jobs(records: list[dict]) -> dict[tuple[str, str], int]:
    """Find flaky jobs across projects.

    Key: (project, job name)
    Value: count of distinct shas where that job has a "failed" record and a later
    (by `at`) "passed" record for the same project+sha.
    Only keys with count >= 1 returned.
    """
    by_proj_sha: dict[tuple[str, str], list[dict]] = {}
    for r in records:
        proj = r.get("project")
        sha = r.get("sha")
        at = r.get("at")
        if not proj or not sha or at is None:
            continue
        by_proj_sha.setdefault((str(proj), str(sha)), []).append(r)

    flaky_counts: dict[tuple[str, str], set[str]] = {}

    for (proj, sha), rec_list in by_proj_sha.items():
        job_names: set[str] = set()
        for r in rec_list:
            for j in r.get("jobs", []):
                if isinstance(j, dict) and j.get("name"):
                    job_names.add(str(j["name"]))
            for f in r.get("failed", []):
                if isinstance(f, dict) and f.get("name"):
                    job_names.add(str(f["name"]))

        for jname in job_names:
            failed_ats = [
                float(r["at"])
                for r in rec_list
                if isinstance(r.get("at"), (int, float)) and _job_failed(r, jname)
            ]
            passed_ats = [
                float(r["at"])
                for r in rec_list
                if isinstance(r.get("at"), (int, float)) and _job_passed(r, jname)
            ]
            if failed_ats and passed_ats and min(failed_ats) < max(passed_ats):
                flaky_counts.setdefault((proj, jname), set()).add(sha)

    return {
        k: len(shas)
        for k, shas in sorted(flaky_counts.items())
        if len(shas) >= 1
    }


def recurring(records: list[dict]) -> dict[tuple[str, str], int]:
    """Find recurring failure summaries across pipeline records.

    Key: (project, summary)
    Value: count of distinct pipeline records having that non-empty summary.
    Only counts >= 2 returned.
    """
    counts: dict[tuple[str, str], set[int]] = {}
    for idx, rec in enumerate(records):
        proj = rec.get("project")
        if not proj or not isinstance(proj, str):
            continue
        failed = rec.get("failed")
        if not isinstance(failed, list):
            continue
        for entry in failed:
            if not isinstance(entry, dict):
                continue
            summary = entry.get("summary")
            if summary and isinstance(summary, str) and summary.strip():
                counts.setdefault((proj, summary), set()).add(idx)

    return {
        k: len(indices)
        for k, indices in sorted(counts.items())
        if len(indices) >= 2
    }
