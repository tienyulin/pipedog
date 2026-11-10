"""Sub-job aggregation rules (see docs/DESIGN.md section 5)."""

from __future__ import annotations

import re

import model

# Evaluated in order; the first matching rule decides the group name.
_RULES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(.+?) \d+/\d+$"),     # parallel:               "test 1/3"
    re.compile(r"^(.+?): \[.*\]$"),     # GitLab matrix:          "test: [3.11, linux]"
    re.compile(r"^(.+?) \(.*\)$"),      # GitHub matrix:          "test (3.11, ubuntu)"
    re.compile(r"^(.+?) / .+$"),        # GitHub reusable wf:     "build / linux"
    re.compile(r"^([^:\s]+):\S"),       # prefix:                 "deploy:k8s-dev"
)


def group_key(name: str) -> str | None:
    """Return the group name for ``name``, or None if it does not look grouped."""
    for rule in _RULES:
        m = rule.match(name)
        if m:
            return m.group(1)
    return None


def group_jobs(jobs: list[model.Job]) -> list[model.Job]:
    """Aggregate sibling jobs sharing a group key into one group Job.

    - A key with a single member is left as a plain leaf.
    - Original order is kept; a group sits at its first member's position.
    - Jobs that already carry ``sub_jobs`` (bridges) never take part.
    """
    keys: list[str | None] = [None if j.sub_jobs else group_key(j.name) for j in jobs]

    members: dict[str, list[model.Job]] = {}
    for job, key in zip(jobs, keys):
        if key is not None:
            members.setdefault(key, []).append(job)

    result: list[model.Job] = []
    emitted: set[str] = set()
    for job, key in zip(jobs, keys):
        if key is None or len(members[key]) < 2:
            result.append(job)
        elif key not in emitted:
            emitted.add(key)
            result.append(model.make_group(key, members[key]))
    return result
