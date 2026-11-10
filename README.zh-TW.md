[English](README.md)

# pipedog (`pd`)

```text
        __              _                _
(\_____/|''-o     _ __ (_) _ __  ___  __| | ___  __ _
(     (_|\_/     | '_ \| || '_ \/ -_)/ _` |/ _ \/ _` |
 \  __  /        | .__/|_|| .__/\___|\__,_|\___/\__, |
  \_) \_)        |_|      |_|                   |___/
```

**pipedog**（`pd`）是常駐終端機的 CI/CD pipeline 監控與分支流程自動化（Flow Automation）工具，同時也是可透過 `npx skills` 安裝的 AI Agent Skill 套件。支援 **GitLab CI** 與 **GitHub Actions**。

pipedog 可依設定的分支鏈（如 `feature/*` → `develop` → `main`）自動開 PR/MR、依 gate 規則在待決定佇列中詢問 merge / promote / deploy，追蹤持續部署（Continuous Deployment, CD）與自動健康驗證及 rollback，並透過 `pd wait` 將 CI 紅燈與 review 修改建議交給當下的 AI coding agent 自動修復。

---

## 系統需求

- **Python** `>=3.8`（僅使用標準庫，無任何第三方套件依賴）

---

## 安裝

### 1. 安裝 Agent Skills

本 repo 內含 6 個專責 skills（`pipedog-setup`、`pipedog-ci-status`、`pipedog-ci-fix`、`pipedog-ci-watch`、`pipedog-pr-review`、`pipedog-approve`）。使用 `npx skills` 預設會一次安裝全部 6 個 skills（可依需求指定多個 agent，加上 `-a <agent>`）：

```bash
npx skills add tienyulin/pipedog -g -a claude-code -a antigravity
```

若只需要安裝特定 skill（例如只裝唯讀查看狀態的 `pipedog-ci-status`），可加上 `--skill`：

```bash
npx skills add tienyulin/pipedog -g -a claude-code -a antigravity --skill pipedog-ci-status
```

| Skill | 路徑 | 職責 |
|---|---|---|
| `pipedog-setup` | `skills/pipedog-setup/SKILL.md` | 核心安裝、設定 `~/.config/pipedog/config.json`、`PATH` 設定、啟動 `pd` 與 `pd --mock` 試玩 |
| `pipedog-ci-status` | `skills/pipedog-ci-status/SKILL.md` | 唯讀查詢 CI/CD 狀態（`pd status --json` / `pd --once`），絕不修改程式碼 |
| `pipedog-ci-fix` | `skills/pipedog-ci-fix/SKILL.md` | 單次修復目前 CI 紅燈（定位 `failed_jobs` → 修復程式碼 → 本地測試 → 安全 push feature branch → `pd ack`） |
| `pipedog-ci-watch` | `skills/pipedog-ci-watch/SKILL.md` | 透過 `pd wait` 持續顧 CI 循環，自動處理 CI 紅燈與 PR/MR review 修改建議 |
| `pipedog-pr-review` | `skills/pipedog-pr-review/SKILL.md` | 處理 PR/MR 留言（`pd comments` → 評估並修改 → push → `pd reply` 不 resolve thread → `pd ack --handled`） |
| `pipedog-approve` | `skills/pipedog-approve/SKILL.md` | 處理待決定佇列（`allow` / `once` / `fix` / `merge` / `open` / `deploy` / `approve` / `reject` / `rollback` / `retry` / `skip`）與 `--remember` 規則管理 |

### 2. 將 `pd` 放進 `PATH`

透過 `npx skills list` 查詢 skill 的 canonical copy 安裝路徑，再將其中的 `bin/pd` symlink 到 `~/.local/bin/pd`（請確保 `~/.local/bin` 已加入 `PATH`）：

```bash
ln -s <canonical copy>/skills/pipedog-setup/bin/pd ~/.local/bin/pd
```

---

## 快速試玩（`--mock` 模式）

不需要任何設定檔或 API token，即可啟動內建的模擬世界體驗 Flow Automation、待決定佇列（`⚠ WAITING FOR YOU`）、即時錯誤解析與 CD 流程：

```bash
pd --mock
```

- **啟動時**：
  - `payment-api`（`feature/checkout→main`）已有 approved + CI green 的 PR，產生 `merge` 待決定項目。
  - `web-frontend`（`feature/ui→develop`）有 `allow_failure` 的 eslint 失敗，產生 `yellow` 黃燈待決定項目。
  - `auth-service`：staging 環境自動部署並驗證通過，production 環境產生 `deploy` 待決定項目。
- **啟動 5 秒後**：`auth-service` 的 `test 2/3` 轉為 `failed`（解析出 `tests/test_auth.py:42 AssertionError`），並發出 `ci_failed` 事件供 `pd wait` 接收。
- **持續推進**：`web-frontend` staging 環境部署後驗證失敗，自動觸發 `rollback` 回 known-good 版本。
- **靜態 Gallery 檢視**：執行 `pd --mock --gallery` 可一次輸出包含所有 pipeline、review、flow 與 environment 情境的靜態面板（可加上 `--ascii` 或 `PIPEDOG_ASCII=1` 切換 ASCII 模式）。

---

## TUI 互動操作與熱鍵

在即時互動式 `pd` 終端畫面中：

### 導覽與畫面控制

- `1`–`9`：直接以數字鍵選取對應編號的待決定項目。
- `↑` / `↓` 或 `k` / `j`：在待決定項目間上下移動游標選取。
- `w`：在預設瀏覽器中開啟該項目的網頁連結（PR/MR 連結或部署目標網址；在 SSH 或 headless Linux 環境下則印出網址）。
- `p`（未選取任何待決定項目時）：切換收合／展開所有已通過的 pipelines（`p collapse passed` / `p expand passed`）。
- `q`：退出 `pd`。

### 針對所選項目的操作熱鍵

- `a`（或 `o`）：`once` — 僅放行此 commit 的黃燈。
- `A`：`allow`（含 `--remember`） — 放行黃燈並將規則寫入 `rules.json`。
- `f`：`fix` — 發出 `yellow_fix` 事件交由 agent 自動修復。
- `m`：`merge` — 重新驗證並 merge 該 PR/MR。
- `p`：`open` — 建立推進分支的 PR/MR（例如 `develop` → `main`）。
- `d`：`deploy` — 觸發部署至目標環境。
- `v`：`approve` — 核准平台環境的部署 gate（如 GitHub environment approval）。
- `x`：`reject` — 拒絕平台環境部署。
- `b`：`rollback` — 回退至上一版 known-good 部署。
- `t`：`retry` — 重試卡住的部署。
- `n`：`skip` — 跳過／暫不決定。

### 安全確認分級機制

為避免誤觸 merge 或造成 production 事故，操作具備分級確認保護：

- **低風險（即時執行）**：黃燈操作（`a`、`A`、`o`、`f`）與跳過決定（`n`）會立即執行。
- **中風險（連按確認）**：PR/MR merge（`m`）、分支 promote（`p`）與非 production 環境的部署／核准／rollback（`d`、`v`、`b`），需在 3 秒內再次按下相同按鍵確認（`press <key> again within 3s to <action>`）。按 Esc 或逾時將取消操作。
- **高風險（輸入 `prod` + Enter）**：針對 `production` 環境的任何部署、核准、rollback 或重試操作，畫面會提示要求手動輸入 `prod` 並按 Enter 確認（`type prod + Enter to <action> <sha> on <env>, Esc cancels:`）。

---

## 設定與 Flow Automation

### 快速開始：`pd init`

在專案 git 倉庫目錄下，一行指令即可自動產生可用設定，且絕不在設定檔中寫入 Token：

```bash
pd init
```

選項：
- `--path DIR`：目標 git 倉庫目錄（預設為當前目錄）。
- `--flow auto|gitflow|github|none`：分支流程策略（預設 `auto`：偵測到 `origin/develop` 時使用 Git Flow `["feature/*", "develop", <default>]`，否則使用 GitHub Flow `["feature/*", <default>]`）。
- `--name NAME`：專案名稱覆寫（預設為倉庫目錄名稱）。
- `--config PATH`：目標設定檔路徑（`--config` > `PIPEDOG_CONFIG` > `~/.config/pipedog/config.json`）。
- `--dry-run`：印出產生的專案 JSON 項目，不寫入檔案。
- `--force`：若專案已設定過，強制以新設定替換。

執行 `pd init` 後，設定環境變數 Token（`export PIPEDOG_GITHUB_TOKEN=...` 或 `gh auth login`；GitLab 則為 `export PIPEDOG_GITLAB_TOKEN=...`），接著執行 `pd doctor` 驗證設定即可。

### 手動建立設定檔

預設設定檔路徑為 `~/.config/pipedog/config.json`（可用 `--config PATH` 或環境變數 `PIPEDOG_CONFIG` 覆寫）：

```bash
mkdir -p ~/.config/pipedog
cp <canonical copy>/skills/pipedog-setup/config.example.json ~/.config/pipedog/config.json
```

支援 Git Flow 與 GitHub Flow 的 `config.json` 範例：

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

- `flow.chain`：分支推進鏈（至少 2 層）。第一層為 feature 分支 glob（如 `"feature/*"`），後續為目標分支（Git Flow `["feature/*", "develop", "main"]` 或 GitHub Flow `["feature/*", "main"]`）。未設定 `flow` 的專案維持純監控模式。
- `flow.lanes`：額外 entry branches（如 `hotfix/*`、`release/*`），直接 merge 至目標 chain branch（如 `main`），並支援自動開啟 back-merge PR（如回合至 `develop`）。
- `flow.max_fix_rounds`：同一分支連續自動修復次數上限（預設 `3`）。
- `flow.merge`：各階段 merge 方式（`method`: `"squash"` 或 `"merge"`；`delete_branch`: `true` 或 `false`）。
- `deploy`：可選的持續部署設定區塊。`after_branch` 指定綠燈後觸發部署的分支。
  - `environments`：目標部署環境清單。`adapter` 可選 `"gitlab"`、`"github"` 或 `"command"`。
  - `trigger`：`"auto"`（綠燈自動觸發部署）或 `"ask"`（產生待決定項目等待確認）。
  - `verify`：自動化健康檢查，支援 HTTP 端點驗證（`url`、`expect_status`、`duration_sec`、`interval_sec`、`fail_after`）或外部指令（`command`）。
  - `rollback`：`"auto"`（驗證失敗自動 rollback 回已知正常版本）或 `"ask"`（產生 rollback 待決定項目）。

### Command Adapter 範本

當使用 `adapter: "command"` 時，可透過 shell 指令整合任何部署工具。系統會自動替換 `{sha}`、`{previous_sha}`、`{previous_id}`、`{env}` 與 `{branch}` 佔位符：

- **Argo CD (`parse: "argocd"`)**：
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

- **kubectl rollout (`parse: "kubectl-rollout"`)**：
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

- **Vercel CLI (`parse: "json"`)**：
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

### 2. 設定 API Token

建議使用環境變數設定 token：

- **GitLab**（啟用 `flow` 寫入需具備 `api` scope）：
  1. `export PIPEDOG_GITLAB_TOKEN="glpat-..."`
  2. `export GITLAB_TOKEN="glpat-..."`
  3. `config.json` 中的 `gitlab.token`
- **GitHub**（需具備 `repo` scope）：
  1. `export PIPEDOG_GITHUB_TOKEN="ghp_..."`
  2. `export GITHUB_TOKEN="ghp_..."`
  3. `config.json` 中的 `github.token`
  4. 若本機已登入 GitHub CLI（`gh auth login`），會自動透過 `gh auth token` 取得

### 3. Webhook 通知設定（選用）

可設定發送外送通知至 Slack、Discord 或自訂 JSON 端點：

```json
"webhooks": [
  {
    "url_env": "PIPEDOG_WEBHOOK_URL",
    "format": "slack",
    "events": ["failed", "decision", "deploy"]
  }
]
```

- `url_env`：存放 webhook 網址的環境變數名稱（建議使用）。
- `url`：直接指定 webhook 網址（必須使用 HTTPS，禁止 loopback 與私有 IP 網段）。
- `format`：`"slack"`、`"discord"` 或 `"json"`。
- `events`：發送通知的事件陣列（`"failed"`、`"decision"`、`"deploy"`）。

---

## 指令與子指令一覽

### 常駐監控

```bash
pd [--mock] [--config PATH] [--once] [--interval SEC] [--no-notify] [--gallery] [--ascii]
```

### Flow、CD 與 Agent 協作子指令

| 指令 | 說明 | Exit Code |
|---|---|---|
| `pd init [--path DIR] [--flow auto\|gitflow\|github\|none] [--name NAME] [--config PATH] [--dry-run] [--force]` | 從目前 git 倉庫產生專案設定並寫入 config.json | `0` 成功寫入；`2` 錯誤；`3` 重複設定 |
| `pd status [--json] [--no-tick]` | 顯示目前狀態畫面（或 `--json` 印出 `active_status.json`） | `0`；無 snapshot `2` |
| `pd envs [--json]` | 列出各環境部署狀態（版本 SHA、狀態、驗證結果、部署時間） | `0` |
| `pd wait [--timeout SEC]` | 阻塞等待需要 agent 處理的事件（自動推進 flow），印出單筆事件 JSON | `0` 成功取得事件；`4` timeout；`5` 已有其他 waiter；`2` 設定錯誤 |
| `pd tick` | 執行一輪 flow engine tick 並更新 snapshot 後結束 | `0`；`2` 設定錯誤 |
| `pd ack <event_id> [--handled ID...] [--pushed SHA]` | 回報已處理的事件，附帶處理過的留言 ID 與新 commit SHA | `0`；不存在 `2` |
| `pd decisions [--json]` | 列出目前待決定的項目（`yellow` / `merge` / `promote` / `deploy` / `deploy_approval` / `rollback` / `deploy_stuck`） | `0` |
| `pd decisions --history [--limit N] [--project P] [--json]` | 查看已決定的歷史紀錄，顯示由誰在何時核准或決定 | `0` |
| `pd decide <id> <answer> [--remember]` | 回答待決定項目（`allow` / `once` / `fix` / `merge` / `open` / `deploy` / `approve` / `reject` / `rollback` / `retry` / `skip`；`allow --remember` 會儲存規則） | `0`；無效 `2` |
| `pd rules list [--json]` / `pd rules rm <id>` | 查看或刪除已記住的黃燈規則 | `0`；規則檔損毀 `2` |
| `pd comments <project> <pr> [--json]` | 列出 PR/MR 所有留言與 review thread（標註 `seen`） | `0`；錯誤 `2` |
| `pd reply <project> <pr> <comment_id> <text>` | 回覆 PR/MR 留言（不自動 resolve thread） | `0`；錯誤 `2` |
| `pd doctor [--offline] [--json] [--config PATH]` | 檢查 pipedog 運作環境、設定檔、Token、Scope 與 API Rate Limit | `0` 正常；`1` 警告；`2` 失敗；`3` 異常崩潰 |
| `pd install-tick [--interval SEC=300]` | 安裝 macOS launchd 定期排程（預設 300 秒，記錄於 `tick.log`） | `0`；`2` 非 macOS 或失敗 |
| `pd uninstall-tick` | 移除 macOS launchd 定期排程 | `0`；`2` 非 macOS 或失敗 |

---

## 背景定期推進排程（選用）

pipedog 無需常駐背景 daemon。`pd wait` 與 TUI `pd` 在執行時每次輪詢都會自動推進分支流程與部署。此外，`pd status` 在有部署進行中或資料過期時也會先推進一次。

若希望在完全未開啟終端機與 agent 時也能定期推進，可選擇性安裝背景排程：

- **macOS（launchd 定期排程）**：
  ```bash
  pd install-tick
  pd install-tick --interval 180
  pd uninstall-tick
  ```
  寫入 `~/Library/LaunchAgents/dev.pipedog.tick.plist` 並記錄輸出於 `<state_dir>/tick.log`（例如 `~/.local/state/pipedog/tick.log`）。
- **Linux（crontab 定期排程）**：
  請手動透過 `crontab -e` 加入：
  ```cron
  */5 * * * * <pd 路徑> tick >> ~/.local/state/pipedog/tick.log 2>&1
  ```
- **日誌輪替**：`pd tick` 執行時若 `tick.log` 超過 1 MiB，會自動以原子方式寫入並保留最後 256 KiB。

---

## 疑難排解：`pd doctor`

當遇到認證、Token、權限、API 流量限制或引擎鎖定等問題時，請先執行 `pd doctor` 診斷：

```bash
pd doctor
```

常用選項：
- `--offline`：跳過所有網路呼叫，僅檢查本機 Python、設定檔語法與狀態目錄。
- `--json`：以結構化 JSON 輸出健康檢查結果。
- `--config PATH`：指定特定設定檔進行檢查。

Exit codes：
- `0`：全部檢查通過。
- `1`：檢測到警告（例如 API 剩餘配額偏低、未安裝選用排程）。
- `2`：一項或多項檢查失敗（例如設定檔無效、Token 遺失或權限過期）。
- `3`：診斷程式異常崩潰。
