---
name: pipedog-ci-fix
description: One-shot diagnosis and fix for failing CI jobs using pipedog's failed_jobs snapshot. Use when the user asks to 修 CI, 修紅燈, pipeline 爆了幫我修, fix failing CI, or fix the broken build.
---

# pipedog-ci-fix — One-Shot CI Failure Diagnosis & Fix

Use this skill to diagnose and fix failing CI jobs from `pd`, run local tests, push the fix to the feature branch, and acknowledge any matching failure event.

> **Security Rule**: Never read, inspect, echo, or print any GitLab/GitHub personal access tokens or secret environment variables (`PIPEDOG_GITLAB_TOKEN`, `GITLAB_TOKEN`, `PIPEDOG_GITHUB_TOKEN`, `GITHUB_TOKEN`, or token fields in `config.json`).

## 1. Prerequisite Check (`pd` in `PATH`)

Requirement: Python >= 3.8, no third-party packages.

Verify that `pd` is available on `PATH`. If `pd` is not found in `PATH`, prompt the user to install the `pipedog-setup` skill first and add `bin/pd` to `PATH` (run `npx skills list` to locate `<canonical copy>`, then `ln -s <canonical copy>/skills/pipedog-setup/bin/pd ~/.local/bin/pd`).

## 2. Inspect `failed_jobs`

1. Run `pd status --json` (or `pd --once` if the snapshot is missing or stale).
2. **Mock Mode Guard**: If `"mock": true` in the snapshot, explain the simulated root cause from `failed_jobs`, **but do NOT modify any files under `local_path`, and do NOT run `git commit` or `git push`**.
3. For each entry in `failed_jobs`:
   - Locate the offending file in `local_path` using `file` and `line` (or infer from `error_summary` and `log_tail` when `file` is `null`).
   - Verify the current branch in `local_path` (`git -C <local_path> rev-parse --abbrev-ref HEAD`) matches the failing `branch` and is a feature branch (first tier of `flow.chain`, e.g. `feature/*`).

## 3. Fix, Test, Push & Acknowledge

1. Edit the code in `local_path` to resolve the root cause.
2. Run the repository's local test suite (e.g. `pytest`, `npm test`, `cargo test`) and confirm all tests pass.
3. Commit and `git push` to the current feature branch.
4. Obtain the new commit SHA (`git -C <local_path> rev-parse HEAD`). If there is an unacknowledged event (`ci_failed`, `yellow_fix`, `deploy_failed`, `verify_failed`) for this branch, acknowledge it:
   ```bash
   pd ack <event_id> --pushed <new_sha>
   ```

## 4. Git & Safety Rules (Strict)

- **Feature branch only**: Only push the event's or current feature branch (must match the first tier of `flow.chain`, e.g. `feature/*`).
- **NEVER force push**: Never run `git push --force` or `git push --force-with-lease`.
- **NEVER push protected branches**: Never push directly to `develop`, `main`, `master`, or any non-feature branch.
- **Merge only via `pd decide`**: Never merge a PR/MR directly; merges must only happen through `pd decide` after user confirmation.
- **NEVER directly touch environments**: For `deploy_failed` or `verify_failed`, never run `kubectl`, `argocd`, or directly manipulate environments. Fixes must be code changes pushed through the standard git flow.
