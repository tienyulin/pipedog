---
name: pipedog-setup
description: Install, configure, and launch the pipedog (pd) CI/CD monitor and flow daemon. Use when the user asks to 安裝 pipedog, 設定 pd, 設定 config.json, 啟動 pd, 試玩 pd --mock, install pipedog, configure pd, or start the pd daemon.
---

# pipedog (`pd`) — Installation, Configuration & Daemon Setup

`pipedog` (`pd`) is an always-on terminal CI/CD pipeline monitor and branch flow automation daemon (`feature/*` → `develop` → `main`) supporting GitLab CI and GitHub Actions.

> **Security Rule**: Never read, inspect, echo, or print any GitLab/GitHub personal access tokens or secret environment variables (`PIPEDOG_GITLAB_TOKEN`, `GITLAB_TOKEN`, `PIPEDOG_GITHUB_TOKEN`, `GITHUB_TOKEN`, or token fields in `config.json`).

## 1. Ensure `pd` Is in `PATH`

System requirement: Python >= 3.8, no third-party packages.

If `pd` is not found in `PATH`:
1. Install the `pipedog-setup` skill package via `npx skills`:
   ```bash
   npx skills add tienyulin/pipedog -g -a claude-code -a antigravity
   ```
2. Find the canonical installation directory with `npx skills list`, then symlink `bin/pd` into `~/.local/bin/pd`:
   ```bash
   ln -s <canonical copy>/skills/pipedog-setup/bin/pd ~/.local/bin/pd
   ```

## 2. Configure `~/.config/pipedog/config.json`

Create `~/.config/pipedog/config.json`:

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

Provide API tokens via environment variables (or `gh auth login` for GitHub):
- GitLab: `export PIPEDOG_GITLAB_TOKEN=...` or `export GITLAB_TOKEN=...` (`api` scope required for flow automation)
- GitHub: `export PIPEDOG_GITHUB_TOKEN=...`, `export GITHUB_TOKEN=...`, or `gh auth login` (`repo` scope required)

### Flow Lanes (optional)

Besides the main chain, projects can declare extra entry branches ("lanes") that merge directly into a target chain branch and optionally back-merge into another chain branch:

```json
"flow": {
  "chain": ["feature/*", "develop", "main"],
  "lanes": [
    {"branch": "hotfix/*",  "into": "main", "back_merge": "develop"},
    {"branch": "release/*", "into": "main", "back_merge": "develop"}
  ]
}
```

- **Fields & Validation**:
  - `branch`: Non-empty branch pattern (e.g. `hotfix/*`). Must not conflict with or fnmatch any chain branch, and duplicate lane branches are rejected.
  - `into`: Target branch, must be one of `chain[1:]` (e.g. `main`). Default merge config is `{"method": "merge", "delete_branch": true}`.
  - `back_merge`: Optional target branch in `chain[1:]` (cannot equal `into`). Default merge config is `{"method": "merge", "delete_branch": false}`.
  - User `merge` entries override these defaults.
- **Behavior**:
  - Lane branches act as entry branches: red CI emits `ci_failed` (respecting `max_fix_rounds`), green CI auto-opens a PR targeting `into`, followed by review gate and merge decision.
  - After a successful merge into `into`, if `back_merge` is configured, pipedog automatically creates and tracks a back-merge PR (`into` → `back_merge`) without triggering immediate promotion of the target branch. Literal chain branches (e.g. `main`, `develop`) are never deleted on merge.

### Webhook Notifications (optional)

Configure outgoing webhooks to Slack, Discord, or generic JSON endpoints:

```json
"webhooks": [
  {
    "url_env": "PIPEDOG_WEBHOOK_URL",
    "format": "slack",
    "events": ["failed", "decision", "deploy"]
  }
]
```

- `url_env`: name of an environment variable holding the webhook URL (preferred; keeps secrets out of `config.json`).
- `url`: direct webhook URL (must start with `https://`; loopback and private IP hosts like `localhost`, `127.*`, `10.*`, `192.168.*`, `172.16-31.*`, `169.254.*` are rejected).
- `format`: `"slack"`, `"discord"`, or `"json"`.
- `events`: list of events to notify on (`"failed"`, `"decision"`, `"deploy"`).

### Background Tick (optional, do not install by default)

pd needs nothing besides itself: the TUI (`pd`), `pd status`, `pd wait` and `pd tick` each advance flow and deploys while they run, and `pd status` ticks first whenever a deploy is in progress or the snapshot is stale. Agents watching CI (`pd wait`, `pipedog-ci-watch`) keep things moving too.

Do NOT install a system scheduler during setup. Only if the user explicitly asks for hands-off background progress, offer `pd install-tick` (macOS launchd; remove with `pd uninstall-tick`) or a crontab line on Linux. If `pd doctor` warns `engine idle` while a deploy is in progress, running `pd status` once is enough.

## 3. Continuous Deployment (`deploy`) Configuration (Optional)

Each project can optionally configure a `deploy` block to automatically track, trigger, verify, and rollback deployments across environments:

```json
"deploy": {
  "after_branch": "main",
  "environments": [
    {
      "name": "staging",
      "adapter": "github",
      "environment": "staging",
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
  ]
}
```

### Command Adapter Templates

When using `adapter: "command"`, you can integrate any deployment tool using shell commands. Placeholders `{sha}`, `{previous_sha}`, `{previous_id}`, `{env}`, and `{branch}` are automatically substituted:

1. **Argo CD (`parse: "argocd"`)**:
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

2. **kubectl rollout (`parse: "kubectl-rollout"`)**:
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

3. **Vercel CLI (`parse: "json"`)**:
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

## 4. Start `pd` or Try `--mock`

- Start the live monitor in a dedicated terminal pane:
  ```bash
  pd
  ```
- Try the built-in simulation without any config or network access:
  ```bash
  pd --mock
  pd --mock --once
  pd --mock --gallery
  ```
  `pd --mock --gallery` prints a static panel containing every pipeline, flow, review, and environment scenario deterministically (useful for UI testing and visual inspection; add `--ascii` or `PIPEDOG_ASCII=1` for ASCII mode).

## 5. Background Tick Schedule (Advance Flow When Agent Is Idle)

Normally, `pd wait` and the interactive TUI `pd` advance flow and deployments automatically while running. `pd status` also advances first whenever a deployment is in progress or the snapshot is stale, so no extra scheduler is needed. Only configure a background scheduler if you explicitly require hands-off progress without any active `pd` or agent session:

- **macOS (launchd scheduled agent)**:
  - Install (runs `pd tick` every 300 seconds by default, loads on boot and inherits `PATH`):
    ```bash
    pd install-tick
    # Or specify custom interval in seconds:
    pd install-tick --interval 180
    ```
  - Uninstall:
    ```bash
    pd uninstall-tick
    ```
  - Details: creates `~/Library/LaunchAgents/dev.pipedog.tick.plist` managed via `launchctl`. Output logs to `<state_dir>/tick.log` (e.g. `~/.local/state/pipedog/tick.log`).
- **Linux (crontab schedule)**:
  - Add to crontab (`crontab -e`):
    ```cron
    */5 * * * * <pd path> tick >> ~/.local/state/pipedog/tick.log 2>&1
    ```
- **Log Rotation**:
  - Scheduled runs output to `<state_dir>/tick.log` (e.g. `~/.local/state/pipedog/tick.log`).
  - During `pd tick`, if `tick.log` exceeds 1 MiB, it is automatically rotated to keep the last 256 KiB atomically.

## 6. Troubleshooting: `pd doctor`

When experiencing issues with configuration, authentication, tokens, permissions, rate limits, or engine status, troubleshooting begins with running `pd doctor` first:

```bash
pd doctor
```

Options:
- `pd doctor --offline`: Skip all network calls and verify local Python, configuration, and state directory only.
- `pd doctor --json`: Output health check results as a JSON list of objects.

Exit codes:
- `0`: All checks passed.
- `1`: One or more warnings detected.
- `2`: One or more checks failed.
- `3`: Doctor crashed (unexpected error).
