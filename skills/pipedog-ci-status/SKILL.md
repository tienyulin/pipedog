---
name: pipedog-ci-status
description: Read-only CI/CD pipeline status check via pd status --json or pd --once. Use when the user asks to 看 CI 狀態, CI 跑完了嗎, pipeline 狀態, 查詢 CI, check CI status, or show pipeline status.
---

# pipedog-ci-status — Read-Only CI/CD Status Check

Use this skill to inspect and summarize current CI/CD pipeline status and pending flow decisions without modifying any code.

> **Security Rule**: Never read, inspect, echo, or print any GitLab/GitHub personal access tokens or secret environment variables (`PIPEDOG_GITLAB_TOKEN`, `GITLAB_TOKEN`, `PIPEDOG_GITHUB_TOKEN`, `GITHUB_TOKEN`, or token fields in `config.json`).

## 1. Prerequisite Check (`pd` in `PATH`)

Requirement: Python >= 3.8, no third-party packages.

Verify that `pd` is available on `PATH`. If `pd` is not found in `PATH`, prompt the user to install the `pipedog-setup` skill first and add `bin/pd` to `PATH` (run `npx skills list` to locate `<canonical copy>`, then `ln -s <canonical copy>/skills/pipedog-setup/bin/pd ~/.local/bin/pd`).

## 2. Read Status Snapshot and Environments

1. Run:
   ```bash
   pd status --json
   ```
   Or to inspect deployment environments specifically:
   ```bash
   pd envs --json
   ```
2. **Freshness check**:
   - If `pd status --json` exits with `2` (snapshot missing) or `now - updated_at > stale_after_sec`:
     - Inform the user that the `pd` daemon is not currently running (or its snapshot is stale).
     - Run `pd --once` (or suggest starting `pd` in a terminal pane) and then read `pd status --json`.
3. **Mock mode check**:
   - If `"mock": true` in the JSON output, explicitly note that `pd` is running in `--mock` simulation mode.

## 3. Report Format (Read-Only)

Summarize the snapshot clearly:
- **Pending Decisions (`decisions_pending`)**: List each decision (`id`, `kind`, `project`, `source→target`, and `reasons`). Kinds include `merge`, `yellow`, `deploy`, `deploy_approval`, `rollback`, `deploy_stuck`.
- **Deployment Environments (`environments`)**: List deployed and active environments (`project`, `env`, `sha` [7-char short sha], `status` [success/running/failed/stuck/none], `verified` [true/false], `deployed_at`, and `web_url`).
- **Failed Jobs (`failed_jobs`)**: List each non-allowed failure (`repo`, `branch`, `job_name`, `file:line`, `error_summary`).
- **Pipelines (`pipelines`)**: List each tracked pipeline (`project`, `branch`, `#pipeline_id`, `status`, and any running or pending jobs).

**Strict Read-Only Rule**: Never edit repository files, never run `git commit`, and never run `git push` in this skill.
