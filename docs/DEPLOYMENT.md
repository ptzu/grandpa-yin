# 部署文件（上線指南）

> 適用架構：Railway（Flask + gunicorn）＋ Supabase（PostgreSQL + Storage）＋ LINE Messaging API ＋ Replicate ＋ Sentry
> 相關文件：[測試環境](./TEST_ENVIRONMENT.md)、[系統健檢](./HEALTH_CHECK.md)、[開發日誌](./DEVELOPMENT_LOG.md)

---

## 一、架構總覽

```
用戶 LINE App
   │  webhook
   ▼
LINE Platform ──► Railway (Flask /webhook, gunicorn -w 2 --threads 8)
                     │
                     ├─► Supabase PostgreSQL（會員、點數、狀態、usage_logs）
                     ├─► Supabase Storage（圖片編輯的暫存圖）
                     ├─► Replicate API（彩色化 / 圖片編輯，背景執行緒池 4 workers + 8 queue）
                     ├─► LINE Messaging API（reply / push / loading animation）
                     └─► Sentry（錯誤追蹤）
```

所有外部依賴都由**環境變數**注入，換環境不需改程式碼。

---

## 二、部署模式：Platform / Standalone

由環境變數 `DEPLOY_MODE` 決定（未設定時預設 `platform`）：

| | `platform`（整合進 Altide） | `standalone`（獨立） |
|---|---|---|
| 身份來源 | Altide `public.linked_identities` → `accounts` | 自有 `grandpa_yin.subjects` |
| 點數/交易 | Altide `public.accounts.points_balance` / `transactions` | 自有 `grandpa_yin.wallet_transactions` |
| 依賴 Altide | 是（需先有 `public.*` 共用層） | 否（只需 `grandpa_yin.*`） |

切換點集中在 `src/services/account_backend.py`（`AccountBackend` port + 兩個 adapter），業務邏輯只認 port。細節見[開發日誌](./DEVELOPMENT_LOG.md)。

**要從零建一套獨立環境（新的 Supabase 專案、不依賴 Altide）看第五章**，那裡是完整的 standalone 建置流程。

> 目前線上跑哪個模式，以 Railway Variables 的 `DEPLOY_MODE` 為準（未設定＝`platform`）。除錯 SQL 也分兩套，見第七章——**表名與欄位不同，別照抄錯的那套**。

---

## 三、Railway 部署步驟

1. 在 [Railway](https://railway.app/) 建專案並連結此 repo。
2. **設定環境變數**（Variables，見第四章清單）。
3. Push 到 `main` 即自動部署（啟動指令見 `Procfile`）。
4. 將 Railway 網域設為 LINE Webhook：`https://<your-app>.up.railway.app/webhook`，並在 LINE Console 開啟 **Use webhook**。

> 從零建一套全新的獨立環境（含新的 Supabase 專案）請走**第五章**，那裡有含 Supabase 設定、連線方式、建表與驗收的完整順序。本章只講 Railway 這一側。

> ⚠️ **Railway 陷阱**：若曾在 Settings → Deploy → **Custom Start Command** 手動填過啟動指令，它會**覆蓋 `Procfile`**。改 `Procfile` 卻沒生效時先查這裡。

### 環境變數改動要 Redeploy

Railway 改 Variables **不會**自動重啟舊容器的 process 內快取，改完務必手動 Redeploy。

---

## 四、環境變數清單

| 變數 | 必填 | 說明 |
|---|---|---|
| `CHANNEL_ACCESS_TOKEN` | ✅ | LINE channel access token |
| `CHANNEL_SECRET` | ✅ | LINE channel secret（webhook 簽章驗證用） |
| `REPLICATE_API_TOKEN` | ✅ | Replicate API token（會計費） |
| `DATABASE_URL` | ✅ | Postgres 連線字串（見第五章） |
| `DEPLOY_MODE` | | `platform`（**未設定時的預設**）/ `standalone`。獨立環境**必須明寫** `standalone` |
| `SUPABASE_URL` | ⭕ | Supabase 專案 URL（圖片 Storage）。技術上可省略，但**正式環境視為必填**——未設定時圖片會以 base64 塞進 `bot_sessions.state_metadata`（JSONB），大照片會讓該欄位膨脹到數 MB，且每則訊息查狀態都要撈出來。另「照片動起來」的縮圖、以及成品保留 30 天都需要它（未設定時成品只剩模型端約一小時的暫存網址，用戶隔天回頭就是破圖）|
| `SUPABASE_SERVICE_ROLE_KEY` | | **service role / secret** key（非 anon / publishable key） |
| `SUPABASE_STORAGE_BUCKET` | | 預設 `linebot-temp-images` |
| `WELCOME_POINTS` | | **覆寫** `config/settings.yml` 的新會員贈點 |
| `COLORIZE_COST` / `EDIT_COST` | | **覆寫** `config/settings.yml` 的點數（見下方） |
| `COLORIZE_MODEL` / `EDIT_MODEL` | | **覆寫** `config/settings.yml` 的模型 ID |
| `ECPAY_MERCHANT_ID` | | 綠界商店代號。以下四個 `ECPAY_*` **要嘛全設、要嘛全不設**；缺任一個都視為未設定，儲值自動停用，服務其餘部分照常 |
| `ECPAY_HASH_KEY` | | 綠界 HashKey（**密鑰**，只放環境變數，絕不進 `settings.yml`——那個檔案在 git 裡） |
| `ECPAY_HASH_IV` | | 綠界 HashIV（**密鑰**，同上） |
| `ECPAY_API_URL` | | 綠界 AioCheckOut 端點；測試站與正式站不同，以綠界官方文件為準 |
| `LIFF_ID` | | 付款頁的 LIFF ID。沒設的話 `/pay` 回 503，且 bot 不會提「儲值」 |
| `LINE_LOGIN_CHANNEL_ID` | | 驗證付款頁 ID token 用的 LINE Login channel ID。沒設的話 `/pay/checkout` 一律 401 |
| `SENTRY_DSN` | | 留空則不啟用 Sentry |
| `SENTRY_ENVIRONMENT` | | `production` / `staging` |
| `IMAGE_WORKERS` / `IMAGE_QUEUE_LIMIT` | | 圖片處理併發（預設 4 / 8） |

> `.env` 不可 commit（已在 `.gitignore`）；各環境變數在 Railway 各自設定。

### 營運設定：`config/settings.yml`

用哪個模型、扣幾點、載入動畫幾秒、**該模型的輸入欄位名稱**，以及新會員贈幾點，都在 `config/settings.yml`。改完 push 即生效，不必改程式碼。

換模型時光改 `model` 是不夠的——不同模型的欄位名稱不一樣（`nano-banana` 的圖片欄位叫 `image_input` 且吃陣列，`restore-image` 叫 `input_image` 吃單值），所以 `input` 區段要一起調。檔案內的註解附了幾個常用模型的對應可直接抄。

**推之前先驗**：

```bash
python3 -m src.core.settings
```

印出每個功能實際生效的模型、點數與欄位對應；設定有誤會列出哪個欄位錯、該怎麼改，並以非 0 結束。

同一道檢查掛在 Railway 的 `preDeployCommand`（見 `railway.json`）：**設定有誤會中止部署**，不會帶著壞設定上線。這道防線是必要的——應用程式本身會吞掉啟動錯誤並在每次請求重試，少了它，壞設定會讓服務看起來活著、但每個 webhook 都回 500。

> 上表的 `*_COST` / `*_MODEL` 環境變數**優先於設定檔**，用於線上不部署就調價。反過來說，只要 Railway 上還留著 `EDIT_COST`，改 `config/settings.yml` 的點數就不會有效果——調完記得把變數移除。`python3 -m src.core.settings` 印的是套用覆寫後的實際值，可用來確認。

---

## 五、從零建立：standalone 上線（新 Supabase 專案）

完全獨立於 Altide 的建置流程。全部做完約 20 分鐘。

### 5.1 建立 Supabase 專案

[Supabase](https://supabase.com/) → **New Project**，Free 方案即可。

**Region — 別照畫面上的建議選**

建立頁面寫「Select the region closest to your users」，**這句話對本架構是誤導**。長輩的手機連的是 LINE 與 Railway，**不會直接連 Supabase**；真正高頻往返的是 Railway ↔ Supabase（每則訊息查 2～3 次資料庫：對話狀態、會員、扣點）。

所以要選的是**離 Railway 最近**的區域：

| Railway 專案的 region | Supabase 選 |
|---|---|
| `asia-southeast1`（新加坡） | Southeast Asia (Singapore) |
| `us-west1` / `us-east4`（**Railway 預設**） | 對應的 US 區，或把 Railway 一併搬到新加坡 |

> Railway 的 region 在 Service → Settings → Deploy → Region。選錯的代價是每則訊息多繞幾趟太平洋，體感明顯。

**Security 三個勾選項**

| 選項 | 設定 | 為什麼 |
|---|---|---|
| Enable Data API | **關** | 本專案只用「直連 Postgres」與「Storage API（`/storage/v1/`）」，完全沒用到 Data API（`/rest/v1/`）。關掉少一個對外入口 |
| Automatically expose new tables | **關** | Supabase 官方也建議關 |
| Enable automatic RLS | **開** | 對本專案零影響（只作用於 `public` schema，我們的表在 `grandpa_yin`；後端的 postgres 角色與 service role 本來就繞過 RLS）。萬一日後 Data API 被打開，表也不會裸奔 |

> 若這個 Supabase 專案日後還要給網頁前端用 `supabase-js` 存取，那 Data API 才需要開。純跑這支 bot 不需要。

建立時設定的 **Database Password** 請保存，等一下組連線字串要用。

### 5.2 取得 `DATABASE_URL` — 用 Session pooler

**Connect → Session pooler** 那條字串，把 `[YOUR-PASSWORD]` 換成密碼：

```
postgresql://postgres.[PROJECT-REF]:[YOUR-PASSWORD]@aws-0-[REGION].pooler.supabase.com:5432/postgres
```

三種連線方式的差別：

| 方式 | 適不適用本專案 |
|---|---|
| **Session pooler**（pooler 主機 :5432） | ✅ **用這個**。支援 IPv4，且相容 SQLAlchemy 的連線池與 prepared statement |
| Direct connection（`db.xxx.supabase.co:5432`） | ⚠️ 新專案**只支援 IPv6**；且本服務是長駐 gunicorn（2 workers × pool 5 + overflow 10，再加圖片背景執行緒），直連容易撞上限 |
| Transaction pooler（:6543） | ❌ 給 serverless 用；不支援 prepared statement，與 SQLAlchemy 預設行為衝突 |

> 這一步順帶解掉[系統健檢](./HEALTH_CHECK.md)裡「連線池上限」那項待辦——新環境直接用對的連線方式，不必事後補。

### 5.3 建立 Storage bucket

**Storage → New bucket**：

| 設定 | 值 |
|---|---|
| 名稱 | `linebot-temp-images`（要與 `SUPABASE_STORAGE_BUCKET` 一致） |
| Public bucket | **關**（維持 Private） |

Private 就夠：圖片只由後端以 service role 讀寫，**結果圖的 URL 來自 Replicate 而非 Supabase**，不需要公開存取。

### 5.4 取得 Storage 用的 key

**Settings → API Keys**，取 **service_role**（新專案顯示為 **secret**，`sb_secret_...`）那一把，填入 `SUPABASE_SERVICE_ROLE_KEY`。

> ⚠️ 別拿成 anon / publishable 那把——它受 RLS 限制，Storage 讀寫會失敗。service role key **等同資料庫的完整權限**，只能放在後端環境變數，絕不可進前端或 commit。

### 5.5 設定 Railway 環境變數

除了第四章的必填項，standalone 額外要設：

```
DEPLOY_MODE=standalone
SUPABASE_URL=https://[PROJECT-REF].supabase.co
SUPABASE_SERVICE_ROLE_KEY=<5.4 取得的 key>
SUPABASE_STORAGE_BUCKET=linebot-temp-images
```

> `DEPLOY_MODE` **未設定時預設是 `platform`**，會去找 Altide 的 `public.accounts`——新專案沒有那些表，會直接失敗。這是最容易漏的一項。
>
> 模型、點數、贈點走 `config/settings.yml`，**不要**在 Railway 設 `EDIT_COST` / `COLORIZE_COST` / `WELCOME_POINTS`，否則之後改設定檔會看不出效果。

### 5.6 建表 —— 自動完成，不必手動

**第一次部署就會自動建表**，你不需要做任何事。Railway 的 `preDeployCommand`（見 `railway.json`）每次部署都會跑：

```
python -m src.core.settings && alembic upgrade head
```

空的 Supabase 專案會被建出：

```
grandpa_yin | subjects             帳號（standalone 身份）
grandpa_yin | wallet_transactions  點數流水
grandpa_yin | bot_sessions         對話狀態
grandpa_yin | usage_logs           功能使用紀錄
grandpa_yin | user_profiles        暱稱、狀態
grandpa_yin | alembic_version      版本記錄
```

連 `grandpa_yin` schema 本身也會自動建（`alembic/env.py` 與 baseline migration 各有一道 `CREATE SCHEMA IF NOT EXISTS`），Supabase 那邊不必先準備任何東西。standalone 下整合 migration 會偵測到沒有 `public.accounts` 而跳過補外鍵，建出來的表**無任何 Altide 依賴**。

**唯一的順序要求**：`DATABASE_URL` 要在該次部署**開始之前**就設好。先 push 後補變數的話，第一次部署會在 preDeploy 階段失敗——好消息是它會**中止部署**，不會帶著沒有表的狀態上線；補上變數後 Redeploy 即可。

> 想在部署前先確認連線字串是對的，可以本地手動跑一次（非必要，只是失敗訊息看得比較快）：
> ```bash
> DATABASE_URL=<5.2 的連線字串> alembic upgrade head
> ```
> 這是冪等的，之後部署再跑一次也不會有事。

> ⚠️ **不要**在空資料庫上跑 `alembic stamp head`。stamp 是「表已經存在、只缺版本記錄」時用的（見 6.1）；在空庫上 stamp 會讓 Alembic 以為表都建好了，結果一張都沒有，服務起來後每個請求都炸。

之後每次部署，Railway 的 `preDeployCommand` 會自動 `alembic upgrade head`，這一步只有第一次要手動做。

### 5.7 驗收清單

```bash
python3 -m src.core.settings          # 設定檔正常？印出實際生效的模型與點數
```

部署後在 LINE 依序試：

| 動作 | 預期 |
|---|---|
| 加好友 | 收到歡迎訊息，含贈送的點數 |
| 輸入「點數」 | 顯示餘額（＝`config/settings.yml` 的 `welcome_points`） |
| **直接傳一張照片** | 跳出「上色／修改／取消」選單 → 代表 Storage 通了 |
| 選「幫照片上色」 | 扣點 → 收到成品圖 |
| 再輸入「歷史」 | 看得到剛才那筆扣點紀錄 |
| 輸入「儲值」 | 有開通金流才會給付款連結；沒開通會明講「還沒開放」 |
| 走完一次付款 | 幾秒內點數入帳，「歷史」看得到「儲值 N 點（訂單 …）」 |

任一步卡住，對照第七章的事故速查。傳照片沒反應 → 多半是 Storage bucket 名稱或 key 錯（此時服務會退回 base64 模式並在 log 留 warning）。

---

## 六、資料庫 schema 與自動 migration

schema 分兩層、各自管理：

- **共用層 `public.*`**（accounts / transactions / linked_identities）由 Altide 的 `altide-landing-page/supabase/schema.sql` 管理（含 `auth.*` / `storage.*` 依賴，僅適用於 Supabase）。本專案**不碰**。
- **產品層 `grandpa_yin.*`**（bot_sessions / usage_logs / user_profiles / subjects / wallet_transactions）由本專案的 **Alembic** 管理，migration 檔在 `alembic/versions/`（命名慣例與常用指令見 [`alembic/README.md`](../alembic/README.md)）。

每次部署，Railway 的 `preDeployCommand`（見 `railway.json`）自動執行 `alembic upgrade head`，把 `grandpa_yin.*` 更新到最新；失敗則中止部署（不會帶著壞 schema 上線）。

**改動流程**：改 `src/models/*.py` → `alembic revision --autogenerate -m "描述"` → **檢視產生的 migration** → commit → push（model 與 migration 檔要一起 push）。

### 6.1 首次導入（**線上 DB 已有** grandpa_yin.* 表時，只做一次）

表若早已由 `schema.sql` 建好、但還沒 Alembic 版本記錄，第一次啟用前要先標記 baseline，否則 `upgrade` 會嘗試重建已存在的表而失敗：

```bash
DATABASE_URL=<線上連線字串> alembic stamp head
```

> ⚠️ 這一步**只適用於表已經存在**的舊環境。全新的空資料庫請直接 `alembic upgrade head`（見 5.6）——在空庫上 stamp 會標記成「已是最新」卻一張表都沒建。

### 6.2 全新資料庫的初始化順序（外鍵依賴）

自 Phase 5 起，baseline migration **不再帶跨 schema 外鍵**，改由整合 migration
`a7b8c9d0e1f2` 冪等補上（只在 `public.accounts` 存在時）。所以：

- **standalone**：`alembic upgrade head` 直接建起 `grandpa_yin.*`，無任何 Altide 依賴。
- **platform（全新環境）**：仍建議**先套用共用層再跑 Alembic**，順序如下：
  ```
  1. 先套用 Altide 共用層 → altide-landing-page/supabase/schema.sql   （建 public.accounts…）
  2. 再跑本專案 Alembic     → alembic upgrade head                    （建 grandpa_yin.* 並補 FK）
  ```

> 與過去不同：**順序反了不會再讓 `upgrade` 失敗**（baseline 無 FK，表照樣建起）；只是整合 migration 當下因 `public.accounts` 不存在而**跳過補 FK**，且該 migration 一旦標記完成就不會自己重跑。若真的先跑了 Alembic、之後才有 accounts，補 FK 的方式是手動 `alembic downgrade f1a2b3c4d5e6 && alembic upgrade head`，或直接手動 `ALTER TABLE … ADD FOREIGN KEY`。因此 platform 全新環境**仍以「先 schema.sql 再 Alembic」為準**。

---

## 七、營運與事故處理（Incident Playbook）

出事時從「通用 triage」開始，五分鐘內定位分級，再跳對應情境。

### 事故分級

| 級別 | 定義 | 例子 | 回應目標 |
|---|---|---|---|
| **P0** | 全體無法使用，或金流失血 | webhook 全掛、重複扣點、免費放送 Replicate | 立即 |
| **P1** | 主要功能失效但有降級 | Replicate 全失敗（有退點）、DB 掛（有維護訊息） | 1 小時內 |
| **P2** | 部分用戶或非核心異常 | 單一用戶點數對不上、歡迎訊息沒發 | 1 個工作天 |

### 通用 triage（前五分鐘）

1. **看 Sentry**：有無新 error 爆量？事件帶 `request_id` / `user_id`。
2. **看 Railway logs**（`railway logs`）：搜 `ERROR` / `exception` / `初始化失敗` / `扣點` / `退點`（log 都帶 8 碼 request_id）。
3. **確認部署狀態**：出事時間點與最近 deploy 吻合 → 先懷疑新版本，回滾（情境 12）。
4. **看依賴狀態頁**：[LINE](https://developers.line.biz/status/)、[Supabase](https://status.supabase.com/)、[Replicate](https://status.replicate.com/)、[Railway](https://status.railway.com/)。
5. **自己傳「!功能」實測**：有回 → webhook 通，問題在特定功能；沒回 → 情境 1。

> 處理過程隨手記在 `log/incidents/YYYY-MM-DD-簡述.md`（時間軸、根因、後續行動）。

### 常見情境速查

| # | 情境 | 級別 | 止血重點 |
|---|---|---|---|
| 1 | Bot 完全不回覆 | P0 | 查 Railway crash / LINE Verify / `Invalid signature`(→CHANNEL_SECRET) / Use webhook 開關 |
| 2 | 回覆很慢、部分沒回 | P1 | 搜 `WORKER TIMEOUT` / `目前使用人數較多`（設計內降級）；尖峰調高 `IMAGE_WORKERS` |
| 3 | 重複處理 / 重複扣點 | P0 | webhook 去重表在**記憶體**，`-w 2` 跨 process 有缺口；退點要**雙寫** transaction |
| 4 | 扣點但沒收到圖 | P1 | 查 `usage_logs`：`failed`=已自動退點；只有 `completed`=push 失敗，手動退點 |
| 5 | DB 連不上 | P1 | Supabase 專案被 **Paused**（免費 7 天）→ Restore；恢復後**要 Restart Railway** |
| 6 | 連線池耗盡 | P1 | `pg_stat_activity` 查連線；`pg_terminate_backend` 清卡死；根治用 pooler(6543) |
| 7 | 點數帳目對不上 | P2 | 以 `transactions` 流水為準，補 adjustment transaction |
| 8 | Storage 故障 | P2 | 清空 `SUPABASE_URL` 退回 base64 模式；查 bucket 名稱 / service role key |
| 9~11 | Replicate 額度/變慢/模型下架 | P1~P2 | 儲值 / 觀察（有界佇列 + 失敗退點自保）/ 改 model input |
| 12 | 新版本出問題 | P0~P1 | **先回滾再研究**：Railway → Deployments → 上一版 Redeploy |
| 13 | 環境變數錯誤/洩漏 | P0 | 對照清單清點；金鑰洩漏立即輪替全部 key |
| 14 | LINE rate limit / 額度用完 | P1 | 查 `/v2/bot/message/quota`；push 失敗期間扣了點的用戶事後補償 |
| 15 | OOM | P1 | 調低 `IMAGE_QUEUE_LIMIT` 或加 RAM；善後撈重啟窗內未收圖的退點 |
| 16 | 付了錢但點數沒進來 | **P0** | 查 `payment_orders`：`status='paid'` 但 `credited_at IS NULL` 就是漏發。先看 `raw_callback` 確認綠界確實回報成功，再用 `scripts/add_points.py` 補點並記錄訂單編號 |
| 17 | 同一筆重複入帳 | P0 | 理論上被 `merchant_trade_no` 唯一鍵擋住；真發生代表有繞過 `payment_service` 的寫入，比對 `transactions` 與 `payment_orders` 後扣回 |

### 常用除錯 SQL（platform 模式）

```sql
-- 某 LINE 用戶的最近交易
SELECT t.created_at, t.amount, t.balance_after, t.description
FROM public.transactions t
JOIN public.linked_identities li ON li.account_id = t.account_id
WHERE li.provider = 'line' AND li.provider_uid = 'U用戶ID'
ORDER BY t.created_at DESC LIMIT 20;

-- 帳目一致性檢查（餘額應等於最後一筆 balance_after）
SELECT a.id, a.points_balance, t.balance_after AS last_tx_balance
FROM public.accounts a
JOIN LATERAL (
  SELECT balance_after FROM public.transactions
  WHERE account_id = a.id ORDER BY created_at DESC LIMIT 1
) t ON true
WHERE a.points_balance <> t.balance_after;

-- 手動退點（務必雙寫，不要直接 UPDATE 餘額）
WITH acct AS (
  SELECT a.id FROM public.accounts a
  JOIN public.linked_identities li ON li.account_id = a.id
  WHERE li.provider = 'line' AND li.provider_uid = 'U用戶ID'
), upd AS (
  UPDATE public.accounts SET points_balance = points_balance + 10
  WHERE id = (SELECT id FROM acct) RETURNING id, points_balance
)
INSERT INTO public.transactions (account_id, amount, service, balance_after, description)
SELECT id, 10, 'silver-grandpa', points_balance, '補償（incident YYYY-MM-DD）' FROM upd;
```

### 常用除錯 SQL（standalone 模式）

同樣三題，換成自有的 `grandpa_yin.subjects` / `wallet_transactions`（**表名與欄位都不同**，事故當下別照抄上一節）：

```sql
-- 某 LINE 用戶的最近交易
SELECT wt.created_at, wt.amount, wt.balance_after, wt.description
FROM grandpa_yin.wallet_transactions wt
JOIN grandpa_yin.subjects s ON s.id = wt.subject_id
WHERE s.provider = 'line' AND s.provider_uid = 'U用戶ID'
ORDER BY wt.created_at DESC LIMIT 20;

-- 帳目一致性檢查（餘額應等於最後一筆 balance_after）
SELECT s.id, s.points_balance, wt.balance_after AS last_tx_balance
FROM grandpa_yin.subjects s
JOIN LATERAL (
  SELECT balance_after FROM grandpa_yin.wallet_transactions
  WHERE subject_id = s.id ORDER BY created_at DESC LIMIT 1
) wt ON true
WHERE s.points_balance <> wt.balance_after;

-- 手動退點（務必雙寫，不要直接 UPDATE 餘額）
WITH subj AS (
  SELECT id FROM grandpa_yin.subjects
  WHERE provider = 'line' AND provider_uid = 'U用戶ID'
), upd AS (
  UPDATE grandpa_yin.subjects SET points_balance = points_balance + 10
  WHERE id = (SELECT id FROM subj) RETURNING id, points_balance
)
INSERT INTO grandpa_yin.wallet_transactions (subject_id, amount, service, balance_after, description)
SELECT id, 10, 'silver-grandpa', points_balance, '補償（incident YYYY-MM-DD）' FROM upd;
```

> 兩種模式共用 `grandpa_yin.usage_logs`（查功能層級的成功／失敗），其 `account_id` 在 standalone 存的是 `subjects.id`、在 platform 存的是 `accounts.id`。

### 待補強清單（來自事故演練）

1. **`/health` endpoint + 外部監控**（UptimeRobot）——目前掛掉只能靠用戶回報。
2. **跨 process 的 webhook 去重**——記憶體去重在 `-w 2` 下有缺口，改用 Postgres `processed_events` 表或 Redis。
3. **推送失敗的自動退點**——目前「處理成功但 push 失敗」會白扣點，靠人工補償。
4. ~~**Supabase connection pooler**~~——✅ 已納入建置步驟（見 5.2，用 Session pooler）。既有環境若仍是直連，改連線字串即可。
5. **`transactions` 加 `source/created_by`**——帳目稽核分辨寫入來源。
6. ~~**Storage bucket 生命週期清理**~~——✅ 已完成（2026-08-10），見下方「排程維運」。
7. **`run_replicate` 加 timeout**——`replicate.run()` 無 client-side timeout，模型吊住會佔住 worker。（model ID 改設定檔的部分已完成，見 `config/settings.yml`。）

### 排程維運（Railway cron）

Supabase Storage **沒有** S3 那種 lifecycle policy 可以在 dashboard 設定，暫存圖的清理要自己排程。

bucket 裡有兩種東西，壽命差一個數量級：

| | 位置 | 保留 | 誰在清 |
|---|---|---|---|
| 暫存圖（用戶上傳的原圖） | `<功能名>/…` | 一次流程 | 即時 + 24 小時兜底 |
| 成品（推給用戶的圖／影片） | `results/…` | **30 天** | 31 天後由同一支腳本回收 |

暫存圖的清理分兩層：
- **即時**——狀態轉換時自動刪掉被取代的暫存圖（`BaseFeature._discard_superseded_image`），涵蓋走完流程、取消、換圖、中途切功能。
- **兜底**——`scripts/cleanup_storage.py` 掃 bucket，刪「超過 24 小時 **且** 沒有任何存活 `bot_session` 引用」的物件。負責 crash、重新部署打斷、用戶棄坑這類即時清理接不到的情況。

成品則單純看時間：`results/` 底下超過 `--result-days`（預設 31）就刪。它們**不會**被任何 `bot_session` 引用，所以腳本刻意把這一區跟暫存圖分開判定——誤用 24 小時那條規則的話，用戶隔天回頭看到的就是滿滿的破圖。預設比 signed URL 的 30 天多留一天，避免在到期邊界上跟用戶搶。

> 容量抓法：一張上色成品約 2 MB、一支 5 秒影片約 5 MB。100 位用戶每人每月 10 次 ≈ 2 GB/月，滾動保留 30 天的話穩定在 2 GB 上下，不會無限成長。

在 Railway 加一個 cron service（與 web service 同一個 repo）：

| 設定 | 值 |
|---|---|
| Start Command | `python scripts/cleanup_storage.py --apply` |
| Cron Schedule | `0 18 * * *`（UTC，約台灣時間凌晨 2 點） |
| 環境變數 | 與 web service 相同（至少要 `DATABASE_URL`、`SUPABASE_URL`、`SUPABASE_SERVICE_ROLE_KEY`） |

> ⚠️ 別把 cron 設在主 web service 上——cron service 執行完必須結束，web service 要常駐。
>
> 首次上線先不加 `--apply` 手動跑一次，確認盤點結果合理再排程。腳本的判定條件同時檢查「夠舊」和「沒被引用」，所以與 `cleanup_user_states.py` 的執行順序無關，也不會誤刪正在流程中的用戶照片。

對話狀態的清理（`scripts/cleanup_user_states.py 24`）可以掛在同一個 cron service，排在 storage 清理之前或之後都可以。
