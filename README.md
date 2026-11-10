[中文說明](README.zh-TW.md)

# pipedog (`pd`)

```text
        __              _                _
(\_____/|''-o     _ __ (_) _ __  ___  __| | ___  __ _
(     (_|\_/     | '_ \| || '_ \/ -_)/ _` |/ _ \/ _` |
 \  __  /        | .__/|_|| .__/\___|\__,_|\___/\__, |
  \_) \_)        |_|      |_|                   |___/
```

**pipedog** (`pd`) is an always-on terminal CI/CD pipeline monitor and branch flow automation tool supporting **GitLab CI** and **GitHub Actions**. It also installs as an AI agent skill package via `npx skills`.

pipedog monitors your branch pipelines, automatically creates PRs/MRs along configured branch chains (e.g. `feature/*` → `develop` → `main`), gates merges and promotions with interactive decision queues, tracks continuous deployments (CD) with automated health verification and rollbacks, and hands off CI failures and review change requests to AI coding agents via `pd wait`.

---

## Requirements

- **Python** `>=3.8` (Standard library only; zero third-party dependencies)

---

## Installation

### 1. Install Agent Skills

This repository ships 6 specialized skills (`pipedog-setup`, `pipedog-ci-status`, `pipedog-ci-fix`, `pipedog-ci-watch`, `pipedog-pr-review`, `pipedog-approve`). Using `npx skills` installs all 6 skills by default (you can specify multiple agents with `-a <agent>`):

```bash
npx skills add tienyulin/pipedog -g -a claude-code -a antigravity
```

To install only a specific skill (e.g. the read-only status inspector):

```bash
npx skills add tienyulin/pipedog -g -a claude-code -a antigravity --skill pipedog-ci-status
```

| Skill | Path | Responsibility |
|---|---|---|
| `pipedog-setup` | `skills/pipedog-setup/SKILL.md` | Core installation, configuring `~/.config/pipedog/config.json`, `PATH` setup, launching `pd` and `--mock` simulation |
| `pipedog-ci-status` | `skills/pipedog-ci-status/SKILL.md` | Read-only CI/CD status query (`pd status --json` / `pd --once`), never modifies code |
| `pipedog-ci-fix` | `skills/pipedog-ci-fix/SKILL.md` | One-shot fix for failing CI jobs (diagnose `failed_jobs` → edit code → local tests → safe push to feature branch → `pd ack`) |
| `pipedog-ci-watch` | `skills/pipedog-ci-watch/SKILL.md` | Continuous CI watch loop via `pd wait`, automatically handling CI failures and PR/MR review comments |
| `pipedog-pr-review` | `skills/pipedog-pr-review/SKILL.md` | Handle PR/MR review comments (`pd comments` → evaluate & apply code changes → push → `pd reply` without resolving threads → `pd ack --handled`) |
| `pipedog-approve` | `skills/pipedog-approve/SKILL.md` | Manage pending decision queue (`allow` / `once` / `fix` / `merge` / `open` / `deploy` / `approve` / `reject` / `rollback` / `retry` / `skip`) and yellow-light rules via `pd rules` |

### 2. Add `pd` to `PATH`

Run `npx skills list` to locate the canonical installation directory, then symlink `bin/pd` to `~/.local/bin/pd` (ensure `~/.local/bin` is in your `PATH`):

```bash
ln -s <canonical copy>/skills/pipedog-setup/bin/pd ~/.local/bin/pd
```

---

## Quick Trial (`--mock` Mode)

Experience flow automation, the pending decision queue (`⚠ WAITING FOR YOU`), real-time error extraction, and CD progression immediately without configuration or API tokens:

```bash
pd --mock
```

- **On launch**:
  - `payment-api` (`feature/checkout→main`) has an approved PR with green CI, generating a `merge` decision.
  - `web-frontend` (`feature/ui→develop`) has an `allow_failure` eslint failure, generating a `yellow` light decision.
  - `auth-service`: staging environment automatically deploys and passes verification; production generates a `deploy` decision.
- **After 5 seconds**: `auth-service` `test 2/3` transitions to `failed` (`tests/test_auth.py:42 AssertionError`), emitting a `ci_failed` event for `pd wait`.
- **Ongoing progression**: `web-frontend` staging deployment verification fails, automatically triggering a `rollback` to the last known-good version.
- **Gallery inspection**: Run `pd --mock --gallery` to render a static panel containing every pipeline, review, flow, and environment scenario deterministically (add `--ascii` or `PIPEDOG_ASCII=1` for ASCII mode).

---

## Interactive TUI Controls & Hotkeys

In the live interactive `pd` terminal monitor:

### Navigation & Views

- `1`–`9`: Select pending decision row directly by number.
- `↑` / `↓` or `k` / `j`: Move selection up or down among pending decisions.
- `w`: Open target in web browser (PR/MR URL or deployment URL; in SSH sessions or headless Linux, prints the URL).
- `p` (when no decision is selected): Toggle collapsing or expanding passed pipelines (`p collapse passed` / `p expand passed`).
- `q`: Quit `pd`.

### Actions on Selected Decision

- `a` (or `o`): `once` — allow yellow light for this commit only.
- `A`: `allow` (with `--remember`) — allow yellow light and save matching rule to `rules.json`.
- `f`: `fix` — emit `yellow_fix` event for agent to fix.
- `m`: `merge` — re-verify and merge approved PR/MR.
- `p`: `open` — open promotion PR/MR (e.g. `develop` → `main`).
- `d`: `deploy` — trigger environment deployment.
- `v`: `approve` — approve platform deployment gate (e.g. GitHub environment approval).
- `x`: `reject` — reject platform deployment gate.
- `b`: `rollback` — roll back to last known-good deployment.
- `t`: `retry` — retry stuck deployment.
- `n`: `skip` — skip / postpone decision.

### Safety Confirmation Tiers

To prevent accidental merges or production outages, actions use tiered confirmation:

- **Low Risk (Instant)**: Yellow light actions (`a`, `A`, `o`, `f`) and skipping decisions (`n`) execute immediately.
- **Medium Risk (Double-Press Confirmation)**: PR/MR merges (`m`), branch promotions (`p`), and non-production deployments/rollbacks (`d`, `v`, `b`) require pressing the key a second time within 3 seconds (`press <key> again within 3s to <action>`). Pressing Esc or letting the timer expire cancels the action.
- **High Risk (Type `prod` + Enter)**: Deploying, approving, rolling back, or retrying deployments on a `production` environment requires typing `prod` followed by Enter (`type prod + Enter to <action> <sha> on <env>, Esc cancels:`).

---

## Configuration & Flow Automation

### Quick Start: `pd init`

Inside your git repository, generate a working configuration in one step without manually writing JSON or exposing tokens:

```bash
pd init
```

Options:
- `--path DIR`: Target repository directory (defaults to current directory).
- `--flow auto|gitflow|github|none`: Branch flow strategy (default `auto`: detects `origin/develop` for Git Flow `["feature/*", "develop", <default>]`, otherwise GitHub Flow `["feature/*", <default>]`).
- `--name NAME`: Project name override (defaults to repository folder name).
- `--config PATH`: Target configuration path (`--config` > `PIPEDOG_CONFIG` > `~/.config/pipedog/config.json`).
- `--dry-run`: Output generated JSON project entry without writing to file.
- `--force`: Replace entry in place if this project is already configured.

After running `pd init`, set your personal access token (`export PIPEDOG_GITHUB_TOKEN=...` or `gh auth login`, or `export PIPEDOG_GITLAB_TOKEN=...`), then run `pd doctor` to verify setup.

### Manual Configuration

Default configuration file location is `~/.config/pipedog/config.json` (override with `--config PATH` or `PIPEDOG_CONFIG` environment variable):

```bash
mkdir -p ~/.config/pipedog
cp <canonical copy>/skills/pipedog-setup/config.example.json ~/.config/pipedog/config.json
```

Example `config.json` supporting Git Flow and GitHub Flow:

```json
{
  "poll_interval_sec": 5,
  "gitlab": { "url": "https://gitlab.com", "token": "" },
  "github": { "api_url": "https://api.github.com", "token": "" },
  "notify": "auto",
  "projects": [
    {
      "provider": "gitlab",
      "id": 123456,
      "name": "auth-service",
      "short": "auth",
      "local_path": "~/workspace/auth-service",
      "branch": "auto",
      "flow": {
        "chain": ["feature/*", "develop", "main"],
        "reviewers": [],
        "max_fix_rounds": 3,
        "merge": {
          "feature→develop": { "method": "squash", "delete_branch": true },
          "develop→main": { "method": "merge", "delete_branch": false }
        }
      },
      "deploy": {
        "after_branch": "main",
        "environments": [
          {
            "name": "staging",
            "adapter": "gitlab",
            "trigger": "auto",
            "verify": {
              "url": "https://staging.example.com/health",
              "expect_status": 200,
              "duration_sec": 300,
              "interval_sec": 30,
              "fail_after": 2
            },
            "rollback": "auto"
          },
          {
            "name": "production",
            "adapter": "gitlab",
            "production": true,
            "trigger": "ask",
            "rollback": "ask"
          }
        ]
      }
    },
    {
      "provider": "github",
      "repo": "octo-org/payment-api",
      "name": "payment-api",
      "short": "pay",
      "local_path": "~/workspace/payment-api",
      "branch": "auto",
      "flow": {
        "chain": ["feature/*", "main"],
        "reviewers": [],
        "max_fix_rounds": 3
      }
    }
  ]
}
```

- `flow.chain`: Branch promotion progression (at least 2 tiers). First tier is a feature branch pattern (e.g. `"feature/*"`), followed by target branches (`["feature/*", "develop", "main"]` for Git Flow, or `["feature/*", "main"]` for GitHub Flow). Projects without `flow` remain in monitor-only mode.
- `flow.lanes`: Extra entry branches (e.g. `hotfix/*`, `release/*`) that merge directly into a target chain branch (e.g. `main`) with optional automated back-merge PRs (e.g. into `develop`).
- `flow.max_fix_rounds`: Maximum consecutive automated fix attempts on a branch (default `3`).
- `flow.merge`: Merge methods per stage (`method`: `"squash"` or `"merge"`; `"delete_branch"`: `true` or `false`).
- `deploy`: Optional continuous deployment block. `after_branch` specifies the branch that triggers deployment progression once green.
  - `environments`: List of target environments. `adapter` can be `"gitlab"`, `"github"`, or `"command"`.
  - `trigger`: `"auto"` (deploy immediately upon trigger) or `"ask"` (generate a pending decision).
  - `verify`: Automated deployment health verification via HTTP endpoint (`url`, `expect_status`, `duration_sec`, `interval_sec`, `fail_after`) or shell command (`command`).
  - `rollback`: `"auto"` (automatically rollback to last verified deployment on verification failure) or `"ask"` (generate a rollback decision).

### Command Adapter Templates

When using `adapter: "command"`, shell commands integrate with any CD tool. Placeholders `{sha}`, `{previous_sha}`, `{previous_id}`, `{env}`, and `{branch}` are automatically substituted:

- **Argo CD (`parse: "argocd"`)**:
  ```json
  {
    "name": "production",
    "adapter": "command",
    "production": true,
    "trigger": "ask",
    "command": {
      "status": "argocd app get myapp -o json",
      "deploy": "argocd app sync myapp --revision {sha}",
      "rollback": "argocd app rollback myapp {previous_id}",
      "parse": "argocd"
    },
    "verify": {
      "command": "./scripts/smoke.sh",
      "duration_sec": 600,
      "interval_sec": 60,
      "fail_after": 2
    },
    "rollback": "ask"
  }
  ```

- **kubectl rollout (`parse: "kubectl-rollout"`)**:
  ```json
  {
    "name": "staging",
    "adapter": "command",
    "trigger": "auto",
    "command": {
      "status": "kubectl rollout status deployment/myapp -n staging",
      "deploy": "kubectl set image deployment/myapp myapp=registry.example.com/myapp:{sha} -n staging",
      "rollback": "kubectl rollout undo deployment/myapp -n staging",
      "parse": "kubectl-rollout"
    },
    "verify": {
      "url": "https://staging.example.com/healthz",
      "expect_status": 200,
      "duration_sec": 180,
      "interval_sec": 15,
      "fail_after": 2
    },
    "rollback": "auto"
  }
  ```

- **Vercel CLI (`parse: "json"`)**:
  ```json
  {
    "name": "preview",
    "adapter": "command",
    "trigger": "auto",
    "command": {
      "status": "echo '{\"status\": \"success\", \"sha\": \"{sha}\"}'",
      "deploy": "vercel --prod --yes",
      "rollback": "vercel rollback {previous_id} --yes",
      "parse": "json"
    },
    "verify": {
      "command": "curl -fsS https://my-app.vercel.app/api/health",
      "duration_sec": 120,
      "interval_sec": 15
    },
    "rollback": "auto"
  }
  ```

### 2. Configure API Tokens

Environment variables are recommended:

- **GitLab** (`api` scope required for flow write operations):
  1. `export PIPEDOG_GITLAB_TOKEN="glpat-..."`
  2. `export GITLAB_TOKEN="glpat-..."`
  3. `gitlab.token` in `config.json`
- **GitHub** (`repo` scope required):
  1. `export PIPEDOG_GITHUB_TOKEN="ghp_..."`
  2. `export GITHUB_TOKEN="ghp_..."`
  3. `github.token` in `config.json`
  4. Automatically resolved via `gh auth token` if logged into GitHub CLI (`gh auth login`)

### 3. Webhook Notifications (Optional)

Configure outgoing notifications to Slack, Discord, or custom JSON endpoints:

```json
"webhooks": [
  {
    "url_env": "PIPEDOG_WEBHOOK_URL",
    "format": "slack",
    "events": ["failed", "decision", "deploy"]
  }
]
```

- `url_env`: Environment variable name holding the webhook URL (recommended).
- `url`: Direct webhook URL (must use HTTPS; loopback and private IP hosts are rejected).
- `format`: `"slack"`, `"discord"`, or `"json"`.
- `events`: Array of notification triggers (`"failed"`, `"decision"`, `"deploy"`).

---

## Commands & Subcommands

### Monitor

```bash
pd [--mock] [--config PATH] [--once] [--interval SEC] [--no-notify] [--gallery] [--ascii]
```

### Flow, CD & Agent Collaboration Subcommands

| Command | Description | Exit Code |
|---|---|---|
| `pd init [--path DIR] [--flow auto\|gitflow\|github\|none] [--name NAME] [--config PATH] [--dry-run] [--force]` | Generate a project entry in config from current git repository | `0` written; `2` error; `3` duplicate |
| `pd status [--json] [--no-tick]` | Display current pipeline status view (or `--json` for `active_status.json`) | `0`; snapshot missing `2` |
| `pd envs [--json]` | List deployment environment states (commit SHA, status, verification, timestamp) | `0` |
| `pd wait [--timeout SEC]` | Block waiting for events requiring agent action (advances flow automatically), outputs single JSON event | `0` on event; `4` timeout; `5` other waiter active; `2` config error |
| `pd tick` | Run a single flow engine poll tick and update snapshot | `0`; `2` config error |
| `pd ack <event_id> [--handled ID...] [--pushed SHA]` | Acknowledge handled event, reporting handled comment IDs and new commit SHA | `0`; not found `2` |
| `pd decisions [--json]` | List current pending decisions (`yellow`, `merge`, `promote`, `deploy`, `deploy_approval`, `rollback`, `deploy_stuck`) | `0` |
| `pd decisions --history [--limit N] [--project P] [--json]` | View resolved decision history showing who approved or answered what and when | `0` |
| `pd decide <id> <answer> [--remember]` | Answer a decision (`allow`, `once`, `fix`, `merge`, `open`, `deploy`, `approve`, `reject`, `rollback`, `retry`, `skip`; `allow --remember` saves rule) | `0`; invalid `2` |
| `pd rules list [--json]` / `pd rules rm <id>` | Inspect or delete saved yellow-light rules | `0`; file corrupted `2` |
| `pd comments <project> <pr> [--json]` | View PR/MR review comments and threads (`seen` tracking) | `0`; error `2` |
| `pd reply <project> <pr> <comment_id> <text>` | Reply to PR/MR review comment (leaves review thread unresolved) | `0`; error `2` |
| `pd doctor [--offline] [--json] [--config PATH]` | Inspect system health, configuration, tokens, scopes, and API rate limits | `0` OK; `1` warnings; `2` failed; `3` crash |
| `pd install-tick [--interval SEC=300]` | Install macOS launchd background schedule (default 300s, logged to `tick.log`) | `0`; `2` non-macOS or error |
| `pd uninstall-tick` | Remove macOS launchd background schedule | `0`; `2` non-macOS or error |

---

## Background Tick Schedule (Optional)

pipedog does not require a background daemon. Both `pd wait` and the interactive TUI `pd` advance branch flows and deployments on each poll tick. `pd status` also advances flow immediately whenever a deployment is active or the snapshot is stale.

If you want branch pipelines and deployments to advance continuously even when no terminal or agent is open, you can optionally configure background execution:

- **macOS (launchd agent)**:
  ```bash
  pd install-tick
  pd install-tick --interval 180
  pd uninstall-tick
  ```
  Writes to `~/Library/LaunchAgents/dev.pipedog.tick.plist` and logs to `<state_dir>/tick.log` (e.g. `~/.local/state/pipedog/tick.log`).
- **Linux (crontab)**:
  Add to crontab via `crontab -e`:
  ```cron
  */5 * * * * <path to pd> tick >> ~/.local/state/pipedog/tick.log 2>&1
  ```
- **Log rotation**: When `tick.log` exceeds 1 MiB, `pd tick` automatically retains the last 256 KiB atomically.

---

## Troubleshooting with `pd doctor`

Whenever you encounter issues with credentials, tokens, network limits, configuration, or daemon locks, run `pd doctor`:

```bash
pd doctor
```

Options:
- `--offline`: Skip network API calls; check local Python, configuration syntax, and state directories only.
- `--json`: Output check results in structured JSON format.
- `--config PATH`: Run health checks against a specific configuration file.

Exit codes:
- `0`: All checks passed.
- `1`: Warnings found (e.g. low rate limits, optional scheduler missing).
- `2`: Health check failed (e.g. invalid config, missing token, expired permissions).
- `3`: Doctor crashed unexpectedly.
