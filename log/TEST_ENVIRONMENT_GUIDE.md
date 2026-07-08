# 銀爺爺 LINE Bot 測試環境建置指南

> 建立日期：2026-07-08
> 目的：開發與測試時完全不影響線上用戶（真實會員、點數、交易）
> 適用架構：Railway（Flask + gunicorn）＋ Supabase（PostgreSQL + Storage）＋ LINE Messaging API ＋ Replicate ＋ Sentry

---

## 一、核心觀念

**好消息：程式碼已經為此準備好了。** 所有外部依賴（LINE、DB、Storage、Replicate、Sentry）都是透過**環境變數**注入，所以「開一套測試環境」本質上就是「準備另一組環境變數 + 另一組外部資源」，**不需要改任何程式碼**。

風險只有一個核心：測試時建立的假會員、扣點、交易，絕不能寫進線上資料。因此每個外部依賴都必須指向獨立的測試資源。

---

## 二、隔離原則（每個依賴都要獨立）

| 依賴 | 為什麼一定要隔離 | 測試環境怎麼做 |
|---|---|---|
| **LINE Channel** | 一個 channel 的 webhook URL **只能設一個**；共用會讓測試訊息混進真實用戶，webhook 也無法同時指兩邊 | 另開一個測試用 channel，取得獨立的 `CHANNEL_ACCESS_TOKEN` / `CHANNEL_SECRET` |
| **Supabase DB** | 測試會建假會員、改點數、寫交易 | 另開一個 Supabase 免費專案當 staging，套用相同 migration，填成測試的 `DATABASE_URL` |
| **Supabase Storage** | 圖片編輯的暫存圖 | 用測試專案自己的 bucket，`SUPABASE_URL` 等指向測試專案即可（跟著 DB 一起隔離） |
| **Replicate** | 會**真的計費**（測試也算錢） | 可共用同一個 token（單次成本低、量小可接受）；不想花錢就在測試環境 mock 掉 |
| **Sentry** | 測試錯誤會污染線上告警 | 設 `SENTRY_ENVIRONMENT=staging` 在 Sentry 分開看；或測試環境不設 `SENTRY_DSN` |

---

## 三、兩種測試環境，看需求選

| | 做法 A：本地 + ngrok | 做法 B：Railway 常駐 staging |
|---|---|---|
| 適用 | 開發時快速迭代 | demo、給家人試用、上線前驗收 |
| 迭代速度 | 最快（改一行存檔即重載） | 較慢（要 push / deploy） |
| 網址 | ngrok 隧道（可固定，見第五章） | Railway 固定網址 |
| 現成工具 | `test/start_local_server.py` | 另開一個 Railway service |

一般建議：**日常開發用 A，需要穩定測試站時再加 B**，兩者可並存。

---

## 四、建置步驟

### 4.1 開測試 LINE Channel

1. 前往 [LINE Developers Console](https://developers.line.biz/)
2. 在現有 provider 下建立一個新的 Messaging API channel（例如命名「銀爺爺-測試」）
3. 記下 **Channel access token** 和 **Channel secret**
4. 用手機加這個測試 Bot 為好友（測試時對它傳訊息）

### 4.2 開測試 Supabase 專案

1. 在 [Supabase](https://supabase.com/) 建立一個新的免費專案（例如「grandpa-yin-staging」）
2. **套用相同 schema**：schema 由另一個 repo（`altide-landing-page/supabase/migrations/`）管理，需在測試專案套用同一批 migration：
   ```bash
   # 於 altide-landing-page 專案，連到測試專案後
   supabase db push
   ```
   （或用 Supabase 後台 SQL Editor 手動執行 migration SQL。）
3. 記下測試專案的連線字串（Settings → Database）與 `SUPABASE_URL` / service role key
4. 在測試專案的 Storage 建立 bucket `linebot-temp-images`

### 4.3 準備 `.env`（範本見附錄）

複製 `env_example.txt`，填入**測試環境的值**。重點：`DATABASE_URL`、`CHANNEL_*`、`SUPABASE_*` 全部是測試專案的值，`SENTRY_ENVIRONMENT=staging`。

### 4.4 本地啟動（做法 A）

```bash
cd test
python start_local_server.py
```

此腳本會自動：啟動 Flask（開發模式，改碼自動重載）→ 啟動 ngrok → 抓到 ngrok 網址。接著把它印出的 `https://xxx/webhook` 填到**測試 channel** 的 Webhook URL 並開啟 Use webhook（第五章可讓這步免手動）。

### 4.5 Railway 常駐 staging（做法 B，可選）

1. 在 Railway 開第二個 service，連結同一個 git repo 但指向測試分支（例如 `develop`）
2. 在該 service 的 Variables 填入整組**測試環境**變數（同 4.3）
3. 部署後取得 Railway 網址，填到測試 channel 的 webhook

---

## 五、解決「webhook 每次要重填」的痛點

ngrok 免費版每次重啟給**隨機網址**，所以每次都要回 Console 重填。兩個解法：

### 解法 A：ngrok 固定網址（最簡單，一勞永逸）

ngrok 免費方案送每個帳號一個固定的 static domain。到 ngrok Dashboard → Domains 領取後：

```bash
ngrok http --url=your-name.ngrok-free.app 5000
```

URL 永遠不變 → webhook 在 Console **填一次，永久有效**。`start_local_server.py` 只要把 ngrok 啟動命令加上 `--url` 參數即可（建議接受 `NGROK_DOMAIN` 環境變數）。**這一個就解決 90% 的痛。**

### 解法 B：腳本自動設定 webhook（連填一次都省）

LINE 提供 API 可用程式設定 webhook，不必進 Console：

```
PUT https://api.line.me/v2/bot/channel/webhook/endpoint
Authorization: Bearer {CHANNEL_ACCESS_TOKEN}
Body: {"endpoint": "https://.../webhook"}
```

`start_local_server.py` 已經會抓到 ngrok URL，只要把 `setup_line_webhook()` 從「印出來叫你手動填」改成「呼叫此 API 自動設定 + 驗證」，即使 URL 每次變也會自動設好。

**建議**：只想快點解決 → 做 A；想完全自動 → A + B 一起（URL 固定又自動設定，跑一次腳本全搞定）。

> 註：以上兩個腳本改動尚未實作，需要時再動手。目前 `start_local_server.py` 是「抓到 URL 後印出、手動填」的流程。

---

## 六、驗證測試環境確實隔離（重要）

設定完，**務必先確認連的是測試庫而非線上**，再開始測：

```bash
# 用一個「線上一定存在、測試庫一定沒有」的會員名稱查詢
python scripts/trace_user.py 某個線上真實用戶的名字
```

- 若回「找不到」→ 正確，連的是空的測試庫，可以放心測
- 若查得到真實用戶資料 → **危險！`DATABASE_URL` 指到線上了，立刻停下修正**

---

## 七、常見坑

1. **絕不要把測試的 `DATABASE_URL` 指到線上 DB** —— 唯一會釀成災難的錯誤（測試腳本一跑就污染真實點數）。用第六章的方式先驗證。
2. **測試 DB 要套用相同 schema** —— migration 在 `altide-landing-page` repo，新開的 Supabase 專案要跑過一遍，否則程式報 `column/table does not exist`。
3. **webhook 一個 channel 只能填一個 URL** —— 這就是測試必須用獨立 channel、不能「線上測試共用一個 channel 切換」的原因。
4. **Replicate 共用 token 會計費** —— 測試也算錢，量大時考慮 mock 或設獨立低額度帳號。
5. **`.env` 不可 commit** —— 已在 `.gitignore`；Railway 各環境的變數各自獨立設定。

---

## 附錄：`.env`（測試環境範本）

```env
# === LINE：填「測試 channel」的值 ===
CHANNEL_ACCESS_TOKEN=測試channel的access_token
CHANNEL_SECRET=測試channel的secret

# === Replicate：可與線上共用（會計費）===
REPLICATE_API_TOKEN=your_replicate_api_token

# === 應用程式 ===
PORT=5000
FLASK_DEBUG=True          # 本地開發開啟自動重載
LOG_LEVEL=DEBUG           # 測試時看詳細 log

# === Sentry：與線上分開，或留空不啟用 ===
SENTRY_DSN=
SENTRY_ENVIRONMENT=staging

# === 資料庫：填「測試 Supabase 專案」的連線字串 ===
DATABASE_URL=postgresql://postgres:[測試專案密碼]@db.[測試PROJECT-ID].supabase.co:5432/postgres

# === Supabase Storage：填測試專案的值 ===
SUPABASE_URL=https://[測試PROJECT-ID].supabase.co
SUPABASE_SERVICE_ROLE_KEY=測試專案的service_role_key
SUPABASE_STORAGE_BUCKET=linebot-temp-images

# === 功能費用：可設低一點方便測試 ===
COLORIZE_COST=10
EDIT_COST=5
WELCOME_POINTS=50

# === 固定 ngrok 網址（領了 static domain 再填，見第五章）===
# NGROK_DOMAIN=your-name.ngrok-free.app

# === 圖片處理併發（可選，預設 4 / 8）===
# IMAGE_WORKERS=4
# IMAGE_QUEUE_LIMIT=8
```
