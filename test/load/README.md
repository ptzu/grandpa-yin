# 壓力測試（Load / Spike / Flood / Soak）

> 這裡是**腳本的快速參考**。完整計畫、執行順序、SLO 與**實測結果**見 [`docs/TEST.md`](../../docs/TEST.md) 第二部「壓力測試」。

用 [k6](https://k6.io) 驗證上線前最擔心的兩件事：**社團尖峰湧入時會不會崩**、**DB 連線池會不會爆**。

## 安裝 k6

```bash
brew install k6          # macOS
# 或見 https://k6.io/docs/get-started/installation/
```

## 兩支腳本

| 腳本 | 打哪 | 副作用 | 何時用 |
|---|---|---|---|
| `health_load.js` | `GET /health` | **無**（不回 LINE、不打 Replicate、不寫資料） | 主測試；**可對正式環境跑** |
| `webhook_load.js` | `POST /webhook` | 寫 DB、觸發（會失敗的）LINE 回覆 | 擬真收訊測試；**只對 staging 跑** |

> `/health` 會真的查一次 DB，所以壓它就能測到**共用連線池**和**記憶體**——這正是尖峰時最脆弱的地方——而且零成本零副作用。這是最划算的第一步。

## 怎麼跑

負載模式用 `-e MODE=` 切換：`steady`（穩定負載）、`spike`（突發尖峰，模擬社團貼文）、`soak`（長時間，抓記憶體/連線洩漏）、`flood`（open model，用 `-e RATE=` 每秒固定硬灌，不自我節流，逼出真實掉包）。

```bash
# 安全：對正式環境壓 /health，模擬尖峰
k6 run -e TARGET=https://your-app.up.railway.app -e MODE=spike test/load/health_load.js

# 擬真：對 staging 壓 /webhook（需帶 CHANNEL_SECRET 算簽章）
k6 run -e TARGET=https://staging.up.railway.app \
       -e CHANNEL_SECRET=你的_channel_secret \
       -e MODE=spike test/load/webhook_load.js
```

## 跑的時候「同時盯」這些（測試的重點不在 k6 數字本身）

| 看板 | 看什麼 | 警戒 |
|---|---|---|
| **Supabase → Database** | 連線數 | 逼近 **60**（Nano/Micro 上限）就是連線池要調小或升 compute |
| **Railway → Metrics** | RAM / CPU | RAM 一路往上不回落 = 洩漏；貼頂 = 要升方案 |
| **k6 summary** | `p(95)`、`http_req_failed`、`http_reqs`/s | 延遲飆高或錯誤率破線 = 到瓶頸了 |
| **Sentry** | 有無新錯誤湧入 | 壓測時爆一堆 = 併發下才會現形的 bug |

`thresholds` 破線時 k6 會以非 0 結束——可直接接 CI 當回歸防線。

## 測「AI worker pool 的優雅降級」

`/webhook` 的文字流程**不會**觸發 Replicate（那要走完「上傳圖 → 確認 → 生成」的有狀態流程），所以用 k6 打 HTTP 測不到 worker pool。改用 **`generation_capacity.py`**：它直接 `import` 真實的 `submit_image_task`，餵假的生成函式（sleep 冒充 Replicate），本機測「容量、忙碌拒絕、排隊時間、握圖 RAM」，零網路、零費用、零副作用。

```bash
# 100 人同時湧入、每張假生成 40 秒、每張圖 3MB
USERS=100 GEN_SECONDS=40 MB_PER_JOB=3 python3 test/load/generation_capacity.py

# 驗證調大 pool
IMAGE_WORKERS=8 IMAGE_QUEUE_LIMIT=16 USERS=100 python3 test/load/generation_capacity.py
```

要用真實 Replicate 端到端驗證（會燒錢），才需要 staging + mock，見 [`docs/TEST.md`](../../docs/TEST.md) 的 T6/T7。

## ⚠️ 安全提醒

- `webhook_load.js` **只對 staging/local 跑**——它會寫正式 DB，且對假 replyToken 發出（失敗的）LINE 呼叫。腳本的 `setup()` 會在 TARGET 不像 staging 時大聲警告。
- 對正式環境只跑 `health_load.js`，且避開尖峰時段、從 `steady` 小量開始，逐步加。
