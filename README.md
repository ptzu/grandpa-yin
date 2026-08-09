# 銀爺爺 LINE Bot

專為長輩設計的 LINE Bot：用 AI 幫黑白老照片上色、依文字描述編輯圖片，並內建點數會員系統。與 Altide 共用一套會員／錢包（見 `log/MEMBER_SYSTEM_README.md`）。

技術架構：**Flask + gunicorn**（Railway 部署）· **Supabase PostgreSQL** · **LINE Messaging API** · **Replicate**（AI 模型）· **Sentry**（錯誤追蹤）。

## 功能

| 功能 | 觸發指令 | 說明 | 點數 |
|---|---|---|---|
| 功能選單 | `!功能`、`使用說明` | Quick Reply 選單，引導其餘功能 | — |
| 圖片彩色化 | `圖片彩色化` | 上傳黑白照 → AI 自動上色 | `COLORIZE_COST`（預設 10）|
| 圖片編輯 | `圖片編輯` | 先傳圖、再輸入文字描述 → AI 依描述編輯 | `EDIT_COST`（預設 5）|
| 會員／點數 | `會員`、`點數`、`歷史` | 查詢點數餘額與交易記錄 | — |

- 新用戶加好友自動建立會員並贈送 `WELCOME_POINTS`（預設 50）點。
- 圖片以背景非同步處理，不阻塞使用者；多用戶對話狀態持久化於 `grandpa_yin.bot_sessions`。

## 開啟測試環境

完整說明見 [`log/TEST_ENVIRONMENT_GUIDE.md`](log/TEST_ENVIRONMENT_GUIDE.md)。快速版（本地開發，零雲端資料庫）：

### 1. 安裝依賴

```bash
pip install -r requirements.txt      # Python 套件
brew install --cask ngrok            # 對外隧道（LINE webhook 需要公網入口）
```

### 2. 設定 `.env`

```bash
cp env_example.txt .env
```

填入**測試用** LINE channel 的 `CHANNEL_ACCESS_TOKEN` / `CHANNEL_SECRET`（勿用正式 channel）、`REPLICATE_API_TOKEN`，以及資料庫（見下）。`.env` 已被 `.gitignore` 排除。

### 3. 建本地測試資料庫

用本機 Postgres，一鍵照 model 建表：

```bash
createdb grandpa_yin_dev
# .env 設 DATABASE_URL=postgresql://<user>@localhost:5432/grandpa_yin_dev
python test/setup_test_db.py
```

`setup_test_db.py` 內建安全鎖：只允許 `DATABASE_URL` 指向本機，避免誤建到線上。
> 註：本地表由 SQLAlchemy model 生成，不含線上的 CHECK / RLS / trigger，足夠功能測試；要與線上完全一致請改用 Supabase staging 專案並套用 `altide-landing-page/supabase/schema.sql`。

### 4. ngrok（一次性設定）

```bash
ngrok config add-authtoken <your-token>   # dashboard.ngrok.com 取得
```

（可選）到 ngrok Dashboard → Domains 領一個固定網域，填進 `.env` 的 `NGROK_DOMAIN`，webhook 網址就永久不變。

### 5. 一鍵啟動

```bash
cd test
python start_local_server.py
```

腳本會自動啟動 Flask（改碼熱重載）→ 啟動 ngrok（有 `NGROK_DOMAIN` 就用固定網址）→ **自動呼叫 LINE API 設定並驗證 webhook**，不需手動進 Console 填。

### 6. 首次 LINE Console 設定（只需一次）

Webhook **網址**由腳本自動設定，但以下幾項是 Console 專屬、API 改不了，第一次要在測試 channel 的 Messaging API 頁手動處理：

- **開啟「Use webhook」** — API 只能設網址，這個「是否真的把訊息送到 webhook」的開關要手動打開（打開後就一直有效）。
- **關閉「Auto-reply messages」** — 否則 LINE 官方罐頭回覆會插嘴。
- **加測試 Bot 為好友** — 掃 Console 的 QR code，才能對它傳訊息測試。

設定完後，日常只要跑第 5 步即可，網址每次自動更新，不必再碰 Console。
> 未設 `NGROK_DOMAIN` 時 ngrok 為隨機網址，重跑腳本會變（腳本會自動重設 webhook）；設了固定網域則永久不變。

## 部署（Railway）

1. 在 [Railway](https://railway.app/) 建專案並連結此 repo。
2. 於 Variables 設定環境變數：`CHANNEL_ACCESS_TOKEN`、`CHANNEL_SECRET`、`REPLICATE_API_TOKEN`、`DATABASE_URL`（Supabase 連線字串），可選 `COLORIZE_COST` / `EDIT_COST` / `WELCOME_POINTS`、`SENTRY_DSN` / `SENTRY_ENVIRONMENT`。
3. Push 到 `main` 即自動部署（啟動指令見 `Procfile`：`gunicorn app:app -w 2 --threads 8`）。
4. 將 Railway 網域設為 LINE Webhook：`https://<your-app>.up.railway.app/webhook`。

### 資料庫 schema 與自動 migration

schema 分兩層、各自管理：

- **共用層 `public.*`**（accounts / transactions / linked_identities）由 Altide 的 `altide-landing-page/supabase/schema.sql` 管理（含 `auth.*` / `storage.*` 依賴，僅適用於 Supabase）。本專案**不碰**。
- **產品層 `grandpa_yin.*`**（bot_sessions / usage_logs / user_profiles）由本專案的 **Alembic** 管理，migration 檔在 `alembic/versions/`。

每次部署，Railway 的 `preDeployCommand`（見 `railway.json`）會自動執行 `alembic upgrade head`，把 `grandpa_yin.*` 的 schema 更新到最新；失敗則中止部署（不會帶著壞 schema 上線）。

**改動流程**：改 `models/*.py` → `alembic revision --autogenerate -m "描述"` → 檢視產生的 migration → commit → push。部署時自動套用。

> **首次導入（線上 DB 已有 grandpa_yin.* 表時，只需做一次）**：因為表早已由 `schema.sql` 建好、但還沒有 Alembic 版本記錄，第一次啟用前要先把現況標記為 baseline，否則 `upgrade` 會嘗試重建已存在的表而失敗：
> ```bash
> DATABASE_URL=<線上連線字串> alembic stamp head
> ```
> 標記後，之後的部署才會只套用「新的」migration。

## 管理腳本

```bash
python scripts/add_member.py                 # 互動式新增會員／加點
python scripts/cleanup_user_states.py 24     # 清理超過 24 小時的舊對話狀態
python scripts/trace_user.py <名字>          # 追查某會員的點數異動（排查用）
```

## 專案結構

```
app.py                    主入口：webhook、初始化、訊息路由
features/                 功能模組（繼承 base_feature.BaseFeature）
  feature_registry.py     訊息路由與功能註冊
  menu / colorize / edit / member_feature.py
  replicate_feature.py    Replicate 模型呼叫共用邏輯
services/
  member_service.py       會員服務層（點數、交易）
  storage_service.py      Supabase Storage（圖片暫存）
models/                   SQLAlchemy 模型（public.* 共用層 + grandpa_yin.* 產品層）
scripts/                  管理／排查腳本
test/                     start_local_server.py（一鍵啟動）、setup_test_db.py、test_local.py
log/                      設計文件與維運手冊
Procfile                  gunicorn 啟動指令
```

## 新增功能

繼承 `features/base_feature.py` 的 `BaseFeature`，實作 `name` / `can_handle` 與訊息處理方法，再於 `app.py` 初始化區以 `feature_registry.register(...)` 註冊即可。
