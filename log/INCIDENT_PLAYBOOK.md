# 銀爺爺 LINE Bot 作戰模擬手冊（Incident Playbook）

> 建立日期：2026-07-08
> 適用架構：Railway（Flask + gunicorn `-w 2 --threads 8`）＋ Supabase（PostgreSQL + Storage）＋ LINE Messaging API ＋ Replicate ＋ Sentry
> 使用方式：出事時從「第一章 通用 SOP」開始，五分鐘內定位是哪一類事故，再跳到對應情境的處理步驟。平時依「第五章」定期演練。

---

## 〇、系統地圖與爆炸半徑

```
用戶 LINE App
   │  webhook
   ▼
LINE Platform ──► Railway (Flask /webhook)
                     │
                     ├─► Supabase PostgreSQL（會員、點數、狀態、usage_logs）
                     ├─► Supabase Storage（圖片編輯的暫存圖）
                     ├─► Replicate API（彩色化 / 圖片編輯，背景執行緒池 4 workers + 8 queue）
                     ├─► LINE Messaging API（reply / push / loading animation）
                     └─► Sentry（錯誤追蹤）
```

各依賴掛掉時的影響範圍：

| 依賴 | 掛掉時的影響 | 系統目前的自我保護 |
|---|---|---|
| LINE Platform | 全服務中斷（訊息進不來也出不去） | 無（只能等 LINE 恢復） |
| Railway | 全服務中斷 | 無 |
| Supabase PostgreSQL | 會員/點數/狀態全失效 | 功能回覆「⚠️ 系統維護中」拒絕服務（不會免費放送） |
| Supabase Storage | 圖片編輯兩步驟流程失效 | 未設定時退回 base64 存 state；已設定但故障時用戶會收到錯誤訊息 |
| Replicate | 彩色化/編輯失效 | 先扣點→失敗退點；執行緒池滿載回「使用人數較多」 |
| Sentry | 只影響觀測，不影響服務 | DSN 未設定時自動跳過 |

關鍵資料表（除錯 SQL 會用到）：
`public.accounts`（點數餘額）、`public.transactions`（交易流水）、`public.linked_identities`（LINE UID → account 對應）、`grandpa_yin.user_profiles`、`grandpa_yin.usage_logs`（功能使用/失敗記錄）、`grandpa_yin.bot_sessions`（對話狀態）。

---

## 一、事故分級與通用 SOP（出事先做這個）

### 分級

| 級別 | 定義 | 例子 | 回應目標 |
|---|---|---|---|
| **P0** | 全體用戶無法使用，或金流正在失血 | webhook 全掛、重複扣點、免費放送 Replicate 額度 | 立即處理 |
| **P1** | 主要功能失效但有降級 | Replicate 全部失敗（有退點）、DB 掛（有維護訊息） | 1 小時內 |
| **P2** | 部分用戶或非核心功能異常 | 單一用戶點數對不上、歡迎訊息沒發 | 1 個工作天內 |

### 通用 triage（前五分鐘）

1. **看 Sentry**：有沒有新的 error 事件？同一種錯誤爆量通常直接指出兇手（事件上有 `request_id` 與 `user_id`）。
2. **看 Railway logs**：Dashboard → 服務 → Logs，或 CLI：
   ```bash
   railway logs
   ```
   搜尋 `ERROR`、`exception`、`初始化失敗`、`扣點`、`退點` 等關鍵字（log 都帶 8 碼 request_id，可以串起同一個請求的完整路徑）。
3. **確認部署狀態**：Railway 最近有沒有新 deploy？出事時間點跟 deploy 時間吻合 → 先懷疑新版本（見情境 12 的回滾）。
4. **看依賴狀態頁**：
   - LINE：https://developers.line.biz/status/
   - Supabase：https://status.supabase.com/
   - Replicate：https://status.replicate.com/
   - Railway：https://status.railway.com/
5. **自己發一則訊息給 Bot 實測**：傳「!功能」看有沒有回。有回 → webhook 路徑是通的，問題在特定功能；沒回 → 進情境 1。

### 事故記錄

處理過程隨手記在 `log/incidents/YYYY-MM-DD-簡述.md`（時間軸、做了什麼、根因、後續行動），事後補防禦。

---

## 二、情境演練：訊息通道（LINE ↔ Railway）

### 情境 1：Bot 完全不回覆任何訊息（P0）

**症狀**：所有用戶傳什麼都沒反應。

**診斷步驟（按順序，找到兇手就停）**：
1. Railway Dashboard：服務是不是 crash / 重啟循環？看 Deploy logs 有沒有啟動錯誤（例如 `CHANNEL_ACCESS_TOKEN 環境變數未設定` → 環境變數被動到，跳情境 13）。
2. LINE Developers Console → Messaging API → Webhook URL → 按 **Verify**：
   - Verify 失敗 → Railway 端問題（服務掛了或 URL 變了）。
   - Verify 成功但訊息沒進來 → 檢查 Console 裡 **Use webhook** 是否被關掉。
3. Railway logs 搜 `Invalid signature`：大量出現 → `CHANNEL_SECRET` 不對（被改過或 channel 換了），跳情境 13。
4. logs 有進 webhook 但沒有回覆 → 看有沒有 exception stack trace，依內容跳對應情境（DB → 情境 5，Replicate → 情境 8）。
5. 以上都正常 → 看 LINE status page，可能是 LINE 平台事故，只能等待並觀察。

**止血**：
- Railway 服務掛掉 → Dashboard 手動 **Restart**；新版本造成 → **Rollback**（情境 12）。
- LINE 平台事故 → 無法止血，恢復後 LINE 會 redelivery，注意觀察有無重複處理（情境 3）。

**預防**：加 `/health` endpoint + 外部監控（UptimeRobot 之類每分鐘打一次），掛掉 5 分鐘內收到通知，而不是等用戶回報。（目前**沒有** /health，見第六章待補強清單。）

---

### 情境 2：回覆很慢、部分訊息沒回（P1）

**症狀**：用戶說「有時候要等很久」「有時候沒回」。

**診斷**：
1. Railway logs 找 `WORKER TIMEOUT`（gunicorn 60 秒殺 worker）→ 有同步路徑卡住了。
2. 找 `回覆訊息失敗 (status=400)`：reply token 逾時（LINE reply token 約 1 分鐘有效）→ 表示處理超過一分鐘才回，同樣是「太慢」的症狀。
3. 對照時間點看是不是尖峰（很多人同時傳圖）：搜 `目前使用人數較多` — 出現代表執行緒池（4 執行中 + 8 排隊）滿了，這是**設計內的降級**，不是故障。
4. 檢查 DB 延遲：Supabase Dashboard → Database → 看 CPU / 連線數。

**止血**：
- 尖峰造成 → 短期把 Railway 環境變數 `IMAGE_WORKERS` 調到 6、`IMAGE_QUEUE_LIMIT` 調到 12（注意：每條 worker 會佔 DB 連線，調高前確認 pool 設定跟得上）。
- 單一請求卡死 → Restart 服務先恢復，再從 logs 的 request_id 追出卡住的呼叫。

**預防**：所有外部呼叫都已有 timeout（健檢問題 4 已修）；若再發生，用 request_id 找出沒設 timeout 的漏網之魚。

---

### 情境 3：同一則訊息被處理兩次 / 重複扣點（P0，金流）

**症狀**：用戶收到兩張結果圖、`transactions` 出現兩筆相同扣點。

**背景知識**：webhook 有用 `webhookEventId` 去重，但去重表在**記憶體**，而 gunicorn 開 `-w 2` 兩個 process——**同一個 event 重送時若打到另一個 process，去重擋不住**。這是已知缺口。

**診斷**：
1. 撈出重複扣點的證據：
   ```sql
   -- 10 分鐘內同帳號同描述扣點 ≥ 2 筆
   SELECT account_id, description, count(*), array_agg(created_at ORDER BY created_at)
   FROM public.transactions
   WHERE amount < 0 AND created_at > now() - interval '1 day'
   GROUP BY account_id, description,
            date_trunc('hour', created_at), floor(extract(minute FROM created_at) / 10)
   HAVING count(*) >= 2;
   ```
2. Railway logs 用該時間點找兩筆處理的 request_id：若 logs 顯示「跳過重複的 webhook event」沒出現、但同一 `webhookEventId` 出現兩次 → 確認是跨 process 的去重缺口。
3. 確認觸發原因：LINE 為什麼重送？通常是 webhook 回應太慢（連動情境 2）。

**止血（補償用戶）**：
```sql
-- 查該用戶的 LINE UID 對應帳號與最近交易
SELECT t.created_at, t.amount, t.balance_after, t.description
FROM public.transactions t
JOIN public.linked_identities li ON li.account_id = t.account_id
WHERE li.provider = 'line' AND li.provider_uid = 'U用戶ID'
ORDER BY t.created_at DESC LIMIT 20;
```
確認多扣後，用管理腳本或 SQL 退點（**退點也要寫一筆 transaction，不要直接 UPDATE 餘額**）：
```sql
WITH acct AS (
  SELECT a.id FROM public.accounts a
  JOIN public.linked_identities li ON li.account_id = a.id
  WHERE li.provider = 'line' AND li.provider_uid = 'U用戶ID'
), upd AS (
  UPDATE public.accounts SET points_balance = points_balance + 10  -- 依實際多扣點數
  WHERE id = (SELECT id FROM acct) RETURNING id, points_balance
)
INSERT INTO public.transactions (account_id, amount, service, balance_after, description)
SELECT id, 10, 'silver-grandpa', points_balance, '重複扣點補償（incident YYYY-MM-DD）' FROM upd;
```

**根治**：把去重狀態移到共用儲存——最省事是直接用現有的 Postgres 建一張 `processed_events(event_id PK, created_at)`，插入衝突即視為重複；或上 Redis。在此之前，備選方案是 `Procfile` 改 `-w 1 --threads 16`（單 process，去重就可靠，但吞吐略降）。

---

### 情境 4：用戶被扣點但沒收到結果圖（P1，客訴最大宗）

**症狀**：用戶回報「點數扣了，圖沒來」。

**診斷（照順序）**：
1. 先查該用戶的交易與使用記錄：
   ```sql
   SELECT ul.created_at, ul.feature_type, ul.points_deducted, ul.status, ul.log_metadata
   FROM grandpa_yin.usage_logs ul
   JOIN public.linked_identities li ON li.account_id = ul.account_id
   WHERE li.provider = 'line' AND li.provider_uid = 'U用戶ID'
   ORDER BY ul.created_at DESC LIMIT 10;
   ```
   - 有 `status='failed'` 的記錄 → 系統已自動退點（看 `log_metadata.refunded_points`），回覆用戶確認餘額即可。
   - 只有 `completed` → 處理成功但**推送失敗**，繼續往下。
2. Railway logs 用時間點搜 `推送訊息`：
   - `推送訊息 ... 重試 3 次後仍失敗` → LINE push 掛了或用戶封鎖 Bot。
   - `status=429` → 觸及 push quota / rate limit（跳情境 14）。
3. 查用戶是否封鎖了 Bot（push 對封鎖者會 4xx）。

**止血**：
- 確認「扣點成功 + 推送失敗 + 未退點」→ 手動退點（用情境 3 的 SQL 模板，描述寫清楚事由）。
- 若是大範圍 push 失敗 → 看 LINE status page。

**預防**：目前「推送失敗」不會自動退點（重試 3 次已是防線）。若這類客訴變多，把 `submit_billed_processing` 改成推送失敗也走退點流程。

---

## 三、情境演練：資料層（Supabase PostgreSQL / Storage）

### 情境 5：資料庫完全連不上（P1）

**症狀**：所有功能回「⚠️ 系統維護中」；logs 出現 `資料庫初始化失敗` 或 SQLAlchemy `OperationalError`。

**診斷**：
1. Supabase Dashboard 開得起來嗎？專案是不是 **Paused**（免費方案 7 天無活動自動暫停）→ 按 Restore。
2. Supabase status page 有沒有 incident。
3. 本機直連測試（用 Railway 上同一條連線字串）：
   ```bash
   psql "$DATABASE_URL" -c "select 1"
   ```
   - 連得上 → 問題在 Railway 到 Supabase 之間，或連線字串被改（情境 13）。
   - 連不上 → Supabase 端問題。
4. 密碼被改過？Supabase Dashboard → Settings → Database → 重設密碼後要同步更新 Railway 的 `DATABASE_URL`。

**止血**：
- 服務端已自動降級成拒絕服務（不會免費放送），可以不動作等 DB 恢復。
- DB 恢復後**要重啟 Railway 服務**：`member_service` 在初始化失敗後是 `None`，不會自己復活。Restart 後傳「點數」實測。

**預防**：免費方案的自動暫停是定時炸彈——上 Pro 方案，或設一個每天 ping DB 的排程（cron 查詢一次即可保持活動）。

---

### 情境 6：連線池耗盡（P1）

**症狀**：間歇性 `QueuePool limit ... connection timed out`；尖峰時段特別明顯。

**背景**：連線總量 = gunicorn 2 processes ×（pool_size + max_overflow）＋ 圖片執行緒每條佔一線。Supabase 免費/小型方案直連上限約 60 條。

**診斷**：
1. 現在用了幾條：
   ```sql
   SELECT count(*), state FROM pg_stat_activity
   WHERE datname = current_database() GROUP BY state;
   ```
2. 有沒有卡死的長交易：
   ```sql
   SELECT pid, now() - xact_start AS dur, state, left(query, 80)
   FROM pg_stat_activity
   WHERE xact_start IS NOT NULL ORDER BY dur DESC LIMIT 10;
   ```

**止血**：
- 有 idle in transaction 卡很久 → `SELECT pg_terminate_backend(pid);` 終止該連線。
- 全面壅塞 → 重啟 Railway 服務釋放所有連線。

**根治**：改用 Supabase connection pooler（連線字串換 port **6543** 的 transaction pooler），應用端連線數就不再直接吃 DB 上限。

---

### 情境 7：點數帳目對不上（P2，但要盡快查）

**症狀**：用戶主張點數不對；或例行檢查發現餘額 ≠ 交易流水。

**診斷**：
1. 一致性總檢查（每個帳號最後一筆 `balance_after` 應等於現在餘額）：
   ```sql
   SELECT a.id, a.points_balance, t.balance_after AS last_tx_balance
   FROM public.accounts a
   JOIN LATERAL (
     SELECT balance_after FROM public.transactions
     WHERE account_id = a.id ORDER BY created_at DESC LIMIT 1
   ) t ON true
   WHERE a.points_balance <> t.balance_after;
   ```
   有結果 → 有人繞過交易直接改餘額（admin 腳本？手動 UPDATE？），或程式有未寫流水的路徑。
2. 用情境 3 的查詢撈該用戶完整流水，逐筆對照 `usage_logs` 與 Railway logs（request_id）還原事件。

**止血**：以 `transactions` 流水為準修正 `accounts.points_balance`，並補一筆 adjustment transaction 說明事由。

**預防**：健檢問題 13 建議的 `transactions.created_by/source` 欄位（區分 linebot / admin_script / 手動 SQL）就是為這種時刻準備的，建議排入。所有手動調整一律走「UPDATE + INSERT transaction」雙寫（如情境 3 模板）。

---

### 情境 8：Supabase Storage 故障 / 設定錯誤（P2）

**症狀**：圖片編輯在「上傳圖片」或「輸入描述」步驟報錯；logs 出現 storage 相關 HTTPError。

**診斷**：
1. logs 搜 `Storage`：
   - `Supabase Storage 未設定` warning → 環境變數沒設，功能退回 base64 模式（能動，但 DB 會胖，盡快補設定）。
   - 上傳 4xx：`404` → bucket 不存在或名稱打錯（對照 `SUPABASE_STORAGE_BUCKET`，預設 `linebot-temp-images`）；`403` → key 錯誤或用到 anon key（必須是 **service role key**）。
2. 手動驗證（用與 Railway 相同的值）：
   ```bash
   curl -X POST "$SUPABASE_URL/storage/v1/object/linebot-temp-images/drill/test.jpg" \
     -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY" \
     -H "Content-Type: image/jpeg" --data-binary "@test.jpg"
   ```

**止血**：Storage 掛掉期間，圖片編輯第一步就會失敗、用戶收到錯誤訊息且**不會被扣點**（扣點在描述輸入之後）——可暫時不動作；若要維持功能，暫時清空 `SUPABASE_URL` 讓它退回 base64 模式（重啟生效）。

**善後**：檢查 bucket 有沒有堆積孤兒物件（用戶上傳圖後中途放棄流程留下的）：Dashboard → Storage → 依日期排序刪舊檔。**預防**：對 bucket 設生命週期清理（或排程刪除超過 1 天的物件）。

---

## 四、情境演練：AI 處理與金流（Replicate）

### 情境 9：Replicate 額度用完（P1）

**症狀**：所有圖片處理失敗；logs 出現 `Replicate 點數不足，請前往 https://replicate.com/account/billing 儲值`；用戶被扣點後又自動退點（`usage_logs` 大量 `failed`）。

**處理**：
1. https://replicate.com/account/billing 儲值。
2. 驗證恢復：自己走一次彩色化流程。
3. 撈受影響用戶確認都有退點：
   ```sql
   SELECT ul.created_at, li.provider_uid, ul.feature_type, ul.log_metadata
   FROM grandpa_yin.usage_logs ul
   JOIN public.linked_identities li ON li.account_id = ul.account_id
   WHERE ul.status = 'failed' AND ul.created_at > now() - interval '1 day'
   ORDER BY ul.created_at;
   ```

**預防**：Replicate 後台設 billing alert / auto-recharge；每月看一次用量趨勢。

### 情境 10：Replicate 變慢或間歇失敗（P1）

**症狀**：處理要等很久；執行緒池滿載回「目前使用人數較多」；部分成功部分退點。

**診斷**：Replicate status page → 該模型頁面（`flux-kontext-apps/restore-image`、`google/nano-banana`）看是否有 degraded 公告；logs 統計 `處理失敗，退還點數` 的頻率。

**止血**：設計上已自我保護（有界佇列 + 失敗退點），主要工作是**觀察**。若失敗率 100% 且持續，考慮暫時對外公告（LINE 官方帳號群發或圖文選單掛維護公告）。

**注意**：`replicate.run()` 本身沒有 client-side timeout，最壞情況是 4 條 worker 都吊著等 Replicate——佇列滿後新請求會被擋在門外，不會拖垮整台，但已排入的用戶會等很久。若成常態，給 `run_replicate` 加 timeout 參數（Replicate SDK 支援 `client.run(..., wait=...)` 或用 prediction API 輪詢）。

### 情境 11：模型被下架或改版（P2）

**症狀**：某功能突然全部失敗，錯誤是 404 / 422 / 輸出解析失敗（`Replicate API 沒有回傳結果`）。

**處理**：
1. 到 Replicate 模型頁確認狀態與 schema 變更。
2. 模型還在但輸出格式變了 → 修 `_extract_output_url`；模型下架 → 換替代模型，改 `replicate_model` 與 input dict。
3. 上線前用 Replicate 網頁 playground 先驗證新模型的輸入輸出。

**預防**：把 model ID 改成環境變數，換模型不用改 code 重新 deploy（目前寫死在 class 屬性）。

---

## 五、情境演練：部署與設定（Railway）

### 情境 12：新版本部署後出問題 → 回滾（P0～P1）

**症狀**：deploy 之後 Sentry 冒新錯誤 / Bot 行為異常。

**處理**：
1. **先回滾再研究**：Railway Dashboard → Deployments → 找上一個正常版本 → **Redeploy**。（git 端等價操作：`git revert <壞 commit>` 後 push，讓 Railway 重建。）
2. 回滾後實測「!功能」「點數」與一次彩色化。
3. 在本機重現問題、修好、補測再上。

**預防**：deploy 後固定做 3 分鐘冒煙測試（傳「!功能」、傳「點數」、走一次圖片編輯）；重大改動選離峰時段上。

### 情境 13：環境變數錯誤／洩漏（P0）

**症狀**：啟動失敗（`XXX 環境變數未設定`）、簽名驗證全失敗、DB 連不上——通常發生在「有人動過 Railway Variables」之後。

**處理**：
1. 對照 `env_example.txt` 清點 Railway Variables 是否齊全拼對：`CHANNEL_ACCESS_TOKEN`、`CHANNEL_SECRET`、`REPLICATE_API_TOKEN`、`DATABASE_URL`、`SUPABASE_URL`、`SUPABASE_SERVICE_ROLE_KEY`、`SUPABASE_STORAGE_BUCKET`、`WELCOME_POINTS`、`COLORIZE_COST`、`EDIT_COST`、`SENTRY_DSN`。
2. 改完 Variables 記得 **Redeploy**（Railway 改變數不會自動重啟舊容器的 process 內快取）。

**若是金鑰洩漏（進了 git、貼到公開頻道）**：
1. 立即輪替：LINE Console 重發 channel access token、Supabase 重設 DB 密碼與 service role key、Replicate 重發 API token。
2. 更新 Railway Variables → Redeploy → 實測。
3. 檢查洩漏期間的異常用量（Replicate billing、`transactions` 異常流水、LINE push 用量）。

### 情境 14：LINE API rate limit / 訊息額度用完（P1）

**症狀**：logs 出現 `status=429`；或 push 全部失敗而 reply 正常（免費方案每月 push 額度用罄）。

**診斷**：
```bash
# 本月已用量與額度
curl -H "Authorization: Bearer $CHANNEL_ACCESS_TOKEN" https://api.line.me/v2/bot/message/quota
curl -H "Authorization: Bearer $CHANNEL_ACCESS_TOKEN" https://api.line.me/v2/bot/message/quota/consumption
```

**處理**：額度用罄 → LINE Official Account Manager 升級方案或等月初重置；429 突刺 → 已有指數退避重試，觀察是否自行恢復。**注意**：push 失敗期間圖片處理結果送不出去但點數已扣，恢復後依情境 4 的 SQL 撈受影響用戶手動補償。

### 情境 15：記憶體不足 / OOM（P1）

**症狀**：Railway 顯示服務被 OOM kill、無預警重啟；重啟瞬間所有進行中的圖片處理消失（用戶已扣點但沒收到圖）。

**背景**：每張處理中的圖以 bytes + base64 形式在記憶體，尖峰時 2 processes ×（4 workers + 8 queue）張圖同時在記憶體。

**處理**：
1. Railway Metrics 看記憶體曲線，確認 OOM 時間點與圖片尖峰重合。
2. 短期：調低 `IMAGE_QUEUE_LIMIT`（例如 4）限制同時在記憶體的圖量，或升級 Railway 方案加 RAM。
3. 善後：依情境 4 撈出重啟時間窗內「有扣點、`usage_logs` 無 failed 記錄、用戶沒收到圖」的案例，手動退點。

---

## 六、平時作戰演練計畫（建議每季跑一次）

在**離峰時段**用自己的測試帳號實際演練，每項演練驗證「症狀有被觀測到 + 按 playbook 能在目標時間內恢復」：

| # | 演練 | 做法 | 預期結果 |
|---|---|---|---|
| 1 | DB 斷線 | Supabase Dashboard 手動 Pause 專案 | Bot 回「系統維護中」；Restore + Restart 後功能恢復 |
| 2 | Storage 失效 | 把 `SUPABASE_STORAGE_BUCKET` 改成不存在的名稱並 redeploy | 圖片編輯上傳步驟失敗且**未扣點**；改回後恢復 |
| 3 | Replicate 失敗退點 | 把 `REPLICATE_API_TOKEN` 改成無效值 | 扣點→失敗→自動退點，`usage_logs` 出現 failed，用戶收到退點訊息 |
| 4 | 佇列滿載 | 暫時把 `IMAGE_WORKERS=1`、`IMAGE_QUEUE_LIMIT=1`，連續丟 3 張圖 | 第 3 張收到「目前使用人數較多」 |
| 5 | 回滾 | 部署一個無害改動，再用 Railway Redeploy 回上一版 | 5 分鐘內完成回滾且冒煙測試通過 |
| 6 | 帳目稽核 | 跑情境 7 的一致性 SQL | 0 筆不一致 |
| 7 | 金鑰輪替 | 照情境 13 流程輪替一把測試環境的 key | 全程服務中斷 < 5 分鐘 |

演練後把「卡住的地方」記進本文件對應情境。

---

## 七、待補強清單（本次模擬暴露的缺口，按優先順序）

1. **`/health` endpoint + 外部監控**：現在服務掛掉只能靠用戶回報。加一個檢查 DB 連線的 `/health`，配 UptimeRobot / Railway healthcheck。（情境 1）
2. **跨 process 的 webhook 去重**：記憶體去重在 `-w 2` 下有缺口，改用 Postgres `processed_events` 表或 Redis。（情境 3）
3. **推送失敗的自動退點**：目前「處理成功但 push 失敗」用戶會白扣點，靠人工補償。（情境 4）
4. **Supabase connection pooler（port 6543）**：根治連線池上限問題。（情境 6）
5. **`transactions` 加 `source/created_by` 欄位**：帳目稽核時能分辨寫入來源。（情境 7）
6. **Storage bucket 生命週期清理**：自動刪除超過 1 天的暫存圖。（情境 8）
7. **Replicate model ID 改環境變數 + `run_replicate` 加 timeout**：換模型免部署、避免 worker 長時間吊死。（情境 10、11）
