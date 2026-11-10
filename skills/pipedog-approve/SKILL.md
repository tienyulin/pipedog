---
name: pipedog-approve
description: List and answer pending flow gate decisions (yellow light, merge PR/MR, promote branch), inspect decision history (what did I approve), and manage yellow-light rules via pd decisions, pd decide, and pd rules. Use when the user asks to 放行黃燈, 同意 merge, 開 promote PR, 管理黃燈規則, approve merge decision, allow yellow light, or check what was approved/decided.
---

# pipedog-approve — Flow Gate Decisions & Yellow-Light Rules

Use this skill when the user wants to inspect or answer pending decisions (`yellow`, `merge`, `promote`, `deploy`, `deploy_approval`, `rollback`, `deploy_stuck`), inspect decision history, or manage saved yellow-light rules.

> **Security Rule**: Never read, inspect, echo, or print any GitLab/GitHub personal access tokens or secret environment variables (`PIPEDOG_GITLAB_TOKEN`, `GITLAB_TOKEN`, `PIPEDOG_GITHUB_TOKEN`, `GITHUB_TOKEN`, or token fields in `config.json`).

## 1. Prerequisite Check (`pd` in `PATH`)

Requirement: Python >= 3.8, no third-party packages.

Verify that `pd` is available on `PATH`. If `pd` is not found in `PATH`, prompt the user to install the `pipedog-setup` skill first and add `bin/pd` to `PATH` (run `npx skills list` to locate `<canonical copy>`, then `ln -s <canonical copy>/skills/pipedog-setup/bin/pd ~/.local/bin/pd`).

## 2. Inspect Pending Decisions

List pending decisions:

```bash
pd decisions
pd decisions --json
```

Each decision has `id`, `kind` (`yellow` | `merge` | `promote` | `deploy` | `deploy_approval` | `rollback` | `deploy_stuck`), `project`, `source`, `target`, `pr`, `head_sha`, and `reasons`.

### Inspect Decision History ("what did I approve")

List resolved (answered or void) decisions newest first to see who decided what and when:

```bash
pd decisions --history
pd decisions --history --limit 10
pd decisions --history --project <project>
pd decisions --history --json
```

Outputs one line per decision showing local time, kind, project, source→target, commit SHA (7 chars), answer or void reason, and who decided it (`by <answered_by>`). Respects `PIPEDOG_ASCII=1` for ASCII arrows (`->`).

## 3. Submit Decision (`pd decide` or TUI keys)

Run `pd decide <id> <answer> [--remember]` according to `kind` (or press hotkey in interactive TUI):

| `kind` | Valid `<answer>` | Key | Meaning |
|---|---|---|---|
| `yellow` | `allow --remember` | `a` | Allow this yellow light and save a matching rule to `~/.config/pipedog/rules.json` for future runs |
| `yellow` | `once` | `o` | Allow this yellow light for the current commit SHA only |
| `yellow` | `fix` | `f` | Emit a `yellow_fix` event so the agent fixes the underlying job |
| `yellow` | `skip` | `n` | Leave as-is without advancing |
| `merge` | `merge` | `m` | Instruct the running `pd` daemon to re-verify `head_sha` + review and merge the PR/MR |
| `merge` | `skip` | `n` | Do not merge now |
| `promote` | `open` | `p` | Instruct the running `pd` daemon to open the promotion PR/MR (e.g. `develop → main`) |
| `promote` | `skip` | `n` | Do not open the promotion PR now |
| `deploy` | `deploy` | `d` | Trigger deployment to target environment |
| `deploy` | `skip` | `n` | Do not deploy now |
| `deploy_approval` | `approve` | `v` | Approve deployment gate on platform (e.g. GitHub environment approval) |
| `deploy_approval` | `reject` | `x` | Reject deployment gate on platform |
| `deploy_approval` | `skip` | `n` | Postpone approval decision |
| `rollback` | `rollback` | `b` | Execute rollback to last known-good deployment |
| `rollback` | `skip` | `n` | Keep current deployment without rollback |
| `deploy_stuck` | `retry` | `t` | Retry stuck deployment |
| `deploy_stuck` | `skip` | `n` | Leave stuck deployment as-is |

## 4. Manage Saved Yellow-Light Rules (`pd rules`)

- List saved rules:
  ```bash
  pd rules list
  pd rules list --json
  ```
- Remove a saved rule by ID:
  ```bash
  pd rules rm <rule_id>
  ```
