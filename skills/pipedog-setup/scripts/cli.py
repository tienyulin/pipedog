"""CLI subcommands for pipedog (status, wait, ack, decide, decisions, rules, comments, reply, tick, install-tick, uninstall-tick, envs, doctor, init)."""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import tui

import config
import engine
import flowmodel
import forge
import model
import renderer
import rules
import snapshot
import store

SUBCOMMANDS: set[str] = {
    "status",
    "wait",
    "ack",
    "decide",
    "decisions",
    "rules",
    "comments",
    "reply",
    "tick",
    "install-tick",
    "uninstall-tick",
    "envs",
    "doctor",
    "init",
}

VALID_ANSWERS: dict[str, set[str]] = {
    "yellow": {"allow", "once", "fix", "skip"},
    "merge": {"merge", "skip"},
    "promote": {"open", "skip"},
    "deploy": {"deploy", "skip"},
    "deploy_approval": {"approve", "reject", "skip"},
    "rollback": {"rollback", "skip"},
    "deploy_stuck": {"retry", "skip"},
}

LAUNCHD_LABEL = "dev.pipedog.tick"


def _default_launchctl(cmd: list[str]) -> int:
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    return proc.returncode


launchctl_runner: Callable[[list[str]], Any] = _default_launchctl
git_runner: Callable[..., Any] = subprocess.run


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _pd_bin_path() -> Path:
    return (Path(__file__).resolve().parent.parent / "bin" / "pd").resolve()


def _dict_to_job(d: dict[str, Any]) -> model.Job:
    err_d = d.get("error")
    err = model.ErrorInfo(**err_d) if isinstance(err_d, dict) else None
    subs = [_dict_to_job(s) for s in d.get("sub_jobs", []) if isinstance(s, dict)]
    return model.Job(
        name=d.get("name", ""),
        status=d.get("status", "skipped"),
        stage=d.get("stage"),
        job_id=d.get("job_id"),
        web_url=d.get("web_url"),
        started_at=d.get("started_at"),
        finished_at=d.get("finished_at"),
        sub_jobs=subs,
        error=err,
        raw_status=d.get("raw_status"),
        allow_failure=bool(d.get("allow_failure", False)),
        workflow=d.get("workflow"),
    )


def _dict_to_pipeline(d: dict[str, Any]) -> model.Pipeline:
    jobs = [_dict_to_job(j) for j in d.get("jobs", []) if isinstance(j, dict)]
    return model.Pipeline(
        project=d.get("project", ""),
        short=d.get("short", ""),
        provider=d.get("provider", "mock"),
        project_ref=d.get("project_ref", ""),
        local_path=d.get("local_path"),
        branch=d.get("branch"),
        pipeline_id=d.get("pipeline_id"),
        status=d.get("status"),
        web_url=d.get("web_url"),
        jobs=jobs,
        fetch_error=d.get("fetch_error"),
        fetched_at=d.get("fetched_at"),
        head_sha=d.get("head_sha"),
    )


def _forge_for_project(project_name: str) -> tuple[dict[str, Any], forge.Forge]:
    cfg_p = config.config_path()
    cfg = config.load(cfg_p)
    proj = next(
        (
            p
            for p in cfg.get("projects", [])
            if p.get("name") == project_name or p.get("repo") == project_name
        ),
        None,
    )
    if proj is None:
        raise ValueError(f"project {project_name!r} not found in config")
    provider = proj.get("provider", "github")
    if provider == "github":
        token = config.github_token(cfg)
        base_url = cfg.get("github", {}).get("api_url", config.DEFAULT_GITHUB_API_URL)
    else:
        token = config.gitlab_token(cfg)
        base_url = cfg.get("gitlab", {}).get("url", config.DEFAULT_GITLAB_URL)
    f = forge.get_forge(provider, base_url, token)
    return proj, f


MAX_TICK_LOG_SIZE = 1024 * 1024  # 1 MiB
KEEP_TICK_LOG_SIZE = 256 * 1024  # 256 KiB


def _rotate_tick_log(log_path: Path | None = None) -> None:
    if log_path is None:
        log_path = snapshot.state_dir() / "tick.log"
    try:
        if not log_path.exists():
            return
        size = log_path.stat().st_size
        if size > MAX_TICK_LOG_SIZE:
            with open(log_path, "rb") as f:
                f.seek(size - KEEP_TICK_LOG_SIZE)
                tail = f.read(KEEP_TICK_LOG_SIZE)
            fd, tmp = tempfile.mkstemp(
                prefix=f".{log_path.name}.", suffix=".tmp", dir=log_path.parent
            )
            try:
                with os.fdopen(fd, "wb") as f_out:
                    f_out.write(tail)
                    f_out.flush()
                    os.fsync(f_out.fileno())
                os.replace(tmp, log_path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
    except Exception:
        pass


def _tick_once(
    cfg_arg: str | None = None,
    *,
    mock: bool = False,
    interval: float | None = None,
) -> int:
    path = config.config_path(cfg_arg)
    try:
        eng = engine.build(
            cfg_arg,
            mock=mock,
            interval=interval,
            once=True,
            no_notify=True,
            forge_factory=engine.get_forge,
        )
    except FileNotFoundError:
        import monitor

        sys.stderr.write(monitor.missing_config_message(path))
        return 2
    except config.ConfigError as exc:
        sys.stderr.write(f"pipedog: {exc}\n")
        return 2

    try:
        eng.tick()
    finally:
        eng.close()
    return 0


def cmd_status(args: list[str]) -> int:
    cfg_arg: str | None = None
    no_tick = "--no-tick" in args
    is_mock = "--mock" in args
    is_json = "--json" in args

    idx = 0
    while idx < len(args):
        if args[idx] == "--config" and idx + 1 < len(args):
            cfg_arg = args[idx + 1]
            idx += 2
        else:
            idx += 1

    snap_path = snapshot.default_path()

    should_tick = False
    if not no_tick and not is_mock:
        if not snap_path.exists():
            should_tick = True
        else:
            try:
                raw_text = snap_path.read_text(encoding="utf-8")
                data_peek = json.loads(raw_text)
            except Exception:
                data_peek = None

            if data_peek is None or not isinstance(data_peek, dict):
                should_tick = True
            elif not data_peek.get("mock", False):
                interval = float(data_peek.get("poll_interval_sec") or 5.0)
                stale_thresh = float(
                    data_peek.get("stale_after_sec") or snapshot.stale_after(interval)
                )
                updated_ts: float | None = None
                updated_at_str = data_peek.get("updated_at")
                if updated_at_str:
                    try:
                        updated_ts = datetime.fromisoformat(updated_at_str).timestamp()
                    except Exception:
                        pass
                if updated_ts is None:
                    try:
                        updated_ts = snap_path.stat().st_mtime
                    except Exception:
                        updated_ts = 0.0

                now = time.time()
                is_stale = (now - updated_ts) > stale_thresh

                envs = data_peek.get("environments") or []
                has_inflight = any(
                    isinstance(e, dict)
                    and e.get("phase") in ("deploying", "verifying", "rolling_back")
                    for e in envs
                )

                if is_stale or has_inflight:
                    should_tick = True

    if should_tick:
        try:
            res = _tick_once(cfg_arg)
            if res != 0:
                pass
        except Exception as exc:
            sys.stderr.write(f"pipedog: tick failed: {exc}\n")

    if not snap_path.exists():
        sys.stderr.write("snapshot not found\n")
        return 2

    try:
        text = snap_path.read_text(encoding="utf-8")
        data = json.loads(text)
    except Exception as e:
        sys.stderr.write(f"cannot read snapshot: {e}\n")
        return 2

    if is_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    try:
        pipelines = [
            _dict_to_pipeline(p) for p in data.get("pipelines", []) if isinstance(p, dict)
        ]
        caps = renderer.detect_caps()
        now = time.time()
        updated_at_str = data.get("updated_at")
        updated_ts = None
        if updated_at_str:
            try:
                updated_ts = datetime.fromisoformat(updated_at_str).timestamp()
            except Exception:
                pass
        mock = bool(data.get("mock", False))
        envs = data.get("environments") or ()
        renderable = renderer.render(
            pipelines,
            now=now,
            caps=caps,
            updated_at=updated_ts,
            mock=mock,
            environments=envs,
            collapse_passed=False,
        )
        tui.print_view(renderable)
    except Exception:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_wait(args: list[str]) -> int:
    timeout: float | None = None
    cfg_arg: str | None = None
    mock = False
    interval_arg: float | None = None
    idx = 0
    while idx < len(args):
        if args[idx] == "--timeout" and idx + 1 < len(args):
            try:
                timeout = float(args[idx + 1])
            except ValueError:
                sys.stderr.write("invalid timeout\n")
                return 2
            idx += 2
        elif args[idx] == "--config" and idx + 1 < len(args):
            cfg_arg = args[idx + 1]
            idx += 2
        elif args[idx] == "--mock":
            mock = True
            idx += 1
        elif args[idx] == "--interval" and idx + 1 < len(args):
            try:
                interval_arg = float(args[idx + 1])
                if interval_arg <= 0:
                    raise ValueError
            except ValueError:
                sys.stderr.write("invalid interval\n")
                return 2
            idx += 2
        else:
            idx += 1

    if not store.acquire_wait_lock():
        sys.stderr.write("another process holds the wait lock\n")
        return 5

    try:
        path = config.config_path(cfg_arg)
        try:
            eng = engine.build(
                cfg_arg,
                mock=mock,
                interval=interval_arg,
                no_notify=True,
                forge_factory=engine.get_forge,
            )
        except FileNotFoundError:
            import monitor

            sys.stderr.write(monitor.missing_config_message(path))
            return 2
        except config.ConfigError as exc:
            sys.stderr.write(f"pipedog: {exc}\n")
            return 2

        start_time = time.time()
        try:
            while True:
                eng.tick()
                ev = store.oldest_unacked()
                if ev is not None:
                    print(json.dumps(flowmodel.to_json(ev), ensure_ascii=False))
                    return 0

                if timeout is not None:
                    elapsed = time.time() - start_time
                    if elapsed >= timeout:
                        sys.stderr.write("timeout waiting for event\n")
                        return 4
                    sleep_dur = min(eng.poll_interval, max(0.01, timeout - elapsed))
                else:
                    sleep_dur = eng.poll_interval
                time.sleep(sleep_dur)
        finally:
            eng.close()
    finally:
        store.release_wait_lock()


def cmd_tick(args: list[str]) -> int:
    _rotate_tick_log()
    cfg_arg: str | None = None
    mock = False
    interval_arg: float | None = None
    idx = 0
    while idx < len(args):
        if args[idx] == "--config" and idx + 1 < len(args):
            cfg_arg = args[idx + 1]
            idx += 2
        elif args[idx] == "--mock":
            mock = True
            idx += 1
        elif args[idx] == "--interval" and idx + 1 < len(args):
            try:
                interval_arg = float(args[idx + 1])
                if interval_arg <= 0:
                    raise ValueError
            except ValueError:
                sys.stderr.write("invalid interval\n")
                return 2
            idx += 2
        else:
            idx += 1

    return _tick_once(cfg_arg, mock=mock, interval=interval_arg)


def cmd_install_tick(
    args: list[str],
    *,
    launchctl: Callable[[list[str]], Any] | None = None,
) -> int:
    if sys.platform != "darwin":
        sys.stderr.write("install-tick is only supported on macOS (darwin)\n")
        return 2

    interval = 300
    idx = 0
    while idx < len(args):
        if args[idx] == "--interval" and idx + 1 < len(args):
            try:
                interval = int(args[idx + 1])
                if interval <= 0:
                    raise ValueError
            except ValueError:
                sys.stderr.write("invalid interval\n")
                return 2
            idx += 2
        else:
            idx += 1

    runner = launchctl if launchctl is not None else launchctl_runner
    plist_path = _plist_path()
    state_dir = snapshot.state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    domain = f"gui/{os.getuid()}"
    if plist_path.exists():
        runner(["launchctl", "bootout", domain, str(plist_path)])

    path_env = os.environ.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin"
    log_path = str(state_dir / "tick.log")
    plist_data = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [str(_pd_bin_path()), "tick"],
        "StartInterval": interval,
        "RunAtLoad": True,
        "EnvironmentVariables": {"PATH": path_env},
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }
    plist_path.write_bytes(plistlib.dumps(plist_data))
    runner(["launchctl", "bootstrap", domain, str(plist_path)])
    return 0


def cmd_uninstall_tick(
    args: list[str],
    *,
    launchctl: Callable[[list[str]], Any] | None = None,
) -> int:
    if sys.platform != "darwin":
        sys.stderr.write("uninstall-tick is only supported on macOS (darwin)\n")
        return 2

    runner = launchctl if launchctl is not None else launchctl_runner
    plist_path = _plist_path()
    domain = f"gui/{os.getuid()}"
    runner(["launchctl", "bootout", domain, str(plist_path)])
    try:
        plist_path.unlink()
    except FileNotFoundError:
        pass
    return 0


def cmd_ack(args: list[str]) -> int:
    if not args:
        sys.stderr.write("usage: pd ack <event_id> [--handled ID...] [--pushed SHA]\n")
        return 2

    event_id = args[0]
    handled: list[str] = []
    pushed: str | None = None
    idx = 1
    while idx < len(args):
        if args[idx] == "--handled":
            idx += 1
            while idx < len(args) and not args[idx].startswith("-"):
                handled.append(args[idx])
                idx += 1
        elif args[idx] == "--pushed" and idx + 1 < len(args):
            pushed = args[idx + 1]
            idx += 2
        else:
            idx += 1

    try:
        store.ack_event(event_id, handled=handled or None, pushed=pushed)
        return 0
    except store.StoreError as e:
        sys.stderr.write(f"{e}\n")
        return 2


def _get_chain_for_project(project_name: str, is_mock: bool = False) -> list[str]:
    if is_mock:
        try:
            import mock
            for p in getattr(mock, "_PROJECTS", []):
                if p.get("name") == project_name or p.get("repo") == project_name:
                    return list(p.get("flow", {}).get("chain") or [])
        except Exception:
            pass
    try:
        cfg = config.load(config.config_path())
        for p in cfg.get("projects", []):
            if p.get("name") == project_name or p.get("repo") == project_name:
                return list(p.get("flow", {}).get("chain") or [])
    except Exception:
        pass
    return []


def _find_pipeline_url(snap: dict[str, Any], project_name: str, head_sha: str) -> str | None:
    for p in snap.get("pipelines", []):
        if isinstance(p, dict):
            if (p.get("project") == project_name or p.get("short") == project_name) and p.get("head_sha") == head_sha:
                url = p.get("web_url")
                if url:
                    return str(url)
    for p in snap.get("pipelines", []):
        if isinstance(p, dict) and (p.get("project") == project_name or p.get("short") == project_name):
            url = p.get("web_url")
            if url:
                return str(url)
    return None


def apply_decision(
    decision_id: str,
    answer: str,
    *,
    remember: bool = False,
    root: Path | None = None,
    rules_path: Path | None = None,
    answered_by: str = "cli",
) -> flowmodel.Decision:
    """Validate and record an answer for a pending decision, adding rules if requested."""
    d = store.get_decision(decision_id, root=root)
    if d is None:
        raise ValueError(f"decision {decision_id} not found")
    if d.status != "pending":
        raise ValueError(f"decision {decision_id} is {d.status}")

    valid = VALID_ANSWERS.get(d.kind, set())
    if answer not in valid:
        raise ValueError(
            f"answer {answer!r} is not valid for {d.kind!r} (must be one of {', '.join(sorted(valid))})"
        )

    updated = store.answer_decision(
        decision_id,
        answer=answer,
        remember=remember,
        root=root,
        answered_by=answered_by,
    )
    if answer == "allow" and remember:
        snap = None
        try:
            snap_p = (
                root / snapshot.FILENAME
                if root is not None
                else snapshot.default_path()
            )
            if snap_p.exists():
                snap = json.loads(snap_p.read_text(encoding="utf-8"))
        except Exception:
            pass
        is_mock = snap.get("mock", False) if isinstance(snap, dict) else False

        rpath = rules_path
        if rpath is None:
            rpath = rules.rules_path(mock=is_mock, root=root)

        import flow

        for reason in d.reasons:
            transition = reason.get("transition")
            if not transition:
                chain = _get_chain_for_project(d.project, is_mock=is_mock)
                transition = flow.transition_of(chain, d.source, d.target)

            source_run = reason.get("web_url") or reason.get("pipeline_url")
            if not source_run and isinstance(snap, dict):
                source_run = _find_pipeline_url(snap, d.project, d.head_sha)
            if not source_run:
                source_run = d.head_sha

            rules.add(
                rpath,
                {
                    "action": "allow",
                    "project": d.project,
                    "transition": transition,
                    "job": reason.get("job", "*"),
                    "kind": reason.get("kind", "*"),
                    "source_run": source_run,
                },
            )
    return updated


def cmd_decide(args: list[str]) -> int:
    if len(args) < 2:
        sys.stderr.write("usage: pd decide <id> <answer> [--remember]\n")
        return 2

    decision_id = args[0]
    answer = args[1]
    remember = "--remember" in args[2:] or "-r" in args[2:]

    try:
        apply_decision(decision_id, answer, remember=remember)
        return 0
    except ValueError as e:
        sys.stderr.write(f"{e}\n")
        return 2
    except store.StoreError as e:
        sys.stderr.write(f"{e}\n")
        return 2


def cmd_decisions(args: list[str]) -> int:
    is_history = "--history" in args
    if not is_history:
        is_json = "--json" in args
        decisions = store.list_decisions(status="pending")
        if is_json:
            for d in decisions:
                print(json.dumps(flowmodel.to_json(d), ensure_ascii=False))
        else:
            if not decisions:
                print("no pending decisions")
            else:
                for d in decisions:
                    print(f"[{d.id}] {d.kind} {d.project} {d.source}→{d.target} ({d.head_sha[:7]})")
                    why = renderer._format_decision_summary(d)
                    if why:
                        print(f"    {why}")
        return 0

    is_json = "--json" in args
    limit = 20
    project_filter: str | None = None

    idx = 0
    while idx < len(args):
        arg = args[idx]
        if arg in ("--history", "--json"):
            idx += 1
        elif arg == "--limit" and idx + 1 < len(args):
            try:
                limit = int(args[idx + 1])
            except ValueError:
                sys.stderr.write("invalid limit\n")
                return 2
            idx += 2
        elif arg.startswith("--limit="):
            try:
                limit = int(arg.split("=", 1)[1])
            except ValueError:
                sys.stderr.write("invalid limit\n")
                return 2
            idx += 1
        elif arg == "--project" and idx + 1 < len(args):
            project_filter = args[idx + 1]
            idx += 2
        elif arg.startswith("--project="):
            project_filter = arg.split("=", 1)[1]
            idx += 1
        else:
            idx += 1

    history = store.list_decision_history(project=project_filter, limit=limit)
    if is_json:
        print(json.dumps([flowmodel.to_json(d) for d in history], ensure_ascii=False))
        return 0

    if not history:
        print("no decision history")
        return 0

    arrow_sym = "->" if os.environ.get("PIPEDOG_ASCII") == "1" else "→"
    for d in history:
        ts = d.answered_at if d.answered_at is not None else d.created_at
        time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        kind_short = renderer.KIND_SHORT_LABELS.get(d.kind, d.kind)
        kind_col = f"{kind_short:<7}"
        src_tgt = f"{d.source or ''}{arrow_sym}{d.target or ''}"
        sha7 = (d.head_sha or "")[:7]
        if d.status == "void":
            ans_col = f"void ({d.note})" if d.note else "void"
        else:
            ans_col = d.answer or d.status
        by_col = f"  by {d.answered_by}" if d.answered_by else ""
        print(f"{time_str}  {kind_col}  {d.project}  {src_tgt}  {sha7}  {ans_col}{by_col}")

    return 0


def cmd_rules(args: list[str]) -> int:
    snap = None
    try:
        snap_p = snapshot.default_path()
        if snap_p.exists():
            snap = json.loads(snap_p.read_text(encoding="utf-8"))
    except Exception:
        pass
    is_mock = snap.get("mock", False) if isinstance(snap, dict) else False
    rpath = rules.rules_path(mock=is_mock)
    rules_list, err = rules.load(rpath)
    if err is not None:
        sys.stderr.write(f"{err}\n")
        return 2

    if not args or args[0] == "list":
        is_json = "--json" in args
        if is_json:
            for r in rules_list:
                print(json.dumps(flowmodel.to_json(r), ensure_ascii=False))
        else:
            if not rules_list:
                print("no rules")
            else:
                for r in rules_list:
                    print(f"[{r.id}] {r.action} {r.project} {r.transition} {r.job} {r.kind}")
        return 0

    if args[0] == "rm":
        if len(args) < 2:
            sys.stderr.write("usage: pd rules rm <id>\n")
            return 2
        rule_id = args[1]
        matching = [r for r in rules_list if r.id == rule_id]
        if not matching:
            sys.stderr.write(f"rule {rule_id!r} not found\n")
            return 2
        with store.state_lock(Path(rpath).name, root=Path(rpath).parent):
            current, _err = rules.load(Path(rpath))
            remaining = [r for r in current if r.id != rule_id]
            rules.save(rpath, remaining)
        return 0

    sys.stderr.write(f"unknown rules subcommand: {args[0]}\n")
    return 2


def cmd_comments(args: list[str]) -> int:
    is_json = "--json" in args
    pos = [a for a in args if not a.startswith("-")]
    if len(pos) < 2:
        sys.stderr.write("usage: pd comments <project> <pr> [--json]\n")
        return 2

    project_name = pos[0]
    try:
        pr_number = int(pos[1])
    except ValueError:
        sys.stderr.write("invalid PR number\n")
        return 2

    try:
        proj, f = _forge_for_project(project_name)
        comments = f.list_comments(proj, pr_number)
    except Exception as e:
        sys.stderr.write(f"{e}\n")
        return 2

    seen_ids = store.seen_comments(project_name, pr_number)
    for c in comments:
        c["seen"] = c["id"] in seen_ids

    if is_json:
        for c in comments:
            print(json.dumps(c, ensure_ascii=False))
    else:
        if not comments:
            print("no comments")
        else:
            for c in comments:
                tag = "[seen]" if c.get("seen") else "[new]"
                print(f"{c.get('id', '')} {tag} {c.get('author', '')}: {c.get('body', '')}")
    return 0


def cmd_reply(args: list[str]) -> int:
    if len(args) < 4:
        sys.stderr.write("usage: pd reply <project> <pr> <comment_id> <text>\n")
        return 2

    project_name, pr_str, comment_id = args[0], args[1], args[2]
    text = " ".join(args[3:])
    marker = "<!-- pipedog-reply -->"
    if not text.endswith(marker):
        body = f"{text}\n{marker}"
    else:
        body = text
    try:
        pr_number = int(pr_str)
    except ValueError:
        sys.stderr.write("invalid PR number\n")
        return 2

    try:
        proj, f = _forge_for_project(project_name)
        url = f.reply(proj, pr_number, comment_id, body)
        ref_label = f"MR !{pr_number}" if proj.get("provider") == "gitlab" else f"PR #{pr_number}"
        store.log_action(f"reply {ref_label} {project_name} {comment_id} -> {url}")
        print(url)
        return 0
    except Exception as e:
        sys.stderr.write(f"{e}\n")
        return 2


def cmd_envs(args: list[str]) -> int:
    is_json = "--json" in args
    envs: list[dict[str, Any]] = []
    snap_path = snapshot.default_path()
    if snap_path.exists():
        try:
            data = json.loads(snap_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("environments"), list):
                envs = list(data["environments"])
        except Exception:
            pass

    if not envs:
        try:
            import deploystore

            for st in deploystore.load_states().values():
                envs.append(
                    {
                        "project": st.project,
                        "env": st.env,
                        "sha": st.sha,
                        "status": (
                            "stuck"
                            if st.phase == "stuck"
                            else ("failed" if st.phase == "failed" else "success" if st.verified_sha else "none")
                        ),
                        "verified": bool(st.verified_sha and st.sha == st.verified_sha),
                        "verified_sha": st.verified_sha,
                        "deployed_at": None,
                        "web_url": None,
                        "production": st.env.lower() in ("prod", "production"),
                    }
                )
        except Exception:
            pass

    if is_json:
        print(json.dumps(envs, ensure_ascii=False))
    else:
        if not envs:
            print("no environments")
        else:
            for e in envs:
                sha7 = (e.get("sha") or "-")[:7]
                status = "verified" if e.get("verified") else e.get("status", "none")
                print(f"{e.get('project', '')} › {e.get('env', '')}  {sha7}  {status}")
    return 0


def cmd_doctor(args: list[str]) -> int:
    is_json = False
    offline = False
    cfg_arg: str | None = None
    idx = 0
    while idx < len(args):
        if args[idx] == "--json":
            is_json = True
            idx += 1
        elif args[idx] == "--offline":
            offline = True
            idx += 1
        elif args[idx] == "--config" and idx + 1 < len(args):
            cfg_arg = args[idx + 1]
            idx += 2
        elif args[idx].startswith("--config="):
            cfg_arg = args[idx].split("=", 1)[1]
            idx += 1
        else:
            idx += 1

    try:
        import doctor

        checks = doctor.run(cfg_path=cfg_arg, offline=offline)
    except Exception as exc:
        msg = str(exc)
        try:
            cfg_p = config.config_path(cfg_arg)
            cfg_dict = config.load(cfg_p)
            secrets = [
                t
                for t in (
                    config.github_token(cfg_dict),
                    config.gitlab_token(cfg_dict),
                )
                if t
            ]
            msg = config.redact(msg, secrets) or msg
        except Exception:
            msg = config.redact(msg, []) or msg
        sys.stderr.write(f"doctor error: {msg}\n")
        return 3

    if is_json:
        print(json.dumps([c.to_dict() for c in checks], indent=2, ensure_ascii=False))
    else:
        is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
        for c in checks:
            if c.level == "ok":
                tag = tui.format_styled("[ OK ]", "green") if is_tty else "[ OK ]"
            elif c.level == "warn":
                tag = tui.format_styled("[WARN]", "yellow") if is_tty else "[WARN]"
            else:
                tag = tui.format_styled("[FAIL]", "red") if is_tty else "[FAIL]"
            print(f"{tag} {c.name} \u2014 {c.detail}")

    if any(c.level == "fail" for c in checks):
        return 2
    if any(c.level == "warn" for c in checks):
        return 1
    return 0


def cmd_init(args: list[str], *, run: Callable[..., Any] | None = None) -> int:
    import init_cmd

    runner = run if run is not None else git_runner
    return init_cmd.cmd_init(args, run=runner)


def main(
    argv: list[str] | None = None,
    *,
    launchctl: Callable[[list[str]], Any] | None = None,
    run: Callable[..., Any] | None = None,
) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if not argv or argv[0] not in SUBCOMMANDS:
        import monitor

        return monitor.main(argv)

    cmd = argv[0]
    args = argv[1:]
    if cmd == "status":
        return cmd_status(args)
    if cmd == "wait":
        return cmd_wait(args)
    if cmd == "tick":
        return cmd_tick(args)
    if cmd == "install-tick":
        return cmd_install_tick(args, launchctl=launchctl)
    if cmd == "uninstall-tick":
        return cmd_uninstall_tick(args, launchctl=launchctl)
    if cmd == "ack":
        return cmd_ack(args)
    if cmd == "decide":
        return cmd_decide(args)
    if cmd == "decisions":
        return cmd_decisions(args)
    if cmd == "rules":
        return cmd_rules(args)
    if cmd == "comments":
        return cmd_comments(args)
    if cmd == "reply":
        return cmd_reply(args)
    if cmd == "envs":
        return cmd_envs(args)
    if cmd == "doctor":
        return cmd_doctor(args)
    if cmd == "init":
        runner = run if run is not None else git_runner
        return cmd_init(args, run=runner)
    return 2


if __name__ == "__main__":
    sys.exit(main())
