"""Stage calculation and grouping for pipedog renderer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import model
import tui


@dataclass
class Stage:
    name: str
    status: str          # "passed", "failed", "running", "pending", "allowed"
    leaves: List[model.Job] = field(default_factory=list)
    jobs: List[model.Job] = field(default_factory=list)


def compute_stage_status(leaves: list[model.Job]) -> str:
    """Aggregate leaf job statuses into a stage status."""
    if not leaves:
        return "pending"

    non_allowed = [l for l in leaves if not l.allow_failure]
    allowed_failed = [l for l in leaves if l.allow_failure and l.status == "failed"]

    if non_allowed:
        agg = model.aggregate_status([l.status for l in non_allowed])
    else:
        # All leaves have allow_failure
        if all(l.status == "passed" for l in leaves):
            agg = "passed"
        elif any(l.status == "failed" for l in leaves):
            agg = "allowed"
        elif any(l.status == "running" for l in leaves):
            agg = "running"
        else:
            agg = "pending"
        return agg

    if agg == "passed" and allowed_failed:
        return "allowed"
    return agg


def group_stages_gitlab(jobs: list[model.Job]) -> list[Stage]:
    """Group GitLab jobs by Job.stage in pipeline order of first appearance."""
    stage_order: list[str] = []
    stage_map: dict[str, list[model.Job]] = {}

    for j in jobs:
        stage_name = j.stage or j.name
        if stage_name not in stage_map:
            stage_order.append(stage_name)
            stage_map[stage_name] = []
        stage_map[stage_name].append(j)

    stages: list[Stage] = []
    for s_name in stage_order:
        stage_jobs = stage_map[s_name]
        leaves: list[model.Job] = []
        for j in stage_jobs:
            leaves.extend(j.leaves())
        status = compute_stage_status(leaves)
        stages.append(Stage(name=s_name, status=status, leaves=leaves, jobs=stage_jobs))

    return stages


def _get_job_start(job: model.Job) -> float | None:
    if job.started_at is not None:
        return job.started_at
    if job.sub_jobs:
        starts = [s.started_at for s in job.sub_jobs if s.started_at is not None]
        if starts:
            return min(starts)
    return None


def group_stages_github(jobs: list[model.Job]) -> list[Stage]:
    """Group leaf jobs into stages by start time within 5 s, with trailing pending stage."""
    started_jobs: list[tuple[model.Job, float]] = []
    unstarted_jobs: list[model.Job] = []

    for j in jobs:
        st = _get_job_start(j)
        if st is not None:
            started_jobs.append((j, st))
        else:
            unstarted_jobs.append(j)

    # Stable sort by start time
    started_jobs.sort(key=lambda item: item[1])

    stage_buckets: list[list[model.Job]] = []
    for j, st in started_jobs:
        if not stage_buckets:
            stage_buckets.append([j])
        else:
            t0 = _get_job_start(stage_buckets[-1][0])
            if t0 is not None and abs(st - t0) <= 5.0:
                stage_buckets[-1].append(j)
            else:
                stage_buckets.append([j])

    if unstarted_jobs:
        stage_buckets.append(unstarted_jobs)

    stages: list[Stage] = []
    for bucket in stage_buckets:
        names: list[str] = []
        for j in bucket:
            names.append(j.name)
        combined_name = "+".join(names)
        if tui.str_width(combined_name) > 24:
            combined_name = tui.ellipsize_text(combined_name, 24)

        leaves: list[model.Job] = []
        for j in bucket:
            leaves.extend(j.leaves())

        status = compute_stage_status(leaves)
        stages.append(Stage(name=combined_name, status=status, leaves=leaves, jobs=bucket))

    return stages


def _stages_for_jobs(jobs: list[model.Job], provider: str) -> list[Stage]:
    """Return Stage objects for a list of jobs according to provider rules."""
    if provider == "gitlab":
        return group_stages_gitlab(jobs)
    elif provider == "github":
        return group_stages_github(jobs)
    else:
        if any(j.stage for j in jobs):
            return group_stages_gitlab(jobs)
        return group_stages_github(jobs)


def get_pipeline_stages(pipeline: model.Pipeline) -> list[Stage]:
    """Return Stage objects for pipeline according to provider rules."""
    return _stages_for_jobs(pipeline.jobs, pipeline.provider)
