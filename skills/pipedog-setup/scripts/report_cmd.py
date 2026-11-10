"""pd report command - aggregates CI history and metrics."""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, TextIO

import deploystore
import history


def main(
    args: list[str],
    root: Path | None = None,
    now: float | None = None,
    out: TextIO | None = None,
) -> int:
    if out is None:
        out = sys.stdout
    days = 7
    project_filter: str | None = None
    as_json = False

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--days":
            if i + 1 >= len(args):
                sys.stderr.write("error: --days requires a value\n")
                return 2
            i += 1
            val_str = args[i]
            try:
                days = int(val_str)
                if days <= 0:
                    raise ValueError
            except ValueError:
                sys.stderr.write(
                    f"error: --days must be a positive integer, got {val_str!r}\n"
                )
                return 2
        elif arg.startswith("--days="):
            val_str = arg.split("=", 1)[1]
            try:
                days = int(val_str)
                if days <= 0:
                    raise ValueError
            except ValueError:
                sys.stderr.write(
                    f"error: --days must be a positive integer, got {val_str!r}\n"
                )
                return 2
        elif arg == "--project":
            if i + 1 >= len(args):
                sys.stderr.write("error: --project requires a value\n")
                return 2
            i += 1
            project_filter = args[i]
        elif arg.startswith("--project="):
            project_filter = arg.split("=", 1)[1]
        elif arg == "--json":
            as_json = True
        elif arg in ("-h", "--help"):
            out.write("Usage: pd report [--days N] [--project NAME] [--json]\n")
            return 0
        else:
            sys.stderr.write(f"error: unrecognized argument: {arg}\n")
            return 2
        i += 1

    current_now = time.time() if now is None else float(now)
    since = current_now - float(days) * 86400.0

    records = history.load(root=root, since=since)
    if project_filter is not None:
        records = [r for r in records if r.get("project") == project_filter]

    project_names = sorted(
        {str(r["project"]) for r in records if r.get("project")}
    )

    if not project_names:
        if as_json:
            payload = {
                "days": days,
                "generated_at": current_now,
                "projects": [],
            }
            out.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        else:
            out.write("no CI history yet (pd records it on every poll)\n")
        return 0

    flaky_by_project_job = history.flaky_jobs(records)
    recurring_by_project = history.recurring(records)
    raw_deploys = deploystore._load_history_dict(root=root)

    projects: list[dict[str, Any]] = []
    for p_name in project_names:
        proj_records = [r for r in records if r.get("project") == p_name]
        runs = len(proj_records)
        passed = sum(1 for r in proj_records if r.get("status") == "passed")
        failed = sum(1 for r in proj_records if r.get("status") == "failed")

        if (passed + failed) == 0:
            pass_rate: float | None = None
        else:
            pass_rate = round(passed / (passed + failed), 2)

        durations = [
            (float(r["finished_at"]) - float(r["started_at"])) / 60.0
            for r in proj_records
            if r.get("finished_at") is not None
            and r.get("started_at") is not None
            and isinstance(r["finished_at"], (int, float))
            and isinstance(r["started_at"], (int, float))
        ]
        if durations:
            median_minutes: float | None = round(
                float(statistics.median(durations)), 1
            )
        else:
            median_minutes = None

        failing_counts: dict[str, int] = {}
        for r in proj_records:
            failed_in_rec = set()
            for f in r.get("failed", []):
                if isinstance(f, dict) and f.get("name"):
                    failed_in_rec.add(str(f["name"]))
            for name in failed_in_rec:
                failing_counts[name] = failing_counts.get(name, 0) + 1

        top_failing = [
            {"job": name, "count": cnt}
            for name, cnt in sorted(
                failing_counts.items(), key=lambda x: (-x[1], x[0])
            )[:3]
        ]

        flaky = [
            {"job": jname, "count": cnt}
            for (proj, jname), cnt in flaky_by_project_job.items()
            if proj == p_name
        ]
        flaky.sort(key=lambda x: (-x["count"], x["job"]))

        rec_items = [
            {"summary": summ, "count": cnt}
            for (proj, summ), cnt in recurring_by_project.items()
            if proj == p_name
        ]
        rec_items.sort(key=lambda x: (-x["count"], x["summary"]))
        recurring = rec_items[:3]

        deploys: dict[str, int] = {}
        prefix = f"{p_name}/"
        for k, items in sorted(raw_deploys.items()):
            if not k.startswith(prefix):
                continue
            env = k[len(prefix) :]
            cnt = 0
            if isinstance(items, list):
                for it in items:
                    rec = deploystore._parse_deployment_record(it)
                    if (
                        rec is not None
                        and rec.status == "success"
                        and rec.created_at is not None
                        and isinstance(rec.created_at, (int, float))
                        and rec.created_at >= since
                    ):
                        cnt += 1
            if cnt > 0:
                deploys[env] = cnt

        projects.append(
            {
                "project": p_name,
                "runs": runs,
                "passed": passed,
                "failed": failed,
                "pass_rate": pass_rate,
                "median_minutes": median_minutes,
                "top_failing": top_failing,
                "flaky": flaky,
                "recurring": recurring,
                "deploys": deploys,
            }
        )

    projects.sort(key=lambda p: p["project"])

    if as_json:
        payload = {
            "days": days,
            "generated_at": current_now,
            "projects": projects,
        }
        out.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        return 0

    blocks: list[str] = []
    for p in projects:
        lines: list[str] = []
        lines.append(f"{p['project']} · last {days} days")
        summary_parts = [f"{p['runs']} run{'s' if p['runs'] != 1 else ''}"]
        if p["pass_rate"] is not None:
            summary_parts.append(f"{round(p['pass_rate'] * 100)}% passed")
        if p["median_minutes"] is not None:
            summary_parts.append(f"median {p['median_minutes']} min")
        lines.append("  " + " · ".join(summary_parts))

        if p["top_failing"]:
            items_str = " · ".join(
                f"{item['job']} ({item['count']})" for item in p["top_failing"]
            )
            lines.append(f"  top failing: {items_str}")

        if p["flaky"]:
            items_str = " · ".join(
                f"{item['job']} ({item['count']} sha{'s' if item['count'] != 1 else ''})"
                for item in p["flaky"]
            )
            lines.append(f"  flaky?: {items_str}")

        if p["recurring"]:
            items_str = " · ".join(
                f'"{item["summary"]}" ({item["count"]}×)'
                for item in p["recurring"]
            )
            lines.append(f"  seen again: {items_str}")

        if p["deploys"]:
            items_str = " · ".join(
                f"{env} {count}" for env, count in sorted(p["deploys"].items())
            )
            lines.append(f"  deploys: {items_str}")

        blocks.append("\n".join(lines))

    out.write("\n\n".join(blocks) + "\n")
    return 0
