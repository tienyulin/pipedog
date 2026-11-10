"""Initialize a project entry in pipedog config from a local git repository."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

import config

Runner = Callable[..., Any]
git_runner: Runner = subprocess.run


class AlreadyConfigured(Exception):
    """Raised when a project is already configured and --force was not given."""

    def __init__(self, name: str, identifier: str = "") -> None:
        self.name = name
        self.identifier = identifier
        msg = f"{name} is already configured"
        super().__init__(msg)


def parse_remote(url: str) -> dict[str, Any] | None:
    """Parse a git remote URL into provider and repo/id.

    Returns:
        {"provider": "github", "repo": "owner/name"} or
        {"provider": "gitlab", "host": "gitlab.example.com", "id": "group/sub/name"}
    or None if unrecognized.
    Credentials are dropped and never retained.
    """
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url:
        return None

    host = ""
    path = ""

    if "://" in url:
        try:
            parsed = urllib.parse.urlsplit(url)
        except Exception:
            return None
        host = parsed.hostname or ""
        path = parsed.path
    elif ":" in url:
        user_host, path = url.split(":", 1)
        if "@" in user_host:
            host = user_host.rsplit("@", 1)[1]
        else:
            host = user_host
    else:
        return None

    host = host.lower().strip()
    path = path.strip().strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    path = path.rstrip("/")

    if not path or not host:
        return None

    if "github" in host:
        parts = path.split("/")
        if len(parts) < 2:
            return None
        repo = f"{parts[0]}/{parts[1]}"
        return {"provider": "github", "repo": repo}
    elif "gitlab" in host:
        parts = path.split("/")
        if len(parts) < 2:
            return None
        return {"provider": "gitlab", "host": host, "id": path}
    else:
        return None


def detect(path: Path | str, run: Runner | None = None) -> dict[str, Any]:
    """Inspect local git repository at path for remote URL, default branch, develop branch."""
    runner = run if run is not None else git_runner
    path_str = str(path)

    # 1. origin URL
    res_url = runner(
        ["git", "-C", path_str, "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    origin_url = (res_url.stdout or "").strip() if res_url.returncode == 0 else ""
    if origin_url and "@" in origin_url and "://" in origin_url:
        try:
            parts = urllib.parse.urlsplit(origin_url)
            netloc = parts.hostname or ""
            if parts.port:
                netloc = f"{netloc}:{parts.port}"
            origin_url = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
        except Exception:
            pass
    remote_info = parse_remote(origin_url) if origin_url else None

    # 2. default branch:
    # `git symbolic-ref --short refs/remotes/origin/HEAD` -> strip `origin/`;
    # fallback `main` if exists in `git branch -r`, else `master`
    default_branch = ""
    res_head = runner(
        ["git", "-C", path_str, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res_head.returncode == 0 and (res_head.stdout or "").strip():
        head_ref = (res_head.stdout or "").strip()
        if head_ref.startswith("origin/"):
            default_branch = head_ref[len("origin/"):]
        else:
            default_branch = head_ref
    else:
        res_br = runner(
            ["git", "-C", path_str, "branch", "-r"],
            capture_output=True,
            text=True,
            check=False,
        )
        branch_lines = (res_br.stdout or "").splitlines()
        r_branches = []
        for line in branch_lines:
            cleaned = line.strip()
            if " -> " in cleaned:
                cleaned = cleaned.split(" -> ")[0].strip()
            if cleaned:
                r_branches.append(cleaned)
        if any(b == "origin/main" or b.split("/")[-1] == "main" for b in r_branches):
            default_branch = "main"
        else:
            default_branch = "master"

    # 3. whether origin/develop exists (`git branch -r --list origin/develop`)
    res_dev = runner(
        ["git", "-C", path_str, "branch", "-r", "--list", "origin/develop"],
        capture_output=True,
        text=True,
        check=False,
    )
    has_develop = bool(res_dev.returncode == 0 and (res_dev.stdout or "").strip())

    det: dict[str, Any] = {
        "url": origin_url,
        "default_branch": default_branch,
        "has_develop": has_develop,
        "develop": has_develop,
        "toplevel": path_str,
        "local_path": path_str,
    }
    if remote_info:
        det["provider"] = remote_info["provider"]
        if remote_info["provider"] == "github":
            det["repo"] = remote_info["repo"]
        elif remote_info["provider"] == "gitlab":
            det["host"] = remote_info["host"]
            det["id"] = remote_info["id"]
        det["remote"] = remote_info

    return det


def build_project(
    det: Mapping[str, Any],
    flow_mode: str | None = "auto",
    name: str | None = None,
) -> dict[str, Any]:
    """Construct project configuration dictionary from detected git repository info."""
    provider = det.get("provider")
    if not provider:
        remote = det.get("remote")
        if isinstance(remote, dict):
            provider = remote.get("provider")
        elif det.get("repo"):
            provider = "github"
        elif det.get("id"):
            provider = "gitlab"

    if provider not in ("github", "gitlab"):
        raise config.ConfigError(f"unsupported provider: {provider!r}")

    toplevel = det.get("toplevel") or det.get("local_path") or ""
    ident = det.get("repo") or det.get("id") or ""
    default_name = str(ident).rsplit("/", 1)[-1] if ident else (Path(str(toplevel)).name if toplevel else "")
    proj_name = str(name) if name else default_name

    project: dict[str, Any] = {"provider": provider}
    if provider == "github":
        repo = det.get("repo")
        if not repo and isinstance(det.get("remote"), dict):
            repo = det["remote"].get("repo")
        if not repo:
            raise config.ConfigError("missing repo for github project")
        project["repo"] = str(repo)
    else:
        pid = det.get("id")
        if not pid and isinstance(det.get("remote"), dict):
            pid = det["remote"].get("id")
        if not pid:
            raise config.ConfigError("missing id for gitlab project")
        project["id"] = str(pid)

    project["name"] = proj_name
    project["local_path"] = str(toplevel)
    project["branch"] = "auto"

    mode = (flow_mode or "auto").lower()
    if mode == "auto":
        has_dev = bool(det.get("has_develop") or det.get("develop"))
        actual_flow = "gitflow" if has_dev else "github"
    else:
        actual_flow = mode

    default_branch = det.get("default_branch") or "main"
    if actual_flow == "gitflow":
        project["flow"] = {"chain": ["feature/*", "develop", default_branch]}
    elif actual_flow == "github":
        project["flow"] = {"chain": ["feature/*", default_branch]}
    elif actual_flow == "none":
        pass
    else:
        raise config.ConfigError(f"invalid flow mode: {flow_mode!r}")

    return project


def merge_into_config(
    existing: dict[str, Any] | None,
    project: dict[str, Any],
    gitlab_host: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Merge project into existing config dictionary, preserving existing entries and secrets."""
    config.normalize_project(project)

    if existing is None:
        cfg: dict[str, Any] = {
            "poll_interval_sec": 5,
            "notify": "auto",
            "projects": [project],
        }
    else:
        cfg = dict(existing)
        projects_list = list(cfg.get("projects", []))

        target_provider = project.get("provider")
        target_id = project.get("repo") if target_provider == "github" else project.get("id")

        match_idx = -1
        for idx, p in enumerate(projects_list):
            if not isinstance(p, dict):
                continue
            p_provider = p.get("provider")
            if not p_provider:
                if p.get("repo"):
                    p_provider = "github"
                elif p.get("id") is not None:
                    p_provider = "gitlab"
            p_id = p.get("repo") if p_provider == "github" else p.get("id")
            if p_provider == target_provider and str(p_id) == str(target_id):
                match_idx = idx
                break

        if match_idx >= 0:
            if not force:
                raise AlreadyConfigured(project.get("name", str(target_id)), str(target_id))
            projects_list[match_idx] = project
        else:
            projects_list.append(project)

        cfg["projects"] = projects_list

    other_gitlab = [
        p for p in cfg.get("projects", [])
        if isinstance(p, dict) and p is not project
        and (p.get("provider") == "gitlab" or (not p.get("provider") and p.get("id") is not None))
    ]
    if gitlab_host and gitlab_host.lower() != "gitlab.com" and not cfg.get("gitlab", {}).get("url") and other_gitlab:
        # One gitlab.url serves every gitlab project; switching it would break the existing ones.
        raise config.ConfigError(
            f"config already has gitlab projects on the default host; set gitlab.url to https://{gitlab_host} "
            "by hand if they all live there"
        )
    if gitlab_host and gitlab_host.lower() != "gitlab.com":
        gl = cfg.get("gitlab")
        if gl is None:
            cfg["gitlab"] = {"url": f"https://{gitlab_host}"}
        elif isinstance(gl, dict) and not gl.get("url"):
            new_gl = dict(gl)
            new_gl["url"] = f"https://{gitlab_host}"
            cfg["gitlab"] = new_gl

    return cfg


def cmd_init(args: list[str], run: Runner | None = None) -> int:
    """Execute the pd init subcommand."""
    runner = run if run is not None else git_runner

    path_arg: str | None = None
    flow_arg: str = "auto"
    name_arg: str | None = None
    config_arg: str | None = None
    dry_run: bool = False
    force: bool = False

    idx = 0
    while idx < len(args):
        arg = args[idx]
        if arg == "--dry-run":
            dry_run = True
            idx += 1
        elif arg == "--force":
            force = True
            idx += 1
        elif arg == "--path":
            if idx + 1 >= len(args):
                sys.stderr.write("pd init: --path requires a directory argument\n")
                return 2
            path_arg = args[idx + 1]
            idx += 2
        elif arg.startswith("--path="):
            path_arg = arg.split("=", 1)[1]
            idx += 1
        elif arg == "--flow":
            if idx + 1 >= len(args):
                sys.stderr.write("pd init: --flow requires an argument\n")
                return 2
            flow_arg = args[idx + 1]
            idx += 2
        elif arg.startswith("--flow="):
            flow_arg = arg.split("=", 1)[1]
            idx += 1
        elif arg == "--name":
            if idx + 1 >= len(args):
                sys.stderr.write("pd init: --name requires an argument\n")
                return 2
            name_arg = args[idx + 1]
            idx += 2
        elif arg.startswith("--name="):
            name_arg = arg.split("=", 1)[1]
            idx += 1
        elif arg == "--config":
            if idx + 1 >= len(args):
                sys.stderr.write("pd init: --config requires a path argument\n")
                return 2
            config_arg = args[idx + 1]
            idx += 2
        elif arg.startswith("--config="):
            config_arg = arg.split("=", 1)[1]
            idx += 1
        else:
            sys.stderr.write(f"pd init: unknown option: {arg}\n")
            return 2

    if flow_arg.lower() not in {"auto", "gitflow", "github", "none"}:
        sys.stderr.write(f"pd init: invalid flow: {flow_arg}\n")
        return 2

    target_dir = path_arg if path_arg is not None else str(Path.cwd())
    res_top = runner(
        ["git", "-C", target_dir, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res_top.returncode != 0:
        sys.stderr.write(f"pd init: {target_dir} is not a git repository\n")
        return 2
    toplevel = (res_top.stdout or "").strip()
    if not toplevel:
        sys.stderr.write(f"pd init: {target_dir} is not a git repository\n")
        return 2

    det = detect(toplevel, run=runner)
    if not det.get("provider"):
        sys.stderr.write("pd init: could not detect github or gitlab remote origin\n")
        return 2

    try:
        project = build_project(det, flow_mode=flow_arg, name=name_arg)
        config.normalize_project(project)
    except config.ConfigError as exc:
        sys.stderr.write(f"pd init: {exc}\n")
        return 2

    if dry_run:
        print(json.dumps(project, indent=2, ensure_ascii=False))
        return 0

    cfg_path = config.config_path(config_arg)
    existing_cfg: dict[str, Any] | None = None
    if cfg_path.exists():
        try:
            text = cfg_path.read_text(encoding="utf-8")
            existing_cfg = json.loads(text)
        except Exception as exc:
            sys.stderr.write(f"pd init: cannot read existing config {cfg_path}: {exc}\n")
            return 2

    gitlab_host = det.get("host")
    try:
        merged_cfg = merge_into_config(
            existing_cfg,
            project,
            gitlab_host=gitlab_host,
            force=force,
        )
    except AlreadyConfigured as exc:
        sys.stderr.write(f"pd init: {exc}\n")
        return 3
    except config.ConfigError as exc:
        sys.stderr.write(f"pd init: {exc}\n")
        return 2

    try:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cfg_path.parent / f".{cfg_path.name}.tmp.{os.getpid()}_{time.time()}"
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with open(fd, "w", encoding="utf-8") as f:
                json.dump(merged_cfg, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(str(tmp_path), str(cfg_path))
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise
    except Exception as exc:
        sys.stderr.write(f"pd init: error writing config: {exc}\n")
        return 2

    name = project.get("name", "")
    provider = project.get("provider", "")
    repo_or_id = project.get("repo") if provider == "github" else project.get("id")
    flow_obj = project.get("flow")
    if isinstance(flow_obj, dict) and flow_obj.get("chain"):
        flow_chain = " → ".join(flow_obj["chain"])
    elif isinstance(flow_obj, list):
        flow_chain = " → ".join(flow_obj)
    else:
        flow_chain = "monitor only"

    print(f"pd init: added {name} ({provider} {repo_or_id}, flow: {flow_chain}) to {cfg_path}")
    if provider == "github":
        print("next: set a token — export PIPEDOG_GITHUB_TOKEN=...  (or: gh auth login)")
        print("      then run: pd doctor")
    else:
        print("next: set a token — export PIPEDOG_GITLAB_TOKEN=...  (api scope)")
        print("      then run: pd doctor")

    return 0
