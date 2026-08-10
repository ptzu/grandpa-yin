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

線上目前跑 **platform** 模式，與 Altide 共用帳號層。

---

## 三、Railway 部署步驟

1. 在 [Railway](https://railway.app/) 建專案並連結此 repo。
2. **設定環境變數**（Variables，見第四章清單）。
3. Push 到 `main` 即自動部署（啟動指令見 `Procfile`）。
4. 將 Railway 網域設為 LINE Webhook：`https://<your-app>.up.railway.app/webhook`，並在 LINE Console 開啟 **Use webhook**。

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
| `DEPLOY_MODE` | | `platform`（預設）/ `standalone` |
| `SUPABASE_URL` | | Supabase 專案 URL（圖片 Storage） |
| `SUPABASE_SERVICE_ROLE_KEY` | | **service role** key（非 anon key） |
| `SUPABASE_STORAGE_BUCKET` | | 預設 `linebot-temp-images` |
| `WELCOME_POINTS` | | 新會員註冊獎勵點數（預設 50） |
| `COLORIZE_COST` / `EDIT_COST` | | **覆寫** `config/models.yml` 的點數（見下方） |
| `COLORIZE_MODEL` / `EDIT_MODEL` | | **覆寫** `config/models.yml` 的模型 ID |
| `SENTRY_DSN` | | 留空則不啟用 Sentry |
| `SENTRY_ENVIRONMENT` | | `production` / `staging` |
| `IMAGE_WORKERS` / `IMAGE_QUEUE_LIMIT` | | 圖片處理併發（預設 4 / 8） |

> `.env` 不可 commit（已在 `.gitignore`）；各環境變數在 Railway 各自設定。

### AI 模型與點數：`config/models.yml`

圖片編輯／彩色化用哪個模型、扣幾點、載入動畫幾秒，以及**該模型的輸入欄位名稱**，都在 `config/models.yml`。改完 push 即生效，不必改程式碼。

換模型時光改 `model` 是不夠的——不同模型的欄位名稱不一樣（`nano-banana` 的圖片欄位叫 `image_input` 且吃陣列，`restore-image` 叫 `input_image` 吃單值），所以 `input` 區段要一起調。檔案內的註解附了幾個常用模型的對應可直接抄。

**推之前先驗**：

```bash
python3 -m src.core.model_config
```

印出每個功能實際生效的模型、點數與欄位對應；設定有誤會列出哪個欄位錯、該怎麼改，並以非 0 結束。

同一道檢查掛在 Railway 的 `preDeployCommand`（見 `railway.json`）：**設定有誤會中止部署**，不會帶著壞設定上線。這道防線是必要的——應用程式本身會吞掉啟動錯誤並在每次請求重試，少了它，壞設定會讓服務看起來活著、但每個 webhook 都回 500。

> 上表的 `*_COST` / `*_MODEL` 環境變數**優先於設定檔**，用於線上不部署就調價。反過來說，只要 Railway 上還留著 `EDIT_COST`，改 `config/models.yml` 的點數就不會有效果——調完記得把變數移除。`python3 -m src.core.model_config` 印的是套用覆寫後的實際值，可用來確認。

---

## 五、取得 Supabase `DATABASE_URL`

1. [Supabase](https://supabase.com/) → **New Project**（Region 選 Tokyo / Singapore，Free 方案即可）。
2. 建立時設定的 **Database Password** 請保存。
3. **Settings → Database → Connection string → URI**，複製如下格式，把 `[YOUR-PASSWORD]` 換成密碼：
   ```
   postgresql://postgres:[YOUR-PASSWORD]@db.[PROJECT-ID].supabase.co:5432/postgres
   ```
4. 高併發時改用 **connection pooler**（port **6543** 的 transaction pooler）連線字串，避免吃到 DB 直連上限。

---

## 六、資料庫 schema 與自動 migration

schema 分兩層、各自管理：

- **共用層 `public.*`**（accounts / transactions / linked_identities）由 Altide 的 `altide-landing-page/supabase/schema.sql` 管理（含 `auth.*` / `storage.*` 依賴，僅適用於 Supabase）。本專案**不碰**。
- **產品層 `grandpa_yin.*`**（bot_sessions / usage_logs / user_profiles / subjects / wallet_transactions）由本專案的 **Alembic** 管理，migration 檔在 `alembic/versions/`（命名慣例與常用指令見 [`alembic/README.md`](../alembic/README.md)）。

每次部署，Railway 的 `preDeployCommand`（見 `railway.json`）自動執行 `alembic upgrade head`，把 `grandpa_yin.*` 更新到最新；失敗則中止部署（不會帶著壞 schema 上線）。

**改動流程**：改 `src/models/*.py` → `alembic revision --autogenerate -m "描述"` → **檢視產生的 migration** → commit → push（model 與 migration 檔要一起 push）。

### 首次導入（線上 DB 已有 grandpa_yin.* 表時，只做一次）

表若早已由 `schema.sql` 建好、但還沒 Alembic 版本記錄，第一次啟用前要先標記 baseline，否則 `upgrade` 會嘗試重建已存在的表而失敗：

```bash
DATABASE_URL=<線上連線字串> alembic stamp head
```

### 全新資料庫的初始化順序（外鍵依賴）

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

### 待補強清單（來自事故演練）

1. **`/health` endpoint + 外部監控**（UptimeRobot）——目前掛掉只能靠用戶回報。
2. **跨 process 的 webhook 去重**——記憶體去重在 `-w 2` 下有缺口，改用 Postgres `processed_events` 表或 Redis。
3. **推送失敗的自動退點**——目前「處理成功但 push 失敗」會白扣點，靠人工補償。
4. **Supabase connection pooler（6543）**——根治連線池上限。
5. **`transactions` 加 `source/created_by`**——帳目稽核分辨寫入來源。
6. ~~**Storage bucket 生命週期清理**~~——✅ 已完成（2026-08-10），見下方「排程維運」。
7. **Replicate model ID 改環境變數 + timeout**——換模型免部署、避免 worker 吊死。

### 排程維運（Railway cron）

Supabase Storage **沒有** S3 那種 lifecycle policy 可以在 dashboard 設定，暫存圖的清理要自己排程。

清理分兩層：
- **即時**——狀態轉換時自動刪掉被取代的暫存圖（`BaseFeature._discard_superseded_image`），涵蓋走完流程、取消、換圖、中途切功能。
- **兜底**——`scripts/cleanup_storage.py` 掃 bucket，刪「超過 24 小時 **且** 沒有任何存活 `bot_session` 引用」的物件。負責 crash、重新部署打斷、用戶棄坑這類即時清理接不到的情況。

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
