"""Single verification run and progress advancement (Task 3).

Checks url, command, and metric as configured in verify block.
Stdlib only. See docs/superpowers/specs/2026-11-08-continuous-deployment-design.md.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import urllib.error
import urllib.request
from typing import Any, Callable, Literal

from deploymodel import CheckResult, VerifyProgress


def default_http_get(url: str, timeout: float = 10.0) -> tuple[int, str]:
    """Default http_get implementation using urllib."""
    req = urllib.request.Request(url, headers={"User-Agent": "pipedog-verify"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", resp.getcode() if hasattr(resp, "getcode") else 200)
            body = resp.read().decode("utf-8", errors="replace")
            return int(status), body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return int(e.code), body
    except Exception as e:
        return 0, f"HTTP error: {e}"


def default_runner(
    cmd: str,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 300.0,
) -> tuple[int, str, str]:
    """Default subprocess runner."""
    proc = subprocess.run(
        cmd,
        shell=True,
        cwd=cwd,
        env=env,
        timeout=timeout,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _normalize_http_response(resp: Any) -> tuple[int, str]:
    if isinstance(resp, tuple):
        status = int(resp[0])
        body = str(resp[1]) if len(resp) > 1 else ""
        return status, body
    status = getattr(resp, "status_code", getattr(resp, "status", None))
    if status is None and hasattr(resp, "getcode"):
        status = resp.getcode()
    if status is None:
        status = 200
    text = getattr(resp, "text", None)
    if text is None and hasattr(resp, "read"):
        data = resp.read()
        text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    elif text is None:
        text = str(resp)
    return int(status), str(text)


def _normalize_runner_result(res: Any) -> tuple[int, str, str]:
    if isinstance(res, int):
        return res, "", ""
    if isinstance(res, tuple):
        code = int(res[0])
        out = str(res[1]) if len(res) > 1 else ""
        err = str(res[2]) if len(res) > 2 else ""
        return code, out, err
    code = getattr(res, "returncode", getattr(res, "exit_code", 0))
    out = getattr(res, "stdout", "") or ""
    err = getattr(res, "stderr", "") or ""
    if isinstance(out, bytes):
        out = out.decode("utf-8", errors="replace")
    if isinstance(err, bytes):
        err = err.decode("utf-8", errors="replace")
    return int(code), str(out), str(err)


def _call_runner(
    runner: Callable[..., Any],
    cmd: str,
    cwd: str | None,
    env: dict[str, str],
    timeout: float,
) -> tuple[int, str, str]:
    try:
        res = runner(cmd, cwd=cwd, env=env, timeout=timeout)
    except TypeError:
        try:
            res = runner(cmd, cwd=cwd, env=env)
        except TypeError:
            try:
                res = runner(cmd, cwd=cwd)
            except TypeError:
                res = runner(cmd)
    return _normalize_runner_result(res)


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    for token_var in (
        "PIPEDOG_GITLAB_TOKEN",
        "GITLAB_TOKEN",
        "PIPEDOG_GITHUB_TOKEN",
        "GITHUB_TOKEN",
    ):
        env.pop(token_var, None)
    return env


def _substitute_placeholders(
    cmd: str,
    *,
    project: dict | None = None,
    env: dict | None = None,
    sha: str = "",
) -> str:
    env_name = ""
    if isinstance(env, dict):
        env_name = env.get("name", "") or env.get("environment", "")
    elif isinstance(env, str):
        env_name = env

    branch = ""
    if isinstance(project, dict):
        branch = project.get("branch", "") or project.get("after_branch", "")

    subs = {
        "{sha}": shlex.quote(sha),
        "{env}": shlex.quote(env_name),
        "{branch}": shlex.quote(branch),
    }
    result = cmd
    for placeholder, val in subs.items():
        result = result.replace(placeholder, val)
    return result


def check_once(
    verify_cfg: dict | None,
    *,
    project: dict | None = None,
    env: dict | None = None,
    sha: str = "",
    http_get: Callable[..., Any] = default_http_get,
    runner: Callable[..., Any] = default_runner,
) -> CheckResult:
    """Run one verification cycle.

    All configured checks (url, command, metric) are executed.
    Any failure means not ok, and details are concatenated.
    """
    if not verify_cfg:
        return CheckResult(ok=True, detail="")

    checks: list[tuple[bool, str]] = []
    clean_env = _clean_env()
    cwd = project.get("local_path") if isinstance(project, dict) else None
    timeout = float(verify_cfg.get("timeout_sec", 300.0))

    # 1. URL check: expect_status defaults to 200, detail contains status and up to 500 chars of body
    if "url" in verify_cfg and verify_cfg["url"]:
        url = str(verify_cfg["url"])
        expect_status = int(verify_cfg.get("expect_status", 200))
        try:
            resp = http_get(url)
            status, body = _normalize_http_response(resp)
        except Exception as e:
            status = 0
            body = f"HTTP request failed: {e}"
        url_ok = (status == expect_status)
        url_detail = f"HTTP {status}: {body[:500]}"
        checks.append((url_ok, url_detail))

    # 2. Command check: exit 0 is ok
    if "command" in verify_cfg and verify_cfg["command"]:
        cmd_template = str(verify_cfg["command"])
        cmd = _substitute_placeholders(cmd_template, project=project, env=env, sha=sha)
        try:
            code, stdout, stderr = _call_runner(runner, cmd, cwd, clean_env, timeout)
            if code == 0:
                cmd_ok = True
                cmd_detail = "command exit 0"
            else:
                cmd_ok = False
                err_text = (stderr or stdout).strip()[-500:]
                cmd_detail = f"command exit {code}: {err_text}".strip()
        except Exception as e:
            cmd_ok = False
            cmd_detail = f"command execution failed: {e}"
        checks.append((cmd_ok, cmd_detail))

    # 3. Metric check: stdout parsed as float, <= max is ok
    if "metric" in verify_cfg and isinstance(verify_cfg["metric"], dict):
        metric_cfg = verify_cfg["metric"]
        metric_cmd_template = metric_cfg.get("command")
        if metric_cmd_template:
            cmd = _substitute_placeholders(str(metric_cmd_template), project=project, env=env, sha=sha)
            try:
                code, stdout, stderr = _call_runner(runner, cmd, cwd, clean_env, timeout)
                if code != 0:
                    metric_ok = False
                    err_text = (stderr or stdout).strip()[-500:]
                    metric_detail = f"metric command exit {code}: {err_text}".strip()
                else:
                    try:
                        val = float(stdout.strip())
                    except (ValueError, TypeError):
                        metric_ok = False
                        metric_detail = f"metric output not float: {stdout.strip()[:100]}"
                    else:
                        max_val = metric_cfg.get("max")
                        min_val = metric_cfg.get("min")
                        if max_val is not None and val > float(max_val):
                            metric_ok = False
                            metric_detail = f"metric {val} > {max_val}"
                        elif min_val is not None and val < float(min_val):
                            metric_ok = False
                            metric_detail = f"metric {val} < {min_val}"
                        else:
                            metric_ok = True
                            cond = f" <= {max_val}" if max_val is not None else ""
                            metric_detail = f"metric {val}{cond}"
            except Exception as e:
                metric_ok = False
                metric_detail = f"metric command execution failed: {e}"
            checks.append((metric_ok, metric_detail))

    if not checks:
        return CheckResult(ok=True, detail="")

    all_ok = all(ok for ok, _ in checks)
    joined_detail = "; ".join(detail for _, detail in checks if detail)
    return CheckResult(ok=all_ok, detail=joined_detail)


def advance(
    progress: VerifyProgress,
    result: CheckResult,
    *,
    now: float,
    cfg: dict | None = None,
) -> tuple[VerifyProgress, Literal["running", "passed", "failed"]]:
    """Advance verify progress with latest check result.

    Consecutive failures reaching fail_after -> failed.
    now - started_at >= duration_sec without reaching fail_after -> passed.
    Otherwise -> running.
    If no verify cfg is given, passes immediately.
    """
    progress.last_check_at = now
    progress.checks += 1
    progress.last_detail = result.detail
    if result.ok:
        progress.consecutive_failures = 0
    else:
        progress.consecutive_failures += 1

    if not cfg:
        return progress, "passed"

    fail_after = int(cfg.get("fail_after", 2))
    if progress.consecutive_failures >= fail_after:
        return progress, "failed"

    duration_sec = float(cfg.get("duration_sec", 300.0))
    if (now - progress.started_at) >= duration_sec:
        return progress, "passed"

    return progress, "running"


def due(
    progress: VerifyProgress,
    *,
    now: float,
    cfg: dict | None = None,
) -> bool:
    """Return whether another verification check is due."""
    if progress.last_check_at is None:
        return True
    interval_sec = float(cfg.get("interval_sec", 30.0)) if cfg else 30.0
    return (now - progress.last_check_at) >= interval_sec
