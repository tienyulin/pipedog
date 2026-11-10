---
name: pipedog-ci-watch
description: Continuous CI watch loop via pd wait that automatically handles CI failures and PR review events as they arrive. Use when the user asks to 顧 CI, 盯 CI, 持續監控並自動修, watch CI loop, or run pd wait.
---

# pipedog-ci-watch — Continuous CI Watch Loop (`pd wait`)

Use this self-contained skill to run the continuous `pd wait` loop, automatically fixing CI failures and addressing PR/MR review comments as events arrive.

> **Security Rule**: Never read, inspect, echo, or print any GitLab/GitHub personal access tokens or secret environment variables (`PIPEDOG_GITLAB_TOKEN`, `GITLAB_TOKEN`, `PIPEDOG_GITHUB_TOKEN`, `GITHUB_TOKEN`, or token fields in `config.json`).

## 1. Prerequisite Check (`pd` in `PATH`)

Requirement: Python >= 3.8, no third-party packages.

Verify that `pd` is available on `PATH`. If `pd` is not found in `PATH`, prompt the user to install the `pipedog-setup` skill first and add `bin/pd` to `PATH` (run `npx skills list` to locate `<canonical copy>`, then `ln -s <canonical copy>/skills/pipedog-setup/bin/pd ~/.local/bin/pd`).

## 2. Watch Loop Procedure

`pd wait` automatically advances the flow engine on every poll cycle without requiring a background `pd` monitor or daemon. Whenever you push code, proceed directly into `pd wait`.

Repeat the following cycle:

1. **Wait for the next event**:
   ```bash
   pd wait
   ```
   - **Exit code `0`**: Event arrived. Parse the single-line event JSON (`id`, `type`, `project`, `branch`, `local_path`, `pr`, `head_sha`, `payload`).
   - **Exit code `5`**: Another agent is already holding the `pd wait` lock. Inform the user that another agent is watching CI, then stop.
   - **Exit code `4`**: Timeout (when `--timeout` is set and expired without events).

2. **Handle `ci_failed` or `yellow_fix`**:
   1. Read `payload.failed_jobs` (`job_name`, `parent_job`, `file`, `line`, `error_summary`, `log_tail`).
   2. Verify `local_path` is checked out on the event's `branch` and that `branch` is a feature branch (first tier of `flow.chain`, e.g. `feature/*`).
   3. Edit the code in `local_path` to fix the failure and run the repository's local test suite until green.
   4. Commit and `git push` to the event's `branch`.
   5. Acknowledge the event with the new commit SHA:
      ```bash
      pd ack <event_id> --pushed <new_sha>
      ```

3. **Handle `review_changes`**:
   1. List all PR/MR comments:
      ```bash
      pd comments <project> <pr> --json
      ```
   2. Inspect each unhandled comment (`"seen": false` or in `payload.comment_ids`) and decide whether a code change is warranted.
   3. If code changes are needed: edit `local_path`, run local tests until green, commit, and `git push` to the event's feature `branch`.
   4. Reply to each addressed comment via `pd reply` (**do NOT resolve the review thread**):
      ```bash
      pd reply <project> <pr> <comment_id> "Fixed in <sha>: <brief summary>"
      ```
   5. Acknowledge the event and mark the comment IDs as handled:
      ```bash
      pd ack <event_id> --handled <comment_id_1> <comment_id_2> --pushed <new_sha>
      ```

4. **Handle `deploy_failed` or `verify_failed`**:
   1. Inspect the failure details from event payload (`env`, `sha`, `details` / `check_result` / `error`).
   2. **Code-only rule**: If the failure is caused by an application issue, create or check out a feature branch in `local_path`, fix the code, test locally, commit and `git push` following the full git flow.
   3. **No direct environment manipulation**: **STRICTLY FORBIDDEN** to use `kubectl`, `argocd`, or any direct environment modification commands (`kubectl edit`, `kubectl apply`, `argocd app sync`, etc.). All fixes must go through the standard code pipeline.
   4. Acknowledge the event:
      ```bash
      pd ack <event_id> --pushed <new_sha>
      ```

5. **Loop**: Immediately return to step 1 (`pd wait`) to watch the newly triggered pipeline.

## 3. Git & Safety Rules (Strict)

- **Feature branch only**: Only push the event's or current feature branch (must match the first tier of `flow.chain`, e.g. `feature/*`).
- **NEVER force push**: Never run `git push --force` or `git push --force-with-lease`.
- **NEVER push protected branches**: Never push directly to `develop`, `main`, `master`, or any non-feature branch.
- **Merge only via `pd decide`**: Never merge a PR/MR directly; merges must only happen through `pd decide` after user confirmation.
- **NEVER directly touch environments**: For `deploy_failed` or `verify_failed`, never run `kubectl`, `argocd`, cloud consoles, or SSH into servers to modify environments directly. Fix only via code changes through the repository flow.
