---
name: pipedog-pr-review
description: Inspect PR/MR review comments via pd comments, apply requested code changes, and reply via pd reply without resolving threads. Use when the user asks to 看 PR 留言, 處理 review 意見, 回覆 PR comment, handle PR review comments, or address review feedback.
---

# pipedog-pr-review — PR/MR Review Comment Handler

Use this skill to inspect PR/MR review comments and threads, apply necessary code fixes, push to the feature branch, reply to each comment, and mark comments as handled.

> **Security Rule**: Never read, inspect, echo, or print any GitLab/GitHub personal access tokens or secret environment variables (`PIPEDOG_GITLAB_TOKEN`, `GITLAB_TOKEN`, `PIPEDOG_GITHUB_TOKEN`, `GITHUB_TOKEN`, or token fields in `config.json`).

## 1. Prerequisite Check (`pd` in `PATH`)

Requirement: Python >= 3.8, no third-party packages.

Verify that `pd` is available on `PATH`. If `pd` is not found in `PATH`, prompt the user to install the `pipedog-setup` skill first and add `bin/pd` to `PATH` (run `npx skills list` to locate `<canonical copy>`, then `ln -s <canonical copy>/skills/pipedog-setup/bin/pd ~/.local/bin/pd`).

## 2. Fetch & Evaluate Comments

1. Fetch comments for the target project and PR/MR number:
   ```bash
   pd comments <project> <pr> --json
   ```
2. Review each comment (`id`, `kind`, `author`, `body`, `path`, `line`, `resolved`, `seen`):
   - Focus on comments where `"seen": false` (or those requested by the user).
   - Determine whether each comment requires a code change or a clarification reply.

## 3. Apply Fixes, Push, Reply & Acknowledge

1. If code changes are needed:
   - Verify the repository is checked out on the PR's source feature branch (matching the first tier of `flow.chain`, e.g. `feature/*`).
   - Edit the relevant files, run the local test suite until green, commit, and `git push` to the feature branch.
2. Reply to each addressed comment (**do NOT resolve the review thread**):
   ```bash
   pd reply <project> <pr> <comment_id> "Fixed in <sha>: <brief summary>"
   ```
3. If handling a `review_changes` event, acknowledge it and record the handled comment IDs:
   ```bash
   pd ack <event_id> --handled <comment_id_1> <comment_id_2> --pushed <new_sha>
   ```

## 4. Git & Merge Safety Rules (Strict)

- **Feature branch only**: Only push the event's or current feature branch (must match the first tier of `flow.chain`, e.g. `feature/*`).
- **NEVER force push**: Never run `git push --force` or `git push --force-with-lease`.
- **NEVER push protected branches**: Never push directly to `develop`, `main`, `master`, or any non-feature branch.
- **Merge only via `pd decide`**: Never merge a PR/MR directly; merges must only happen through `pd decide` after user confirmation.
