"""Configuration loading, token resolution and branch detection.

See docs/DESIGN.md section 4. Tokens are resolved env-first; nothing in this
module ever prints or logs a token value.
"""

from __future__ import annotations

import base64
import dataclasses
import fnmatch
import ipaddress
import json
import os
import re
import subprocess
import urllib.parse
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Callable

import deploymodel

DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_GITLAB_URL = "https://gitlab.com"
DEFAULT_GITHUB_API_URL = "https://api.github.com"
NOTIFY_MODES: tuple[str, ...] = ("auto", "osc9", "osascript", "bell", "off")
PROVIDERS: tuple[str, ...] = ("gitlab", "github")
VALID_DEPLOY_ADAPTERS: frozenset[str] = frozenset({"github", "gitlab", "command"})
VALID_WEBHOOK_FORMATS: frozenset[str] = frozenset({"slack", "discord", "json"})
VALID_WEBHOOK_EVENTS: frozenset[str] = frozenset({"failed", "decision", "deploy"})

_GIT_TIMEOUT = 3.0
_GH_TIMEOUT = 5.0

Runner = Callable[..., subprocess.CompletedProcess]


class ConfigError(Exception):
    """Invalid or unreadable configuration (message is safe to print)."""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def default_config_path() -> Path:
    return Path.home() / ".config" / "pipedog" / "config.json"


def config_path(cli_path: str | os.PathLike[str] | None = None,
                env: Mapping[str, str] | None = None) -> Path:
    """--config PATH > env PIPEDOG_CONFIG > ~/.config/pipedog/config.json."""
    env = os.environ if env is None else env
    if cli_path:
        return Path(cli_path).expanduser()
    if env.get("PIPEDOG_CONFIG"):
        return Path(env["PIPEDOG_CONFIG"]).expanduser()
    return default_config_path()


# ---------------------------------------------------------------------------
# Loading / normalization
# ---------------------------------------------------------------------------


def safe_url(url: str) -> str:
    """Return scheme + host with path redacted, e.g. https://hooks.slack.com/…"""
    if not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(url)
        scheme = parts.scheme or "https"
        netloc = parts.netloc
        if "@" in netloc:
            netloc = netloc.split("@", 1)[1]
        if netloc:
            return f"{scheme}://{netloc}/…"
        return f"{scheme}://…"
    except Exception:
        return "https://…/…"


def _is_private_or_local_host(hostname: str | None) -> bool:
    if not hostname:
        return True
    host = hostname.strip().lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    if host in ("::1", "[::1]"):
        return True
    raw_ip = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ip = ipaddress.ip_address(raw_ip)
        if ip.is_loopback or ip.is_private or ip.is_link_local:
            return True
    except ValueError:
        pass
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        first = int(parts[0])
        second = int(parts[1])
        if first == 127:
            return True
        if first == 10:
            return True
        if first == 192 and second == 168:
            return True
        if first == 169 and second == 254:
            return True
        if first == 172 and (16 <= second <= 31):
            return True
    return False


def _normalize_webhook(item: Any, env: Mapping[str, str], index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ConfigError(f"webhooks[{index}] must be an object")

    fmt = item.get("format")
    if not isinstance(fmt, str) or fmt.strip().lower() not in VALID_WEBHOOK_FORMATS:
        raise ConfigError(
            f"webhooks[{index}].format must be one of {', '.join(sorted(VALID_WEBHOOK_FORMATS))}"
        )
    fmt = fmt.strip().lower()

    events_raw = item.get("events")
    if not isinstance(events_raw, list) or not events_raw:
        raise ConfigError(f"webhooks[{index}].events must be a non-empty list")
    events: list[str] = []
    for ev in events_raw:
        if not isinstance(ev, str) or ev.strip().lower() not in VALID_WEBHOOK_EVENTS:
            raise ConfigError(
                f"webhooks[{index}].events contains unknown event {ev!r}; "
                f"must be one of {', '.join(sorted(VALID_WEBHOOK_EVENTS))}"
            )
        events.append(ev.strip().lower())

    url = None
    url_env = None
    if "url_env" in item and item["url_env"]:
        if not isinstance(item["url_env"], str):
            raise ConfigError(f"webhooks[{index}].url_env must be a string")
        url_env = item["url_env"].strip()
        env_val = env.get(url_env)
        if env_val and str(env_val).strip():
            url = str(env_val).strip()
        elif "url" not in item:
            raise ConfigError(f"environment variable {url_env!r} is not set or empty")

    if not url and "url" in item and item["url"]:
        if not isinstance(item["url"], str):
            raise ConfigError(f"webhooks[{index}].url must be a string")
        url = item["url"].strip()

    if not url:
        raise ConfigError(f"webhooks[{index}] requires 'url' or 'url_env'")

    if not url.startswith("https://"):
        raise ConfigError(f"webhook URL must start with 'https://' ({safe_url(url)})")

    parts = urllib.parse.urlsplit(url)
    hostname = parts.hostname
    if not hostname or _is_private_or_local_host(hostname):
        raise ConfigError(f"webhook URL host not allowed: {safe_url(url)}")

    res: dict[str, Any] = {
        "url": url,
        "format": fmt,
        "events": events,
    }
    if url_env:
        res["url_env"] = url_env
    return res


def load(path: Path, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Read and normalize a config file.

    Raises FileNotFoundError when the file does not exist and ConfigError for
    any other problem (bad JSON, bad structure).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or type(exc).__name__}") from None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        # Only position info; never echo file content (it may hold a token).
        raise ConfigError(f"invalid JSON in {path}: line {exc.lineno} column {exc.colno}") from None
    return normalize(raw, env=env)


def normalize(raw: Any, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Fill defaults and the legacy top-level keys; validate projects."""
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a JSON object")

    eff_env = os.environ if env is None else env

    gitlab = raw.get("gitlab") or {}
    github = raw.get("github") or {}
    if not isinstance(gitlab, dict) or not isinstance(github, dict):
        raise ConfigError("'gitlab' / 'github' must be objects")

    # Legacy spec keys: gitlab_url / private_token.
    gitlab_url = gitlab.get("url") or raw.get("gitlab_url") or DEFAULT_GITLAB_URL
    gitlab_token = gitlab.get("token") or raw.get("private_token") or ""
    github_url = github.get("api_url") or DEFAULT_GITHUB_API_URL
    github_token = github.get("token") or ""

    interval = raw.get("poll_interval_sec", DEFAULT_POLL_INTERVAL)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval <= 0:
        raise ConfigError("'poll_interval_sec' must be a positive number")

    notify = raw.get("notify", "auto")
    if notify is False:
        notify = "off"
    if notify not in NOTIFY_MODES:
        raise ConfigError(f"'notify' must be one of {', '.join(NOTIFY_MODES)}")

    projects_raw = raw.get("projects", [])
    if not isinstance(projects_raw, list):
        raise ConfigError("'projects' must be a list")
    projects = [normalize_project(p, i) for i, p in enumerate(projects_raw)]

    webhooks_raw = raw.get("webhooks", [])
    if webhooks_raw is None:
        webhooks_raw = []
    if not isinstance(webhooks_raw, list):
        raise ConfigError("'webhooks' must be a list")
    webhooks = [_normalize_webhook(w, eff_env, i) for i, w in enumerate(webhooks_raw)]

    return {
        "poll_interval_sec": float(interval),
        "gitlab": {"url": str(gitlab_url).rstrip("/"), "token": str(gitlab_token)},
        "github": {"api_url": str(github_url).rstrip("/"), "token": str(github_token)},
        "notify": notify,
        "projects": projects,
        "webhooks": webhooks,
    }


def derive_short(name: str) -> str:
    """First segment of name split on '-' / '_' (auth-service -> auth)."""
    for part in re.split(r"[-_]", name):
        if part:
            return part
    return name


def _normalize_flow(flow_raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(flow_raw, dict):
        raise ConfigError(f"projects[{index}].flow must be an object")
    chain_raw = flow_raw.get("chain")
    if (
        not isinstance(chain_raw, list)
        or len(chain_raw) < 2
        or not all(isinstance(b, str) and b.strip() for b in chain_raw)
    ):
        raise ConfigError(f"projects[{index}].flow.chain must be a list of at least 2 branches")
    chain = [b.strip() for b in chain_raw]

    reviewers_raw = flow_raw.get("reviewers", [])
    if not isinstance(reviewers_raw, list) or not all(isinstance(r, str) for r in reviewers_raw):
        raise ConfigError(f"projects[{index}].flow.reviewers must be a list of strings")
    reviewers = list(reviewers_raw)

    max_fix_rounds = flow_raw.get("max_fix_rounds", 3)
    if (
        isinstance(max_fix_rounds, bool)
        or not isinstance(max_fix_rounds, int)
        or max_fix_rounds < 0
    ):
        raise ConfigError(f"projects[{index}].flow.max_fix_rounds must be a non-negative integer")

    lanes_raw = flow_raw.get("lanes", [])
    if lanes_raw is None:
        lanes_raw = []
    if not isinstance(lanes_raw, list):
        raise ConfigError(f"projects[{index}].flow.lanes must be a list")

    normalized_lanes: list[dict[str, Any]] = []
    seen_branches: set[str] = set()
    for i, item in enumerate(lanes_raw):
        prefix = f"projects[{index}].flow.lanes[{i}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{prefix} must be an object")

        branch = item.get("branch")
        if not isinstance(branch, str) or not branch.strip():
            raise ConfigError(f"{prefix}.branch must be a non-empty string")
        branch = branch.strip()

        for c in chain:
            if branch == c or fnmatch.fnmatchcase(branch, c) or fnmatch.fnmatchcase(c, branch):
                raise ConfigError(f"{prefix}.branch conflicts with chain entry {c!r}")

        if branch in seen_branches:
            raise ConfigError(f"{prefix}.branch duplicate {branch!r}")
        seen_branches.add(branch)

        into = item.get("into")
        if not isinstance(into, str) or into not in chain[1:]:
            raise ConfigError(f"{prefix}.into must be one of {chain[1:]}")

        back_merge = item.get("back_merge")
        if back_merge is not None:
            if not isinstance(back_merge, str) or back_merge not in chain[1:] or back_merge == into:
                raise ConfigError(
                    f"{prefix}.back_merge must be one of {chain[1:]} and not equal to into"
                )

        normalized_lanes.append(
            {
                "branch": branch,
                "into": into,
                "back_merge": back_merge,
            }
        )

    merge_defaults: dict[str, dict[str, Any]] = {
        "feature→develop": {"method": "squash", "delete_branch": True},
        "develop→main": {"method": "merge", "delete_branch": False},
    }
    first_layer = chain[0].split("/*", 1)[0] if "/*" in chain[0] else chain[0].rstrip("*")
    if first_layer:
        merge_defaults.setdefault(
            f"{first_layer}→{chain[1]}",
            {"method": "squash", "delete_branch": True},
        )
    for i in range(1, len(chain) - 1):
        merge_defaults.setdefault(
            f"{chain[i]}→{chain[i + 1]}",
            {"method": "merge", "delete_branch": False},
        )
    for lane in normalized_lanes:
        br = lane["branch"]
        into = lane["into"]
        bm = lane["back_merge"]
        layer = br.split("/*", 1)[0] if "/*" in br else br.rstrip("*")
        merge_defaults.setdefault(
            f"{layer}→{into}",
            {"method": "merge", "delete_branch": True},
        )
        if bm:
            merge_defaults.setdefault(
                f"{into}→{bm}",
                {"method": "merge", "delete_branch": False},
            )

    merge_raw = flow_raw.get("merge")
    if merge_raw is not None:
        if not isinstance(merge_raw, dict):
            raise ConfigError(f"projects[{index}].flow.merge must be an object")
        for trans, cfg in merge_raw.items():
            if not isinstance(cfg, dict):
                raise ConfigError(f"projects[{index}].flow.merge[{trans!r}] must be an object")
            base = dict(merge_defaults.get(trans, {"method": "merge", "delete_branch": False}))
            if "method" in cfg:
                if cfg["method"] not in ("squash", "merge"):
                    raise ConfigError(
                        f"projects[{index}].flow.merge[{trans!r}].method must be 'squash' or 'merge'"
                    )
                base["method"] = cfg["method"]
            if "delete_branch" in cfg:
                base["delete_branch"] = bool(cfg["delete_branch"])
            merge_defaults[str(trans)] = base

    return {
        "chain": chain,
        "reviewers": reviewers,
        "max_fix_rounds": int(max_fix_rounds),
        "merge": merge_defaults,
        "lanes": normalized_lanes,
    }


def _normalize_deploy(
    deploy_raw: Any,
    flow: dict[str, Any] | None,
    index: int,
) -> dict[str, Any]:
    if not isinstance(deploy_raw, dict):
        raise ConfigError(f"projects[{index}].deploy must be an object")

    if "after_branch" in deploy_raw:
        raw_ab = deploy_raw["after_branch"]
        if not isinstance(raw_ab, str) or not raw_ab.strip():
            raise ConfigError(
                f"projects[{index}].deploy.after_branch must be a non-empty string"
            )
        after_branch = raw_ab.strip()
    else:
        if flow is not None and flow.get("chain"):
            after_branch = flow["chain"][-1]
        else:
            after_branch = "main"

    envs_raw = deploy_raw.get("environments")
    if not isinstance(envs_raw, list):
        raise ConfigError(f"projects[{index}].deploy.environments must be a list")
    if not envs_raw:
        raise ConfigError(f"projects[{index}].deploy.environments must not be empty")

    normalized_envs: list[dict[str, Any]] = []
    for e_idx, env_raw in enumerate(envs_raw):
        if not isinstance(env_raw, dict):
            raise ConfigError(
                f"projects[{index}].deploy.environments[{e_idx}] must be an object"
            )

        name = env_raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(
                f"projects[{index}].deploy.environments[{e_idx}].name must be a non-empty string"
            )
        name = name.strip()

        adapter = env_raw.get("adapter")
        if not isinstance(adapter, str) or adapter not in VALID_DEPLOY_ADAPTERS:
            raise ConfigError(
                f"projects[{index}].deploy.environments[{e_idx}].adapter must be one of "
                f"{', '.join(sorted(VALID_DEPLOY_ADAPTERS))}"
            )

        env_name = env_raw.get("environment")
        if env_name is None:
            env_name = name
        elif not isinstance(env_name, str) or not env_name.strip():
            raise ConfigError(
                f"projects[{index}].deploy.environments[{e_idx}].environment must be a non-empty string"
            )
        else:
            env_name = env_name.strip()

        if "production" in env_raw:
            production = bool(env_raw["production"])
        else:
            production = name.lower() in ("prod", "production") or env_name.lower() in ("prod", "production")

        if "managed_by" in env_raw:
            managed_by = env_raw["managed_by"]
            if managed_by not in ("platform", "pd"):
                raise ConfigError(
                    f"projects[{index}].deploy.environments[{e_idx}].managed_by must be 'platform' or 'pd'"
                )
        else:
            managed_by = "platform" if adapter in ("github", "gitlab") else "pd"

        if "trigger" in env_raw:
            trigger = env_raw["trigger"]
            if trigger not in ("auto", "ask"):
                raise ConfigError(
                    f"projects[{index}].deploy.environments[{e_idx}].trigger must be 'auto' or 'ask'"
                )
        else:
            trigger = "ask" if production else "auto"

        if "rollback" in env_raw:
            rollback = env_raw["rollback"]
            if rollback not in ("auto", "ask"):
                raise ConfigError(
                    f"projects[{index}].deploy.environments[{e_idx}].rollback must be 'auto' or 'ask'"
                )
        else:
            rollback = "ask" if production else "auto"

        verify_raw = env_raw.get("verify")
        if verify_raw is not None:
            if not isinstance(verify_raw, dict):
                raise ConfigError(
                    f"projects[{index}].deploy.environments[{e_idx}].verify must be an object"
                )
            verify_cfg = dict(verify_raw)
            verify_cfg.setdefault("fail_after", 2)
            verify_cfg.setdefault("interval_sec", 30)
            verify_cfg.setdefault("duration_sec", 300)
        else:
            verify_cfg = None

        github_raw = env_raw.get("github")
        if github_raw is not None:
            if not isinstance(github_raw, dict):
                raise ConfigError(
                    f"projects[{index}].deploy.environments[{e_idx}].github must be an object"
                )
            github_cfg = dict(github_raw)
        else:
            github_cfg = None

        gitlab_raw = env_raw.get("gitlab")
        if gitlab_raw is not None:
            if not isinstance(gitlab_raw, dict):
                raise ConfigError(
                    f"projects[{index}].deploy.environments[{e_idx}].gitlab must be an object"
                )
            gitlab_cfg = dict(gitlab_raw)
        else:
            gitlab_cfg = None

        command_raw = env_raw.get("command")
        if command_raw is not None:
            if not isinstance(command_raw, dict):
                raise ConfigError(
                    f"projects[{index}].deploy.environments[{e_idx}].command must be an object"
                )
            command_cfg = dict(command_raw)
        else:
            command_cfg = None

        if adapter == "command":
            if command_cfg is None or not command_cfg.get("status"):
                raise ConfigError(
                    f"projects[{index}].deploy.environments[{e_idx}]: command adapter requires 'command.status'"
                )

        if managed_by == "pd":
            if adapter == "github":
                if github_cfg is None or not github_cfg.get("workflow"):
                    raise ConfigError(
                        f"projects[{index}].deploy.environments[{e_idx}]: managed_by 'pd' on github requires 'github.workflow'"
                    )
            elif adapter == "gitlab":
                if gitlab_cfg is None or not gitlab_cfg.get("job"):
                    raise ConfigError(
                        f"projects[{index}].deploy.environments[{e_idx}]: managed_by 'pd' on gitlab requires 'gitlab.job'"
                    )
            elif adapter == "command":
                if command_cfg is None or not command_cfg.get("deploy"):
                    raise ConfigError(
                        f"projects[{index}].deploy.environments[{e_idx}]: managed_by 'pd' on command requires 'command.deploy'"
                    )

        env_cfg = deploymodel.EnvConfig(
            name=name,
            adapter=adapter,
            environment=env_name,
            production=production,
            managed_by=managed_by,
            trigger=trigger,
            rollback=rollback,
            verify=verify_cfg,
            github=github_cfg,
            gitlab=gitlab_cfg,
            command=command_cfg,
        )
        normalized_envs.append(dataclasses.asdict(env_cfg))

    return {
        "after_branch": after_branch,
        "environments": normalized_envs,
    }


def normalize_project(p: Any, index: int = 0) -> dict[str, Any]:
    """Return a copy of a project entry with provider / name / short filled."""
    if not isinstance(p, dict):
        raise ConfigError(f"projects[{index}] must be an object")
    proj = dict(p)

    provider = proj.get("provider")
    if not provider:
        if proj.get("repo"):
            provider = "github"
        elif proj.get("id") not in (None, ""):
            provider = "gitlab"
        else:
            raise ConfigError(f"projects[{index}]: need 'repo' (github) or 'id' (gitlab)")
    if provider not in PROVIDERS:
        raise ConfigError(f"projects[{index}]: unknown provider {provider!r}")
    proj["provider"] = provider

    if provider == "github":
        repo = proj.get("repo")
        if not isinstance(repo, str) or "/" not in repo:
            raise ConfigError(f"projects[{index}]: github 'repo' must be 'owner/repo'")
        default_name = repo.rsplit("/", 1)[-1]
    else:
        pid = proj.get("id")
        if pid in (None, ""):
            raise ConfigError(f"projects[{index}]: gitlab project needs 'id'")
        default_name = str(pid).rsplit("/", 1)[-1]

    name = proj.get("name") or default_name
    proj["name"] = str(name)
    proj["short"] = str(proj.get("short") or derive_short(proj["name"]))
    if proj.get("local_path"):
        proj["local_path"] = str(Path(str(proj["local_path"])).expanduser())
    else:
        proj["local_path"] = None
    proj.setdefault("branch", "auto")
    if proj.get("flow") is not None:
        proj["flow"] = _normalize_flow(proj["flow"], index)
    if proj.get("deploy") is not None:
        proj["deploy"] = _normalize_deploy(proj["deploy"], proj.get("flow"), index)
    return proj


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def gitlab_token(cfg: Mapping[str, Any], env: Mapping[str, str] | None = None) -> str | None:
    """PIPEDOG_GITLAB_TOKEN -> GITLAB_TOKEN -> config.gitlab.token."""
    env = os.environ if env is None else env
    for value in (env.get("PIPEDOG_GITLAB_TOKEN"), env.get("GITLAB_TOKEN"),
                  (cfg.get("gitlab") or {}).get("token")):
        if value and str(value).strip():
            return str(value).strip()
    return None


def github_token(cfg: Mapping[str, Any], env: Mapping[str, str] | None = None,
                 run: Runner = subprocess.run) -> str | None:
    """PIPEDOG_GITHUB_TOKEN -> GITHUB_TOKEN -> config.github.token -> `gh auth token`."""
    env = os.environ if env is None else env
    for value in (env.get("PIPEDOG_GITHUB_TOKEN"), env.get("GITHUB_TOKEN"),
                  (cfg.get("github") or {}).get("token")):
        if value and str(value).strip():
            return str(value).strip()
    try:
        res = run(["gh", "auth", "token"], capture_output=True, text=True,
                  timeout=_GH_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    token = (res.stdout or "").strip()
    return token or None


_SCRUB_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]{20,}"), "***"),
    (re.compile(r"glpat-[A-Za-z0-9_-]{20,}"), "***"),
    (re.compile(r"(?i)\b(Authorization:\s*Bearer\s+)[^\s\"',]+"), r"\g<1>***"),
    (re.compile(r"(?i)\b(PRIVATE-TOKEN:\s*)[^\s\"',]+"), r"\g<1>***"),
    (re.compile(r"(?i)\b(token=)[^&#'\"\s]+"), r"\g<1>***"),
)


def redact(text: str | None, secrets: Iterable[str | None]) -> str | None:
    """Replace any secret occurrence in text with '***' (defense in depth)."""
    if not text:
        return text
    targets: set[str] = set()
    for s in (secrets if secrets is not None else ()):
        if not s or not isinstance(s, str) or len(s) < 4:
            continue
        targets.add(s)
        if len(s) >= 8:
            targets.add(urllib.parse.quote(s, safe=""))
            targets.add(urllib.parse.quote_plus(s))
            b64 = base64.b64encode(s.encode("utf-8")).decode("ascii")
            targets.add(b64)
            b64_nopad = b64.rstrip("=")
            if b64_nopad:
                targets.add(b64_nopad)

    for target in sorted(targets, key=len, reverse=True):
        if target in text:
            text = text.replace(target, "***")

    for pattern, repl in _SCRUB_PATTERNS:
        text = pattern.sub(repl, text)

    return text


# ---------------------------------------------------------------------------
# Branch
# ---------------------------------------------------------------------------


def resolve_branch(project: Mapping[str, Any], run: Runner = subprocess.run) -> str | None:
    """Concrete branch -> itself; 'auto'/missing + existing local_path -> git HEAD; else None.

    Called on every poll so a local `git switch` is followed immediately.
    """
    branch = project.get("branch")
    if branch and branch != "auto":
        return str(branch)
    local_path = project.get("local_path")
    if not local_path:
        return None
    path = Path(str(local_path)).expanduser()
    if not path.is_dir():
        return None
    try:
        res = run(["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
                  capture_output=True, text=True, timeout=_GIT_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    name = (res.stdout or "").strip()
    if not name or name == "HEAD":
        return None
    return name
