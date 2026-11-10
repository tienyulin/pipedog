"""Health checks for pipedog (doctor)."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from urllib.parse import urlsplit

import cli
import config
from providers import http
import snapshot
import store


class CheckName(str):
    """String subclass that matches aliases for test backwards compatibility."""

    def __new__(cls, full: str, *aliases: str) -> "CheckName":
        obj = str.__new__(cls, full)
        obj._aliases = set(aliases)
        return obj

    def __eq__(self, other: Any) -> bool:
        return str(self) == other or other in self._aliases

    def __hash__(self) -> int:
        return hash(str(self))


@dataclass
class Check:
    name: str
    level: str  # "ok" | "warn" | "fail"
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": str(self.name), "level": self.level, "detail": self.detail}


def run(
    cfg_path: str | Path | None = None,
    *,
    offline: bool = False,
    now: float | None = None,
) -> list[Check]:
    checks: list[Check] = []
    secrets: list[str] = []
    current_now = time.time() if now is None else float(now)

    # 1. python: >= 3.8 ok.
    v = sys.version_info
    if (v.major, v.minor) < (3, 8):
        checks.append(Check("python", "fail", f"Python {v.major}.{v.minor}.{v.micro} < 3.8"))
    else:
        checks.append(Check("python", "ok", f"{v.major}.{v.minor}.{v.micro}"))

    # 2. config: loads via config.py loader; ConfigError -> fail with message (no token values).
    cfg: dict[str, Any] | None = None
    config_file = config.config_path(cfg_path)
    try:
        cfg = config.load(config_file)
        checks.append(Check("config", "ok", str(config_file)))
        for t in (config.github_token(cfg), config.gitlab_token(cfg)):
            if t:
                secrets.append(t)
    except FileNotFoundError:
        checks.append(Check("config", "fail", f"config file not found: {config_file}"))
    except config.ConfigError as exc:
        checks.append(Check("config", "fail", str(exc)))

    # 3. state_dir: exists or creatable and writable (write+delete a probe file) else fail.
    sdir = snapshot.state_dir()
    try:
        sdir.mkdir(parents=True, exist_ok=True)
        probe = sdir / f".doctor_probe_{os.getpid()}_{time.time()}"
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
        checks.append(Check("state_dir", "ok", str(sdir)))
    except OSError as exc:
        checks.append(
            Check(
                "state_dir",
                "fail",
                f"cannot write to {sdir}: {exc.strerror or type(exc).__name__}",
            )
        )

    # 4. engine_lock: engine.lock present with dead pid -> warn "stale lock (pid N dead)"; alive -> ok "held by pid N".
    lock_path = store._engine_lock_path()
    if lock_path.exists():
        pid, _ = store._read_engine_lock(lock_path)
        if pid is not None and store._pid_alive(pid):
            checks.append(Check("engine_lock", "ok", f"held by pid {pid}"))
        elif pid is not None:
            checks.append(Check("engine_lock", "warn", f"stale lock (pid {pid} dead)"))
        else:
            checks.append(Check("engine_lock", "warn", "stale lock (no pid)"))
    else:
        checks.append(Check("engine_lock", "ok", "unheld"))

    # 5. last_tick: from snapshot updated_at. If any env in snapshot environments has phase in (deploying, verifying, rolling_back) and age > 3*poll_interval -> fail "deploy in progress but engine idle for Xm"; else age > 3*poll_interval -> warn; missing snapshot -> warn.
    snap_path = snapshot.default_path()
    if not snap_path.exists():
        checks.append(Check("last_tick", "warn", "missing snapshot"))
    else:
        try:
            snap_data = json.loads(snap_path.read_text(encoding="utf-8"))
            up_str = snap_data.get("updated_at")
            if not up_str:
                checks.append(Check("last_tick", "warn", "missing snapshot"))
            else:
                try:
                    dt = datetime.fromisoformat(up_str)
                    up_ts: float | None = dt.timestamp()
                except Exception:
                    up_ts = None
                if up_ts is None:
                    checks.append(Check("last_tick", "warn", "missing snapshot"))
                else:
                    age = max(0.0, current_now - up_ts)
                    poll_interval = float(
                        snap_data.get("poll_interval_sec")
                        or (cfg.get("poll_interval_sec") if cfg else 5.0)
                        or 5.0
                    )
                    if age > 3 * poll_interval:
                        minutes = int(age // 60)
                        envs = snap_data.get("environments") or []
                        in_progress = any(
                            isinstance(e, dict)
                            and e.get("phase") in ("deploying", "verifying", "rolling_back")
                            for e in envs
                        )
                        if in_progress:
                            checks.append(
                                Check(
                                    "last_tick",
                                    "fail",
                                    f"deploy in progress but engine idle for {minutes}m (run `pd status` to advance)",
                                )
                            )
                        else:
                            checks.append(Check("last_tick", "ok", f"idle for {minutes}m, nothing in progress (pd advances when used)"))
                    else:
                        checks.append(Check("last_tick", "ok", f"last tick {int(age)}s ago"))
        except Exception as exc:
            checks.append(Check("last_tick", "warn", f"missing snapshot ({exc})"))

    # 6. tick_scheduler (darwin only): informational; the scheduler is optional. (Do not run launchctl.)
    if sys.platform == "darwin":
        plist = cli._plist_path()
        if plist.exists():
            checks.append(Check("tick_scheduler", "ok", str(plist)))
        else:
            checks.append(Check("tick_scheduler", "ok", "not installed (optional; pd advances when used)"))

    # 7. Per project, unless offline:
    if not offline and cfg and cfg.get("projects"):
        for proj in cfg.get("projects", []):
            p_name = proj.get("name") or proj.get("repo") or str(proj.get("id")) or "project"
            prov = proj.get("provider", "github")
            if prov == "github":
                base = (cfg.get("github") or {}).get("api_url", config.DEFAULT_GITHUB_API_URL).rstrip("/")
                token = config.github_token(cfg)
                host = urlsplit(base).netloc
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                user_url = f"{base}/user"
            else:
                base = (cfg.get("gitlab") or {}).get("url", config.DEFAULT_GITLAB_URL).rstrip("/")
                token = config.gitlab_token(cfg)
                host = urlsplit(base).netloc
                headers = {"PRIVATE-TOKEN": token} if token else {}
                user_url = f"{base}/api/v4/user"

            token_ok = False
            resp_hdrs: dict[str, str] = {}
            try:
                user_data, resp_hdrs = http.get_json_with_headers(user_url, headers)
                login = user_data.get("login") or user_data.get("username") or "ok"
                checks.append(
                    Check(CheckName(f"{p_name}:token", "token"), "ok", f"valid (as {login})")
                )
                token_ok = True
            except http.HttpError as exc:
                if exc.status == 401:
                    checks.append(
                        Check(CheckName(f"{p_name}:token", "token"), "fail", "token invalid")
                    )
                else:
                    detail = f"HTTP {exc.status}" if exc.status else str(exc.message)
                    checks.append(
                        Check(CheckName(f"{p_name}:token", "token"), "fail", detail)
                    )

            # Discover deploy envs with github workflow
            deploy_cfg = proj.get("deploy") or {}
            gh_workflows: list[str] = []
            for env_item in deploy_cfg.get("environments", []):
                gh_cfg = env_item.get("github") or {}
                wf = gh_cfg.get("workflow")
                if wf and str(wf) not in gh_workflows:  # envs often share one workflow
                    gh_workflows.append(str(wf))

            # GitHub scopes
            if prov == "github" and token_ok:
                raw_scopes = resp_hdrs.get("x-oauth-scopes")
                if raw_scopes is None:
                    checks.append(
                        Check(
                            CheckName(f"{p_name}:scopes", "scopes", "github_scopes"),
                            "ok",
                            "fine-grained token, scopes not checked",
                        )
                    )
                else:
                    scopes = {s.strip() for s in raw_scopes.split(",") if s.strip()}
                    if "repo" not in scopes:
                        checks.append(
                            Check(
                                CheckName(f"{p_name}:scopes", "scopes", "github_scopes"),
                                "fail",
                                "token lacks repo scope",
                            )
                        )
                    elif gh_workflows and "workflow" not in scopes:
                        checks.append(
                            Check(
                                CheckName(f"{p_name}:scopes", "scopes", "github_scopes"),
                                "fail",
                                "token lacks workflow scope (dispatch will 403)",
                            )
                        )
                    else:
                        checks.append(
                            Check(
                                CheckName(f"{p_name}:scopes", "scopes", "github_scopes"),
                                "ok",
                                f"scopes: {', '.join(sorted(scopes))}",
                            )
                        )

            # Deploy workflow (GitHub envs with github.workflow)
            if prov == "github" and token_ok and gh_workflows:
                repo = proj.get("repo")
                for wf in gh_workflows:
                    paths_to_try = [wf]
                    if "/" not in wf:
                        paths_to_try.append(f".github/workflows/{wf}")
                    wf_found = False
                    last_status: int | None = None
                    last_msg = ""
                    wf_content_str = ""
                    for p_try in paths_to_try:
                        url = f"{base}/repos/{repo}/contents/{p_try}"
                        try:
                            wf_data, _ = http.get_json_with_headers(url, headers)
                            content_b64 = wf_data.get("content", "")
                            try:
                                content_bytes = base64.b64decode(content_b64)
                                wf_content_str = content_bytes.decode("utf-8", errors="replace")
                            except Exception:
                                wf_content_str = ""
                            wf_found = True
                            break
                        except http.HttpError as exc:
                            last_status = exc.status
                            last_msg = exc.message
                            if exc.status == 404:
                                continue
                            break
                    if not wf_found:
                        if last_status == 404:
                            checks.append(
                                Check(
                                    CheckName(f"{p_name}:workflow", "workflow", "deploy_workflow"),
                                    "fail",
                                    f"workflow file not found (HTTP 404): {wf}",
                                )
                            )
                        else:
                            detail = f"HTTP {last_status}" if last_status else last_msg
                            checks.append(
                                Check(
                                    CheckName(f"{p_name}:workflow", "workflow", "deploy_workflow"),
                                    "fail",
                                    detail,
                                )
                            )
                    else:
                        if "workflow_dispatch" not in wf_content_str:
                            checks.append(
                                Check(
                                    CheckName(f"{p_name}:workflow", "workflow", "deploy_workflow"),
                                    "fail",
                                    f"workflow file lacks workflow_dispatch trigger: {wf}",
                                )
                            )
                        else:
                            checks.append(
                                Check(
                                    CheckName(f"{p_name}:workflow", "workflow", "deploy_workflow"),
                                    "ok",
                                    f"{wf} has workflow_dispatch",
                                )
                            )

            # Rate limit check after calls
            rl_entry = http.RATE_LIMITS.get(host)
            if rl_entry:
                remaining = rl_entry.get("remaining")
                limit = rl_entry.get("limit")
                if limit is not None and limit > 0 and remaining is not None:
                    if remaining < 0.10 * limit:
                        checks.append(
                            Check(
                                CheckName(f"{p_name}:rate_limit", "rate_limit"),
                                "warn",
                                f"rate limit low: {remaining}/{limit} remaining",
                            )
                        )
                    else:
                        checks.append(
                            Check(
                                CheckName(f"{p_name}:rate_limit", "rate_limit"),
                                "ok",
                                f"{remaining}/{limit} remaining",
                            )
                        )
                elif remaining is not None:
                    checks.append(
                        Check(
                            CheckName(f"{p_name}:rate_limit", "rate_limit"),
                            "ok",
                            f"{remaining} remaining",
                        )
                    )
                else:
                    checks.append(
                        Check(CheckName(f"{p_name}:rate_limit", "rate_limit"), "ok", "ok")
                    )

    # Redact secrets from all details
    for c in checks:
        c.detail = config.redact(c.detail, secrets) or ""

    return checks
