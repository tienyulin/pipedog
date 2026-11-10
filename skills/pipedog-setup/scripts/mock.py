"""Simulated provider and forge for `--mock` (docs/DESIGN.md section 7, Mock).

Time-driven fake world: three projects with `flow` whose jobs and PRs
drive the flow state machine (merge decision, yellow-light decision, and
a `ci_failed` event at 5s). Grouping goes through the real `grouping.group_jobs()`
and the failure root cause through the real `extractor.extract()`.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Any, Literal

import extractor
import flowmodel
import grouping
import model
import snapshot
import store
from forge import Forge, ForgeError

FAIL_AFTER = 5.0      # auth-service `test 2/3` fails
FLOW_AFTER = 12.0     # payment-api (`main` branch): unit-tests passes, integration starts

DEMO_ROOT = "/tmp/pipedog-demo"
_FALLBACK_MOCK_DIR: Path | None = None


def mock_state_dir(root: Path | None = None) -> Path:
    """Resolve isolated state directory for `--mock` (never touches real ~/.local/state/pipedog)."""
    global _FALLBACK_MOCK_DIR
    if root is not None:
        return Path(root)
    override = os.environ.get("PIPEDOG_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if _FALLBACK_MOCK_DIR is None or not _FALLBACK_MOCK_DIR.exists():
        _FALLBACK_MOCK_DIR = Path(tempfile.mkdtemp(prefix="pipedog-mock-"))
    return _FALLBACK_MOCK_DIR

# Fake GitLab trace of the failing `test 2/3` job (ANSI + section markers on purpose,
# so the real log cleaner has something to do).
FAKE_PYTEST_LOG = (
    "\x1b[0Ksection_start:1762557300:prepare_script\r\x1b[0K\x1b[36;1mPreparing environment\x1b[0;m\n"
    "Running on runner-abc123 via gitlab-runner...\n"
    "\x1b[0Ksection_end:1762557301:prepare_script\r\x1b[0K\n"
    "\x1b[32;1m$ pytest -q tests/\x1b[0;m\n"
    "........F.....\n"
    "=================================== FAILURES ===================================\n"
    "______________________________ test_login_returns_200 _________________________\n"
    "\n"
    "    def test_login_returns_200(client):\n"
    "        resp = client.post('/login', json={'user': 'tony', 'password': 'pw'})\n"
    ">       assert resp.status_code == 200, f'expected 200 got {resp.status_code}'\n"
    "E       AssertionError: expected 200 got 401\n"
    "\n"
    "/builds/acme/auth-service/tests/test_auth.py:42: AssertionError\n"
    "=========================== short test summary info ============================\n"
    "FAILED tests/test_auth.py::test_login_returns_200 - AssertionError: expected 200 got 401\n"
    "1 failed, 13 passed in 3.21s\n"
    "\x1b[31;1mERROR: Job failed: exit code 1\x1b[0;m\n"
)

_PROJECTS: list[dict[str, Any]] = [
    {
        "provider": "gitlab",
        "id": 104,
        "name": "auth-service",
        "short": "auth",
        "local_path": f"{DEMO_ROOT}/auth-service",
        "branch": "feature/login",
        "mock_branch": "feature/login",
        "flow": {
            "chain": ["feature/*", "develop", "main"],
            "reviewers": [],
            "max_fix_rounds": 3,
        },
        "deploy": {
            "after_branch": "main",
            "environments": [
                {
                    "name": "staging",
                    "adapter": "command",
                    "trigger": "auto",
                    "command": {
                        "status": "echo '{\"status\": \"success\", \"sha\": \"a1b2c3d\", \"id\": \"dep-auth-stg\"}'",
                        "deploy": "echo 'deploying auth staging'",
                        "rollback": "echo 'rolling back auth staging'",
                        "parse": "json",
                    },
                    "verify": {
                        "command": "exit 0",
                        "duration_sec": 0,
                        "interval_sec": 1,
                        "fail_after": 1,
                    },
                },
                {
                    "name": "production",
                    "adapter": "command",
                    "production": True,
                    "trigger": "ask",
                    "command": {
                        "status": "echo '{\"status\": \"none\"}'",
                        "deploy": "echo 'deploying auth prod'",
                        "rollback": "echo 'rolling back auth prod'",
                        "parse": "json",
                    },
                },
            ],
        },
    },
    {
        "provider": "github",
        "repo": "octo-org/payment-api",
        "name": "payment-api",
        "short": "pay",
        "local_path": f"{DEMO_ROOT}/payment-api",
        "branch": "main",
        "mock_branch": "feature/checkout",
        "flow": {
            "chain": ["feature/*", "main"],
            "reviewers": [],
            "max_fix_rounds": 3,
        },
    },
    {
        "provider": "gitlab",
        "id": 305,
        "name": "web-frontend",
        "short": "web",
        "local_path": f"{DEMO_ROOT}/web-frontend",
        "branch": "main",
        "mock_branch": "feature/ui",
        "flow": {
            "chain": ["feature/*", "develop", "main"],
            "reviewers": [],
            "max_fix_rounds": 3,
        },
        "deploy": {
            "after_branch": "main",
            "environments": [
                {
                    "name": "staging",
                    "adapter": "command",
                    "trigger": "auto",
                    "rollback": "auto",
                    "command": {
                        "status": "echo '{\"status\": \"success\", \"sha\": \"c7d8e9f\", \"id\": \"dep-web-stg\"}'",
                        "history": "echo '[{\"status\": \"success\", \"sha\": \"c7d8e90\", \"id\": \"dep-web-good\"}]'",
                        "deploy": "echo 'deploying web staging'",
                        "rollback": "echo 'rolling back web staging to {previous_id}'",
                        "parse": "json",
                    },
                    "verify": {
                        "command": "exit 1",
                        "duration_sec": 30,
                        "interval_sec": 8,
                        "fail_after": 2,
                    },
                },
            ],
        },
    },
]


class MockForge(Forge):
    """In-memory Forge for `--mock` mode (never touches the network)."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root
        self._next_number = 50
        self._prs: dict[tuple[str, int], flowmodel.PrState] = {
            ("payment-api", 18): flowmodel.PrState(
                number=18,
                url="https://github.com/octo-org/payment-api/pull/18",
                source="feature/checkout",
                target="main",
                head_sha="e4f5a6b",
                state="open",
                review="approved",
                unresolved_threads=0,
                comment_ids=[],
                author_is_me=True,
                approvals_given=1,
                approvals_required=1,
                mergeable=True,
            ),
        }

    def me(self) -> str:
        return "mock-user"

    def _sync_executed_merges(self, project_name: str) -> None:
        try:
            decisions = store.list_decisions(
                status="executed", root=mock_state_dir(self._root)
            )
        except Exception:
            return
        for d in decisions:
            if d.project == project_name and d.kind == "merge" and d.pr is not None:
                pr = self._prs.get((project_name, d.pr))
                if pr is not None:
                    pr.state = "merged"

    def find_open_pr(
        self, project: dict, source: str, target: str
    ) -> flowmodel.PrState | None:
        proj_name = str(project.get("name") or "")
        self._sync_executed_merges(proj_name)
        for (p_name, _), pr in self._prs.items():
            if (
                p_name == proj_name
                and pr.source == source
                and pr.target == target
                and pr.state == "open"
            ):
                return pr
        return None

    def get_pr(self, project: dict, number: int) -> flowmodel.PrState:
        proj_name = str(project.get("name") or "")
        self._sync_executed_merges(proj_name)
        pr = self._prs.get((proj_name, number))
        if pr is None:
            raise ForgeError(f"PR #{number} not found in MockForge")
        return pr

    def create_pr(
        self,
        project: dict,
        source: str,
        target: str,
        title: str,
        reviewers: list[str] = (),
        **kwargs: Any,
    ) -> flowmodel.PrState:
        proj_name = str(project.get("name") or "")
        num = self._next_number
        self._next_number += 1
        pr = flowmodel.PrState(
            number=num,
            url=f"https://example.com/{proj_name}/pull/{num}",
            source=source,
            target=target,
            head_sha="mock-sha",
            state="open",
            review="review_required",
            unresolved_threads=0,
            comment_ids=[],
            author_is_me=True,
        )
        self._prs[(proj_name, num)] = pr
        return pr

    def list_comments(self, project: dict, number: int) -> list[dict[str, Any]]:
        return []

    def reply(self, project: dict, number: int, comment_id: str, body: str) -> str:
        proj_name = str(project.get("name") or "")
        return f"https://example.com/{proj_name}/pull/{number}#comment-{comment_id}"

    def merge(
        self,
        project: dict,
        number: int,
        *,
        method: Literal["squash", "merge"],
        delete_branch: bool,
        expected_sha: str,
    ) -> None:
        pr = self.get_pr(project, number)
        if pr.head_sha != expected_sha:
            raise ForgeError("head moved")
        pr.state = "merged"


def resolve_mock_start(
    now: float | None = None, root: Path | None = None
) -> float:
    """Resolve start timestamp for `--mock`, persisting across `--once` calls in mock state dir."""
    current = time.time() if now is None else float(now)
    elapsed_env = os.environ.get("PIPEDOG_MOCK_ELAPSED")
    if elapsed_env:
        try:
            return current - float(elapsed_env)
        except ValueError:
            pass
    marker = mock_state_dir(root) / ".mock_start"
    try:
        if marker.exists():
            saved = float(marker.read_text(encoding="utf-8").strip())
            if 0 <= current - saved <= 3600:
                return saved
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{current}\n", encoding="utf-8")
    except OSError:
        pass
    return current


class MockWorld:
    def __init__(self, start: float, root: Path | None = None):
        self.start = float(start)
        self.root = mock_state_dir(root)
        self._error: model.ErrorInfo | None = None
        self._forge = MockForge(root=self.root)
        self._init_demo_dirs()
        self._seed_mock_deploys()

    def _init_demo_dirs(self) -> None:
        try:
            for p in _PROJECTS:
                lp = p.get("local_path")
                if lp:
                    os.makedirs(lp, exist_ok=True)
        except OSError:
            pass

    def _seed_mock_deploys(self) -> None:
        try:
            import deploymodel
            import deploystore

            root = self.root
            st_auth = deploystore.get_state("auth-service", "staging", root=root)
            if st_auth is None:
                deploystore.save_state(
                    deploymodel.DeployState(
                        project="auth-service",
                        env="staging",
                        sha="a1b2c3d",
                        verified_sha="a1b2c3d",
                        phase="verified",
                    ),
                    root=root,
                )
                deploystore.record_good(
                    "auth-service",
                    "staging",
                    deploymodel.DeploymentRecord(
                        id="dep-auth-stg",
                        sha="a1b2c3d",
                        status="success",
                    ),
                    root=root,
                )

            st_web = deploystore.get_state("web-frontend", "staging", root=root)
            if st_web is None:
                deploystore.save_state(
                    deploymodel.DeployState(
                        project="web-frontend",
                        env="staging",
                        sha="c7d8e9f",
                        phase="verifying",
                        verify=deploymodel.VerifyProgress(
                            sha="c7d8e9f",
                            started_at=self.start,
                            checks=1,
                            consecutive_failures=1,
                            last_check_at=self.start,
                        ),
                    ),
                    root=root,
                )
                deploystore.record_good(
                    "web-frontend",
                    "staging",
                    deploymodel.DeploymentRecord(
                        id="dep-web-good",
                        sha="c7d8e90",
                        status="success",
                    ),
                    root=root,
                )
        except Exception:
            pass

    def projects(self) -> list[dict[str, Any]]:
        return [dict(p) for p in _PROJECTS]

    def forge(self) -> MockForge:
        return self._forge

    # -- helpers -----------------------------------------------------------

    def _failure(self) -> model.ErrorInfo:
        # Parse once through the real extractor; reuse afterwards (like a trace cache).
        if self._error is None:
            self._error = extractor.extract(FAKE_PYTEST_LOG, ["/builds/acme/auth-service/"])
        return self._error

    def fetch(self, project: dict, branch: str | None, now: float) -> model.Pipeline:
        name = str(project.get("name") or "")
        cfg = next((p for p in _PROJECTS if p["name"] == name), project)
        resolved_branch = branch if branch is not None else cfg.get("branch")
        ref = str(cfg.get("repo") or cfg.get("id") or name)
        base = model.Pipeline(
            project=str(project.get("name") or "?"),
            short=str(project.get("short") or cfg.get("short") or "?"),
            provider=cfg.get("provider", "gitlab"),
            project_ref=ref,
            local_path=project.get("local_path") or cfg.get("local_path"),
            branch=resolved_branch,
            fetched_at=now,
        )
        t = now - self.start
        if name == "auth-service":
            pipeline_id, web_url, head_sha, leaves = self._auth(t, resolved_branch)
        elif name == "payment-api":
            pipeline_id, web_url, head_sha, leaves = self._payment(t, resolved_branch)
        elif name == "web-frontend":
            pipeline_id, web_url, head_sha, leaves = self._web(t, resolved_branch)
        else:
            base.fetch_error = "unknown mock project"
            return base

        jobs = grouping.group_jobs(leaves)
        base.pipeline_id = pipeline_id
        base.web_url = web_url
        base.head_sha = head_sha
        base.jobs = jobs
        non_allowed_statuses = [
            "passed" if (leaf.status == "failed" and leaf.allow_failure) else leaf.status
            for leaf in leaves
        ]
        base.status = model.aggregate_status(non_allowed_statuses)
        return base

    # -- projects ----------------------------------------------------------

    def _job(
        self,
        url: str,
        jid: int,
        name: str,
        status: str,
        stage: str | None = None,
        started_ago: float | None = None,
        dur: float | None = None,
        *,
        allow_failure: bool = False,
    ) -> model.Job:
        """Leaf job; started_ago is relative to the world's start time."""
        started = self.start - started_ago if started_ago is not None else None
        finished = None
        if status in ("passed", "failed") and started is not None:
            finished = started + (dur if dur is not None else 30.0)
        return model.Job(
            name=name,
            status=status,  # type: ignore[arg-type]
            stage=stage,
            job_id=jid,
            web_url=f"{url}/{jid}",
            started_at=started,
            finished_at=finished,
            allow_failure=allow_failure,
        )

    def _auth(
        self, t: float, branch: str | None = None
    ) -> tuple[int, str, str, list[model.Job]]:
        base = "https://gitlab.com/acme/auth-service"
        jobs_url = f"{base}/-/jobs"
        j = self._job
        if branch == "main":
            leaves = [
                j(jobs_url, 998870, "lint", "passed", "check", 300, 25),
                j(jobs_url, 998871, "build", "passed", "build", 260, 90),
                j(jobs_url, 998872, "security-scan", "passed", "check", 300, 60),
                j(jobs_url, 998876, "test 1/3", "passed", "test", 160, 70),
                j(jobs_url, 998877, "test 2/3", "passed", "test", 160, 70),
                j(jobs_url, 998878, "test 3/3", "passed", "test", 160, 70),
            ]
            return 104928, f"{base}/-/pipelines/104928", "a1b2c3d", leaves

        failed = t >= FAIL_AFTER
        test2 = j(
            jobs_url,
            998877,
            "test 2/3",
            "failed" if failed else "running",
            "test",
            started_ago=40,
            dur=40 + FAIL_AFTER if failed else None,
        )
        if failed:
            test2.error = self._failure()
        leaves = [
            j(jobs_url, 998870, "lint", "passed", "check", 300, 25),
            j(jobs_url, 998871, "build", "passed", "build", 260, 90),
            j(jobs_url, 998872, "security-scan", "passed", "check", 300, 60),
            j(jobs_url, 998876, "test 1/3", "passed", "test", 160, 70),
            test2,
            j(jobs_url, 998878, "test 3/3", "running", "test", 35),
        ]
        return 104928, f"{base}/-/pipelines/104928", "a1b2c3d", leaves

    def _payment(
        self, t: float, branch: str | None = None
    ) -> tuple[int, str, str, list[model.Job]]:
        base = "https://github.com/octo-org/payment-api"
        run_url = f"{base}/actions/runs/7700112233/job"
        j = self._job
        if branch and branch != "main":
            leaves = [
                j(run_url, 51001, "build (3.12)", "passed", None, 400, 120),
                j(run_url, 51002, "build (3.13)", "passed", None, 400, 130),
                j(run_url, 51003, "lint", "passed", None, 300, 30),
                j(run_url, 51004, "typecheck", "passed", None, 298, 45),
                j(run_url, 51005, "docs", "passed", None, 200, 50),
                j(run_url, 51006, "unit-tests", "passed", None, 100, 75),
                j(run_url, 51007, "integration", "passed", None, 100, 60),
            ]
            return 7700112233, f"{base}/actions/runs/7700112233", "e4f5a6b", leaves

        flowed = t >= FLOW_AFTER
        leaves = [
            j(run_url, 51001, "build (3.12)", "passed", None, 400, 120),
            j(run_url, 51002, "build (3.13)", "passed", None, 400, 130),
            j(run_url, 51003, "lint", "passed", None, 300, 30),
            j(run_url, 51004, "typecheck", "passed", None, 298, 45),
            j(run_url, 51005, "docs", "passed", None, 200, 50),
            j(
                run_url,
                51006,
                "unit-tests",
                "passed" if flowed else "running",
                None,
                75,
                75 + FLOW_AFTER if flowed else None,
            ),
            j(
                run_url,
                51007,
                "integration",
                "running" if flowed else "pending",
                None,
                -FLOW_AFTER if flowed else None,
            ),
        ]
        return 7700112233, f"{base}/actions/runs/7700112233", "e4f5a6b", leaves

    def _web(
        self, t: float, branch: str | None = None
    ) -> tuple[int, str, str, list[model.Job]]:
        base = "https://gitlab.com/acme/web-frontend"
        jobs_url = f"{base}/-/jobs"
        j = self._job
        if branch and branch != "main":
            leaves = [
                j(jobs_url, 770001, "install", "passed", "prepare", 600, 80),
                j(
                    jobs_url,
                    770002,
                    "eslint",
                    "failed",
                    "check",
                    500,
                    40,
                    allow_failure=True,
                ),
                j(jobs_url, 770003, "unit", "passed", "test", 480, 90),
                j(jobs_url, 770004, "e2e", "passed", "test", 300, 110),
                j(jobs_url, 770005, "deploy:k8s-dev", "passed", "deploy", 200, 60),
                j(jobs_url, 770006, "deploy:k8s-prod", "passed", "deploy", 120, 55),
            ]
            return 55120, f"{base}/-/pipelines/55120", "c7d8e9f", leaves

        leaves = [
            j(jobs_url, 770001, "install", "passed", "prepare", 600, 80),
            j(jobs_url, 770002, "eslint", "passed", "check", 500, 40),
            j(jobs_url, 770003, "unit", "passed", "test", 480, 90),
            j(jobs_url, 770004, "e2e", "passed", "test", 300, 110),
            j(jobs_url, 770005, "deploy:k8s-dev", "passed", "deploy", 200, 60),
            j(jobs_url, 770006, "deploy:k8s-prod", "passed", "deploy", 120, 55),
        ]
        return 55120, f"{base}/-/pipelines/55120", "c7d8e9f", leaves


def gallery_data(now: float = 1762558500.0) -> dict[str, Any]:
    """Return deterministic data exercising every pipeline / flow / env scenario."""
    decisions = [
        flowmodel.Decision(
            id="dec-merge",
            kind="merge",
            project="payment-api",
            source="feature/checkout",
            target="main",
            pr=18,
            head_sha="e4f5a6b",
            reasons=[{"summary": "approved 2/2, CI green, mergeable"}],
            created_at=now - 120,
        ),
        flowmodel.Decision(
            id="dec-yellow",
            kind="yellow",
            project="web-frontend",
            source="feature/ui",
            target="develop",
            pr=22,
            head_sha="c7d8e9f",
            reasons=[
                {"job": "eslint", "kind": "allowed_failure", "summary": "eslint allowed"}
            ],
            created_at=now - 200,
        ),
        flowmodel.Decision(
            id="dec-promote",
            kind="promote",
            project="auth-service",
            source="develop",
            target="main",
            pr=30,
            head_sha="a1b2c3d",
            reasons=[{"summary": "develop CI green at a1b2c3d"}],
            created_at=now - 300,
        ),
        flowmodel.Decision(
            id="dec-deploy",
            kind="deploy",
            project="auth-service",
            source="main",
            target="production",
            pr=None,
            head_sha="a1b2c3d",
            reasons=[{"summary": "staging verified a1b2c3d"}],
            created_at=now - 400,
        ),
        flowmodel.Decision(
            id="dec-gate",
            kind="deploy_approval",
            project="payment-api",
            source="main",
            target="production",
            pr=None,
            head_sha="e4f5a6b",
            reasons=[{"summary": "platform gate gate-prod waiting for approval"}],
            created_at=now - 500,
        ),
        flowmodel.Decision(
            id="dec-rb",
            kind="rollback",
            project="web-frontend",
            source="main",
            target="staging",
            pr=None,
            head_sha="c7d8e90",
            reasons=[
                {"summary": "verify failed on staging: HTTP 500; roll back to b2c3d4e"}
            ],
            created_at=now - 600,
        ),
        flowmodel.Decision(
            id="dec-stuck",
            kind="deploy_stuck",
            project="web-frontend",
            source="main",
            target="staging",
            pr=None,
            head_sha="c7d8e90",
            reasons=[{"summary": "rollback failed on staging, retry?"}],
            created_at=now - 700,
        ),
    ]

    warnings = ["warning: 1 job retry remaining on runner-04"]

    prs: dict[tuple[str, str], flowmodel.PrState] = {
        ("pr-approved-mergeable", "feature/checkout"): flowmodel.PrState(
            number=12,
            url="https://github.com/octo/pr-approved-mergeable/pull/12",
            source="feature/checkout",
            target="main",
            head_sha="e4f5a6b",
            state="open",
            review="approved",
            approvals_given=1,
            approvals_required=1,
            unresolved_threads=2,
            mergeable=True,
        ),
        ("pr-changes-requested", "feature/pay-fix"): flowmodel.PrState(
            number=42,
            url="https://github.com/octo/pr-changes-requested/pull/42",
            source="feature/pay-fix",
            target="main",
            head_sha="c4a46e5",
            state="open",
            review="changes_requested",
            unresolved_threads=3,
            mergeable=False,
        ),
        ("pr-review-required", "feature/auth-v2"): flowmodel.PrState(
            number=15,
            url="https://github.com/octo/pr-review-required/pull/15",
            source="feature/auth-v2",
            target="main",
            head_sha="4455667",
            state="open",
            review="review_required",
            unresolved_threads=0,
            mergeable=True,
        ),
        ("pr-no-approval-rule", "feature/quick-fix"): flowmodel.PrState(
            number=19,
            url="https://github.com/octo/pr-no-approval-rule/pull/19",
            source="feature/quick-fix",
            target="main",
            head_sha="3322110",
            state="open",
            review="none",
            unresolved_threads=0,
            mergeable=True,
        ),
        ("gitlab-mr", "feature/api"): flowmodel.PrState(
            number=88,
            url="https://gitlab.com/acme/gitlab-mr/-/merge_requests/88",
            source="feature/api",
            target="main",
            head_sha="8877665",
            state="open",
            review="approved",
            approvals_given=2,
            approvals_required=2,
            unresolved_threads=0,
            mergeable=True,
        ),
    }

    pipelines = [
        model.Pipeline(
            project="gitlab-running",
            short="gl-run",
            provider="gitlab",
            project_ref="101",
            branch="main",
            pipeline_id=101,
            status="running",
            head_sha="a1b2c3d",
            jobs=[
                model.Job(name="build", status="passed", stage="build", started_at=now - 300, finished_at=now - 240),
                model.Job(name="lint", status="passed", stage="lint", started_at=now - 240, finished_at=now - 200),
                model.make_group(
                    "test",
                    [
                        model.Job(name="test 1/3", status="passed", stage="test", started_at=now - 200, finished_at=now - 150),
                        model.Job(name="test 2/3", status="running", stage="test", started_at=now - 160),
                        model.Job(name="test 3/3", status="pending", stage="test"),
                    ],
                ),
                model.Job(name="deploy", status="pending", stage="deploy"),
            ],
        ),
        model.Pipeline(
            project="gitlab-failed",
            short="gl-fail",
            provider="gitlab",
            project_ref="102",
            branch="feature/fix",
            pipeline_id=102,
            status="failed",
            head_sha="f1a11ed",
            jobs=[
                model.Job(name="build", status="passed", stage="build", started_at=now - 180, finished_at=now - 120),
                model.Job(
                    name="unit-tests",
                    status="failed",
                    stage="test",
                    started_at=now - 120,
                    finished_at=now - 42,
                    error=model.ErrorInfo(
                        error_summary="AssertionError: expected 200 (line 42)",
                    ),
                ),
                model.Job(name="deploy", status="pending", stage="deploy"),
            ],
        ),
        model.Pipeline(
            project="gitlab-allowed-fail",
            short="gl-allow",
            provider="gitlab",
            project_ref="103",
            branch="feature/ui",
            pipeline_id=103,
            status="running",
            head_sha="c7d8e9f",
            jobs=[
                model.Job(name="build", status="passed", stage="build", started_at=now - 200, finished_at=now - 150),
                model.Job(
                    name="eslint",
                    status="failed",
                    stage="check",
                    allow_failure=True,
                    started_at=now - 150,
                    finished_at=now - 120,
                ),
                model.Job(name="test", status="running", stage="test", started_at=now - 120),
                model.Job(name="deploy", status="pending", stage="deploy"),
            ],
        ),
        model.Pipeline(
            project="gitlab-passed",
            short="gl-pass",
            provider="gitlab",
            project_ref="104",
            branch="main",
            pipeline_id=104,
            status="passed",
            head_sha="b0b0b0b",
            jobs=[
                model.Job(name="build", status="passed", stage="build", started_at=now - 600, finished_at=now - 500),
                model.Job(name="test", status="passed", stage="test", started_at=now - 500, finished_at=now - 400),
                model.Job(name="deploy", status="passed", stage="deploy", started_at=now - 400, finished_at=now - 350),
            ],
        ),
        model.Pipeline(
            project="gitlab-pending",
            short="gl-pend",
            provider="gitlab",
            project_ref="105",
            branch="feature/next",
            pipeline_id=105,
            status="pending",
            head_sha="0011223",
            jobs=[
                model.Job(name="build", status="pending", stage="build"),
                model.Job(name="test", status="pending", stage="test"),
                model.Job(name="deploy", status="pending", stage="deploy"),
            ],
        ),
        model.Pipeline(
            project="gitlab-manual-skipped",
            short="gl-man",
            provider="gitlab",
            project_ref="106",
            branch="hotfix/patch",
            pipeline_id=106,
            status="pending",
            head_sha="5566778",
            jobs=[
                model.Job(name="build", status="passed", stage="build", started_at=now - 200, finished_at=now - 150),
                model.Job(name="cleanup", status="skipped", stage="clean"),
                model.Job(name="deploy-manual", status="manual", stage="deploy"),
            ],
        ),
        model.Pipeline(
            project="github-parallel",
            short="gh-par",
            provider="github",
            project_ref="octo/gh-par",
            branch="feature/perf",
            pipeline_id=201,
            status="running",
            head_sha="9988776",
            jobs=[
                model.Job(name="lint", status="passed", started_at=now - 100, finished_at=now - 70),
                model.Job(name="test", status="passed", started_at=now - 98, finished_at=now - 50),
                model.Job(name="build", status="running", started_at=now - 50),
                model.Job(name="deploy", status="pending"),
            ],
        ),
        model.Pipeline(
            project="github-multi-workflow",
            short="gh-multi",
            provider="github",
            project_ref="octo/gh-multi",
            branch="main",
            pipeline_id=206,
            status="passed",
            head_sha="5a6b7c8",
            jobs=[
                model.Job(
                    name="test",
                    status="passed",
                    started_at=now - 120,
                    finished_at=now - 60,
                    workflow="CI",
                ),
                model.Job(
                    name="mock-smoke",
                    status="passed",
                    started_at=now - 120,
                    finished_at=now - 70,
                    workflow="CI",
                ),
                model.Job(
                    name="lint",
                    status="passed",
                    started_at=now - 120,
                    finished_at=now - 90,
                    workflow="Lint",
                ),
            ],
        ),
        model.Pipeline(
            project="pr-approved-mergeable",
            short="pr-app",
            provider="github",
            project_ref="octo/pr-app",
            branch="feature/checkout",
            pipeline_id=202,
            status="running",
            head_sha="e4f5a6b",
            jobs=[
                model.Job(name="build", status="passed", started_at=now - 200, finished_at=now - 150),
                model.Job(name="test", status="running", started_at=now - 150),
                model.Job(name="deploy", status="pending"),
            ],
        ),
        model.Pipeline(
            project="pr-changes-requested",
            short="pr-chg",
            provider="github",
            project_ref="octo/pr-chg",
            branch="feature/pay-fix",
            pipeline_id=203,
            status="failed",
            head_sha="c4a46e5",
            jobs=[
                model.Job(
                    name="test",
                    status="failed",
                    started_at=now - 90,
                    finished_at=now - 15,
                    error=model.ErrorInfo(
                        error_summary="AssertionError: expected 200 (line 42)",
                        file="src/pay.py",
                        line=42,
                    ),
                ),
            ],
        ),
        model.Pipeline(
            project="pr-review-required",
            short="pr-rev",
            provider="github",
            project_ref="octo/pr-rev",
            branch="feature/auth-v2",
            pipeline_id=204,
            status="running",
            head_sha="4455667",
            jobs=[
                model.Job(name="build", status="passed", started_at=now - 150, finished_at=now - 80),
                model.Job(name="test", status="running", started_at=now - 80),
            ],
        ),
        model.Pipeline(
            project="pr-no-approval-rule",
            short="pr-noapp",
            provider="github",
            project_ref="octo/pr-noapp",
            branch="feature/quick-fix",
            pipeline_id=205,
            status="running",
            head_sha="3322110",
            jobs=[
                model.Job(name="test", status="running", started_at=now - 40),
            ],
        ),
        model.Pipeline(
            project="gitlab-mr",
            short="gl-mr",
            provider="gitlab",
            project_ref="107",
            branch="feature/api",
            pipeline_id=107,
            status="running",
            head_sha="8877665",
            jobs=[
                model.Job(name="build", status="passed", stage="build", started_at=now - 100, finished_at=now - 50),
                model.Job(name="test", status="running", stage="test", started_at=now - 50),
            ],
        ),
        model.Pipeline(
            project="long-chain-wrap",
            short="long",
            provider="gitlab",
            project_ref="108",
            branch="release/v2",
            pipeline_id=108,
            status="running",
            head_sha="1122334",
            jobs=[
                model.Job(name="prepare", status="passed", stage="prepare", started_at=now - 800, finished_at=now - 700),
                model.Job(name="lint", status="passed", stage="lint", started_at=now - 700, finished_at=now - 650),
                model.Job(name="compile", status="passed", stage="compile", started_at=now - 650, finished_at=now - 550),
                model.Job(name="unit", status="passed", stage="unit", started_at=now - 550, finished_at=now - 450),
                model.Job(name="integration", status="passed", stage="integration", started_at=now - 450, finished_at=now - 350),
                model.Job(name="security", status="passed", stage="security", started_at=now - 350, finished_at=now - 250),
                model.Job(name="package", status="running", stage="package", started_at=now - 250),
                model.Job(name="deploy", status="pending", stage="deploy"),
            ],
        ),
        model.Pipeline(
            project="fetch-error",
            short="err",
            provider="gitlab",
            project_ref="109",
            branch="main",
            pipeline_id=None,
            status=None,
            fetch_error="HTTP 401 Unauthorized",
        ),
        model.Pipeline(
            project="no-pipeline-yet",
            short="nopipe",
            provider="github",
            project_ref="octo/nopipe",
            branch="feature/new-repo",
            pipeline_id=None,
            status=None,
            jobs=[],
        ),
    ]

    environments = [
        {
            "project": "auth-service",
            "env": "staging",
            "sha": "a1b2c3d4e5f6",
            "status": "success",
            "verified": True,
        },
        {
            "project": "auth-service",
            "env": "production",
            "sha": None,
            "status": "waiting_approval",
            "verified": False,
        },
        {
            "project": "web-frontend",
            "env": "staging",
            "sha": "c7d8e9f01234",
            "status": "running",
            "verified": False,
        },
        {
            "project": "web-frontend",
            "env": "production",
            "sha": None,
            "status": "none",
            "verified": False,
        },
        {
            "project": "payment-api",
            "env": "staging",
            "sha": "e4f5a6b7c8d9",
            "status": "failed",
            "rollback": "auto",
            "verified": False,
        },
    ]

    return {
        "pipelines": pipelines,
        "decisions": decisions,
        "warnings": warnings,
        "environments": environments,
        "prs": prs,
    }


def gallery_panel(caps: Any = None, now: float = 1762558500.0) -> Any:
    """Return static mock gallery Panel containing every scenario."""
    import renderer
    if caps is None:
        caps = renderer.detect_caps()
    data = gallery_data(now=now)
    return renderer.render(
        data["pipelines"],
        now=now,
        caps=caps,
        decisions=data["decisions"],
        warnings=data["warnings"],
        environments=data["environments"],
        prs=data["prs"],
        mock=True,
        collapse_passed=False,
    )

