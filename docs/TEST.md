# 測試文件

> 涵蓋兩大塊：
> - **第一部　功能與整合測試環境**——開發/測試時完全不影響線上用戶（真實會員、點數、交易）。
> - **第二部　壓力測試**——上線前用數據確認尖峰湧入撐得住，含如何執行與實測結果。
>
> 相關文件：[部署](./DEPLOYMENT.md)、[擴展與運營](./OPERATION.md)、[系統健檢](./HEALTH_CHECK.md)

---

## 一、核心觀念

所有外部依賴（LINE、DB、Storage、Replicate、Sentry）都透過**環境變數**注入，所以「開一套測試環境」＝「準備另一組環境變數 + 另一組外部資源」，**不需要改任何程式碼**。

唯一的核心風險：測試建立的假會員、扣點、交易，**絕不能寫進線上資料**。每個外部依賴都要指向獨立的測試資源。

---

## 〇、離線測試（零依賴，先跑這個）

改動任何流程／狀態機／金流後，先跑這套：不需要資料庫、LINE channel、Supabase、Replicate，全部以 fake 取代（見 `test/conftest.py`），直接驅動 `FeatureRegistry` 的路由。

```bash
pip install -r requirements-dev.txt   # 只需一次
pytest
```

| 檔案 | 涵蓋 |
|---|---|
| `test_image_flow.py` | 先傳圖問意圖 → 交棒 → 選描述 → 直接扣點出圖的完整路徑（拿到圖＋描述就開始做，無確認關卡），以及取消／換圖／點數不足／群組靜默／中途切功能等岔路，另含中斷路徑都不留 Storage 孤兒圖 |
| `test_routing.py` | 路由層契約：註冊順序即優先序、`photo_intent` catch-all 必須最後、全局命令可在流程中途穿透且不破壞流程、其他功能的觸發指令不被當成輸入吃掉 |
| `test_billing.py` | 金流：扣點後才執行、失敗退點並清狀態、扣不到點就不動用外部資源、執行緒池滿載時降級 |
| `test_cleanup_storage.py` | 清理腳本的時間戳解析（含 Supabase 的超微秒精度格式） |
| `test_payment.py` | 儲值：CheckMacValue 驗簽、點數包設定驗證、建單快照定價，以及回調的「只發一次點」——重送、竄改金額、偽造簽章、付款失敗各走一條 |
| `test_topup_command.py` | 「儲值」指令與入口顯示：金流或 LIFF 少一半就完全不提儲值 |

同一套測試由 GitHub Actions 在每次 push / PR 時於 Python 3.9 與 3.12 上執行（`.github/workflows/ci.yml`）。

> 有兩項標記為 `xfail`：「每則訊息只查一次狀態」。這是已知缺口——路由層查好的 state 沒有傳進 `handle_*`，功能內會再查一次 DB。測試先寫著，等路由層收斂時把標記拿掉就有回歸保護。

`test/test_local.py` 是另一回事：它對著執行中的伺服器發 HTTP，屬於下面的整合測試，不在 `pytest` 套件內。

下面幾章講的是需要真實外部資源的整合測試。

---

## 二、選一種測試環境

| | 做法 A：本地 standalone（最快） | 做法 B：本地 + 測試 Supabase | 做法 C：Railway 常駐 staging |
|---|---|---|---|
| 適用 | 純功能開發、單機驗證 | 要驗證與 Altide 整合 | demo、給家人試用、上線前驗收 |
| DB | 本機 Postgres，只建 `grandpa_yin.*` | 測試 Supabase 專案（含 `public.*`） | 測試 Supabase 專案 |
| `DEPLOY_MODE` | `standalone`（**不需要 Altide**） | `platform` | `platform` |
| 迭代速度 | 最快 | 快 | 較慢（要 push / deploy） |

**日常開發建議用 A**：不依賴 Altide、不碰雲端，一鍵起本機庫即可跑完整點數流程。需要驗證整合時再用 B/C。

---

## 三、環境變數（`.env`）

複製範本後填入**測試環境的值**：

```bash
cp .env.example .env
```

### 做法 A：本地 standalone 範本

```env
DEPLOY_MODE=standalone
DATABASE_URL=postgresql://<你的user>@localhost:5432/grandpa_yin_dev

CHANNEL_ACCESS_TOKEN=測試channel的access_token
CHANNEL_SECRET=測試channel的secret
REPLICATE_API_TOKEN=your_replicate_api_token   # 會計費，量小可共用

PORT=5000
FLASK_DEBUG=True        # 本地開發自動重載
LOG_LEVEL=DEBUG         # 測試時看詳細 log
SENTRY_DSN=             # 留空不啟用

COLORIZE_COST=10
EDIT_COST=5
WELCOME_POINTS=50

# 固定 ngrok 網址（領了 static domain 再填，見第六章）
# NGROK_DOMAIN=your-name.ngrok-free.app
```

### 做法 B/C：整合（platform）額外項

```env
DEPLOY_MODE=platform
DATABASE_URL=postgresql://postgres:[測試專案密碼]@db.[測試PROJECT-ID].supabase.co:5432/postgres
SUPABASE_URL=https://[測試PROJECT-ID].supabase.co
SUPABASE_SERVICE_ROLE_KEY=測試專案的service_role_key
SUPABASE_STORAGE_BUCKET=linebot-temp-images
SENTRY_ENVIRONMENT=staging
```

> 完整變數清單見[部署文件第四章](./DEPLOYMENT.md#四環境變數清單)。

---

## 四、本地資料庫（做法 A）

用本機 Postgres，一鍵照 model 建表：

```bash
createdb grandpa_yin_dev
# .env 設 DATABASE_URL=postgresql://<user>@localhost:5432/grandpa_yin_dev
python test/setup_test_db.py
```

`setup_test_db.py` 會：建立所需 schema → 照 SQLAlchemy model `create_all` 建表 → 把 Alembic 版本 `stamp` 到 head（讓本地 DB 與 migration 一致，日後改 model 只套差異）。

內建**安全鎖**：只允許 `DATABASE_URL` 指向本機（localhost / 127.0.0.1 / socket），避免誤建到線上。

> standalone 模式下，`grandpa_yin.subjects` / `wallet_transactions` 承擔身份與點數，**完全不需要 `public.accounts` 等 Altide 表**即可跑完整流程（註冊→發獎→加點→扣點→退點→歷史）。
>
> 註：本地表由 model 生成，不含線上的 CHECK / RLS / trigger，足夠功能測試；要與線上完全一致請用做法 B（測試 Supabase 專案）並套用 `altide-landing-page/supabase/schema.sql`。

---

## 五、本地伺服器（做法 A / B）

```bash
./start_local_server.sh
```

腳本會自動：啟動 Flask（改碼熱重載）→ 啟動 ngrok（有 `NGROK_DOMAIN` 就用固定網址，否則隨機）→ **自動呼叫 LINE API 設定並驗證 webhook**，不需手動進 Console 填。

### 首次 LINE Console 設定（只需一次）

Webhook 網址由腳本自動設定，但以下是 Console 專屬、API 改不了，第一次要在**測試 channel** 手動處理：

- **開啟「Use webhook」**——API 只能設網址，這個總開關要手動打開。
- **關閉「Auto-reply messages」**——否則官方罐頭回覆會插嘴。
- **加測試 Bot 為好友**——掃 Console QR code 才能傳訊息測試。

設定完後日常只要跑上面那條指令即可。

---

## 六、解決「webhook 每次要重填」

ngrok 免費版每次重啟給隨機網址。到 ngrok Dashboard → Domains 領一個固定 static domain，填進 `.env` 的 `NGROK_DOMAIN`：

```bash
ngrok config add-authtoken <your-token>   # dashboard.ngrok.com 取得
```

`start_local_server.sh` 讀到 `NGROK_DOMAIN` 就用固定網址 → webhook 網址永久不變，且腳本每次自動重設，Console **填一次永久有效**。

---

## 七、隔離原則（做法 B/C，每個依賴都要獨立）

| 依賴 | 為什麼要隔離 | 測試環境怎麼做 |
|---|---|---|
| **LINE Channel** | 一個 channel 的 webhook 只能設一個，共用會混進真實用戶 | 另開測試 channel，用獨立 `CHANNEL_*` |
| **Supabase DB** | 測試會建假會員、改點數、寫交易 | 另開 Supabase 專案當 staging，套用相同 migration |
| **Supabase Storage** | P圖大神暫存圖 | 用測試專案自己的 bucket |
| **Replicate** | 會**真的計費** | 量小可共用 token；不想花錢就 mock |
| **Sentry** | 測試錯誤污染線上告警 | `SENTRY_ENVIRONMENT=staging` 或留空 |

---

## 八、驗證確實隔離（做法 B/C 必做）

設定完，**先確認連的是測試庫而非線上**，再開始測：

```bash
# 用一個「線上一定存在、測試庫一定沒有」的會員名稱查詢
python scripts/trace_user.py 某個線上真實用戶的名字
```

- 回「找不到」→ 正確，連的是空的測試庫。
- 查得到真實用戶資料 → **危險！`DATABASE_URL` 指到線上了，立刻停下修正。**

---

## 八之二、測儲值（不必真的付錢）

綠界有測試環境，可以在拿到正式商店帳號前跑完整條路。四個 `ECPAY_*` 加 `LIFF_ID`、
`LINE_LOGIN_CHANNEL_ID` 填測試值即可（測試站網址與測試商店代號以綠界官方文件為準）。

**上線前一定要做的一件事**：用綠界測試環境實際跑一筆，確認 `CheckMacValue` 對得上。
那套編碼規則（.NET UrlEncode 的字元替換）容易有細微出入，本地測試只驗到「自我一致
且擋得下竄改」，驗不到「跟綠界算出來的一樣」。對不上的症狀是每一筆都失敗，很明顯。

不想開瀏覽器時，可以直接對 `/pay/ecpay/callback` 送一筆自己簽好的回調：

```python
from src.services.ecpay_client import check_mac_value
payload = {"MerchantTradeNo": "<訂單編號>", "RtnCode": "1", "TradeAmt": "250", ...}
payload["CheckMacValue"] = check_mac_value(payload, HASH_KEY, HASH_IV)
```

送兩次，餘額只會增加一次——這是這條路徑最該驗的行為。

---

## 九、常見坑

1. **絕不要把測試 `DATABASE_URL` 指到線上 DB**——唯一會釀成災難的錯誤。用第八章先驗證。
2. **做法 B 的測試 DB 要套用相同 schema**——否則報 `column/table does not exist`。
3. **webhook 一個 channel 只能填一個 URL**——這就是測試必須用獨立 channel 的原因。
4. **Replicate 共用 token 會計費**——量大時考慮 mock 或獨立低額度帳號。
5. **`.env` 不可 commit**——已在 `.gitignore`。

---
---

# 第二部　壓力測試（Load / Spike / Flood / 生成容量）

> 腳本在 `test/load/`。目標：在把服務丟到社團之前，用數據回答三個問題——
> 1. **尖峰湧入會不會崩？**（社團貼文 = 突發流量）
> 2. **DB 連線池會不會爆？**（Nano/Micro 上限 60）
> 3. **長時間跑會不會記憶體/連線洩漏？**

## 壓-1. 範圍與方法

| 路徑 | 打什麼 | 測到什麼 | 環境 |
|---|---|---|---|
| `GET /health` | `health_load.js` | DB 連線池、記憶體、基礎設施飽和度 | **可對正式環境**（零副作用） |
| `POST /webhook` | `webhook_load.js` | 收訊路徑、DB 寫入、去重、HTTP 執行緒 | **僅 staging** |
| 生成 worker pool | `generation_capacity.py` | 有界池的容量、忙碌拒絕、排隊時間、握圖 RAM | **本機**（import 真實 `task_executor`，假 Replicate） |

**分層策略**：先用 `/health` 對正式環境安全地摸清基礎設施上限；擬真的收訊/生成測試一律在 staging 做。

## 壓-2. 前置準備（Checklist）

- [ ] `brew install k6`（已完成，v2.2.0）
- [ ] 拿到正式 `TARGET` 網址（例：`https://linebot-production-d32b.up.railway.app`）
- [ ] 開好三個監看分頁：
  - [ ] **Supabase → Database → Connections**（看即時連線數）
  - [ ] **Railway → 服務 → Metrics**（RAM / CPU）
  - [ ] **Sentry**（壓測時有無新錯誤湧入）
- [ ] （擬真測試才需要）**staging 環境**：Railway 另一 service + Supabase 另一專案 + Replicate mock
- [ ] 選一個**低流量時段**跑，避免影響真實用戶

## 壓-3. 指標與通過標準（SLO）

| 指標 | 來源 | 通過標準 | 說明 |
|---|---|---|---|
| p95 延遲 | k6 `http_req_duration` | `/health` < 800ms；`/webhook` < 1s | 95% 請求要夠快 |
| 錯誤率 | k6 `http_req_failed` | < 1%（webhook < 2%） | 5xx / 逾時比例 |
| DB 連線數 | Supabase | 峰值 **< 60** | 逼近就要縮池或升 compute |
| RAM | Railway | 峰值不貼頂、**soak 後會回落** | 不回落 = 洩漏 |
| 優雅降級 | 回應內容 | 超載時回「系統繁忙」，**非 5xx / 非崩潰** | 這是有界池的重點 |

> 這些門檻已寫進腳本的 `thresholds`，破線時 k6 直接以非 0 結束。

## 壓-4. 測試情境（由淺入深，照順序跑）

### T1 — Baseline（基準，1 VU）
確認閒置時單一請求的正常延遲。當作後面所有數字的對照。
```bash
k6 run -e TARGET=$URL -e MODE=steady --vus 1 --duration 30s test/load/health_load.js
```
**判準**：p95 應該很低（幾十～百餘 ms）。若這裡就慢，是基礎問題，不用往下測。

### T2 — Load（穩定負載，MODE=steady）
模擬「預期的日常尖峰」持續一段時間。
```bash
k6 run -e TARGET=$URL -e MODE=steady test/load/health_load.js
```
**判準**：p95、錯誤率在 SLO 內；DB 連線平穩、不成長。

### T3 — Spike（突發尖峰，MODE=spike）⭐ 最關鍵
10 秒內從 5 衝到 150 VU，模擬社團貼文一出大家同時湧入。
```bash
k6 run -e TARGET=$URL -e MODE=spike test/load/health_load.js
```
**判準**：
- 服務**不崩**（沒有一連串 5xx）。
- DB 連線衝高但**不破 60**；尖峰過後**回落**。
- 允許延遲短暫升高，但不該長時間卡住。

### T3.5 — Flood（開放模型洪水，MODE=flood）
Spike 是 **closed model**（固定 VU、等回應才發下一個），伺服器一慢就自我節流，偏溫和。Flood 是 **open model**：用 `constant-arrival-rate` **固定每秒硬灌 N 個**，不管伺服器回不回得動——這才逼得出「排隊爆掉、大量掉包」的真實洪水。
```bash
k6 run -e TARGET=$URL -e MODE=flood -e RATE=500 -e DURATION=1m test/load/health_load.js
```
- `-e RATE=500`：每秒**總**請求數（不是 per-VU）；k6 自動開 VU 去湊這個速率。
- **要看的新指標**：`dropped_iterations`（k6 連請求都發不出去＝伺服器撐不住到達率）。
**判準**：找出「舒適速率」（掉包趨近 0）與「膝蓋」（錯誤率開始破 1%）。逐步加 `RATE` 直到明顯掉包，就是這台的**實際吞吐天花板**。⚠️ 對正式環境跑會讓真實用戶短暫變慢，用低流量時段、縮短 `DURATION`。

### T4 — Soak（持久，MODE=soak）
中等負載跑 30 分鐘，抓慢性問題。
```bash
k6 run -e TARGET=$URL -e MODE=soak test/load/health_load.js
```
**判準**：RAM **持平**（非一路往上）；連線數**用完會還**（非只增不減）。任一往上爬 = 洩漏，要查。

### T5 — Stress / Breakpoint（找天花板，選做）
持續加壓直到指標破線，記下「開始崩的 VU 數」= 你的**實際餘裕**。
```bash
k6 run -e TARGET=$URL --stage 30s:50 --stage 30s:100 --stage 30s:200 --stage 30s:400 test/load/health_load.js
```
**判準**：不是要它通過，是要**知道極限在哪、先崩的是什麼**（DB 連線？RAM？CPU？）——上線後才知道離牆多遠。

### T6 — Webhook 擬真（staging）
```bash
k6 run -e TARGET=$STAGING -e CHANNEL_SECRET=xxx -e MODE=spike test/load/webhook_load.js
```
**判準**：webhook 快速回 200；DB 寫入路徑不爆連線。（LINE 回覆會失敗，屬預期。）

### T7 — Worker pool 容量與降級（本機，`generation_capacity.py`）
不走 HTTP。直接 `import` 真實的 `submit_image_task`，餵假的生成函式（sleep 冒充 Replicate，握著一塊圖大小的隨機 bytes），模擬 N 人同時湧入。回答 `/health` 測不到的問題：**第幾個開始「忙碌」、同時真正跑幾個、排隊等多久、握圖吃多少 RAM**。零網路、零費用、零副作用。
```bash
# 預設 IMAGE_WORKERS=4 / IMAGE_QUEUE_LIMIT=8：容量 12，第 13 人起忙碌
USERS=100 GEN_SECONDS=40 MB_PER_JOB=3 python3 test/load/generation_capacity.py

# 驗證調大：容量 24，看更多人進得來、RAM 變化
IMAGE_WORKERS=8 IMAGE_QUEUE_LIMIT=16 USERS=100 python3 test/load/generation_capacity.py
```
| 變數 | 意義 | 預設 |
|---|---|---|
| `USERS` | 同時湧入的人數 | 20 |
| `GEN_SECONDS` | 假生成耗時（設成真實 Replicate 秒數才擬真） | 6 |
| `MB_PER_JOB` | 每張輸入圖佔的記憶體（用隨機 bytes 模擬不可壓縮的 JPEG） | 5 |
| `IMAGE_WORKERS` / `IMAGE_QUEUE_LIMIT` | 覆寫 pool 大小（import 時讀取） | 4 / 8 |

**判準**：超量的人收到「忙碌」（`submit` 回 False）、**不崩**；RAM 隨容量線性成長且在 Railway 上限（48GB）內。
> 注意：這是**單一 process** 視角；正式環境 `-w 2` 有兩個獨立池，**全站容量 ≈ 這裡的 2 倍**。

## 壓-5. 執行順序

```
T1 Baseline ──► 數字正常？──► T2 Load ──► SLO 內？──► T3 Spike ⭐ ──► 不崩、連線<60？
                                                              │（上線核心關卡）
                                                              ▼
                        T4 Soak ──► RAM/連線持平？──► T5 Stress（選做）──► T6/T7 擬真&降級
```
每一階做完就**記錄結果**（見下），任一階破線就**停下來調校再重跑**，不要硬往下。

## 壓-6. 結果記錄表

| 情境 | 日期 | 峰值 VU / 速率 | p95 | 錯誤率 | 吞吐 | 通過? | 備註 |
|---|---|---|---|---|---|---|---|
| T1 Baseline | 2026-09-07 | 1 VU | 87ms | 0% | 14/s | ✅ | 基準線 |
| T2 Load (steady) | 2026-09-07 | 20 VU | 99ms | 0% | 202/s | ✅ | 20 倍併發延遲幾乎不動 |
| T3 Spike | 2026-09-07 | 150 VU | 257ms | 1.21% | 638/s | ⚠️ | 1.2% 是**優雅降級 503**，非崩潰；CPU 打滿 2.3 vCPU |
| Flood 500/s | 2026-09-07 | 500/s | 117ms | 0.17% | 497/s | ✅ | 舒適區，dropped=43 |
| Flood 800/s | 2026-09-07 | 800/s | 1.08s | 1.52% | 765/s | ⚠️ | 超載，dropped=1264；實際吞吐天花板 ~765/s |

DB 連線全程 **< 20/60**（Supavisor pooler 保護，從未逼近 60）；RAM 全程平穩 **~340MB**（無洩漏）。

**生成 worker pool（T7，`generation_capacity.py`，2026-09-08）**

| 設定 | 容量（單 process） | 100 人湧入：收下 / 忙碌 | 同時生圖 | 握圖 RAM（本機量） |
|---|---|---|---|---|
| 4 / 8（預設） | 12 | 12 / 88 | 4 | +50MB（12×~4MB） |
| 8 / 16（建議上線值） | 24 | 24 / 76 | 8 | +75MB |

> 全站 `-w 2` 為 2 倍：8/16 設定下 **同時生圖 16、系統容量 48、可持續 ~24 人/分**。

## 壓-7. 判讀 → 調校對照

| 觀察到 | 可能原因 | 對策 |
|---|---|---|
| DB 連線逼近 60 | 池開太大 / 流量高 | 縮 `pool_size`，或升 Supabase compute |
| 錯誤是「等不到 DB 連線」 | 池不夠 HTTP+worker 用 | 調高 `pool_size`（同時確認沒破 60） |
| RAM 貼頂 / OOM | 併發太高、檔案佔記憶體 | 降 `IMAGE_WORKERS`，或升 Railway 方案 |
| RAM soak 後不回落 | 記憶體洩漏 | 查程式，別急著上線 |
| 尖峰時大量 5xx | 沒優雅降級 / 崩潰 | 檢查有界池是否生效、timeout 設定 |
| 延遲高但錯誤率低 | 排隊中，尚可接受 | 視體感決定是否加 worker/compute |

## 壓-8. Go-Live 判準

**全部滿足才上線：**
- [ ] T3 Spike：不崩、DB 連線 < 60、尖峰後回落
- [ ] T4 Soak：RAM 與連線持平無洩漏
- [ ] Sentry 壓測期間無未預期的新錯誤類型
- [ ] 已知極限（T5）距離預期流量有**至少 2～3 倍餘裕**
- [ ] 前置的基礎設施到位：**Supabase Pro**、告警、Replicate spend 上限

## 壓-9. 安全提醒

- 對**正式環境只跑 `/health`**，且從小量、低流量時段開始。
- `webhook_load.js` **只對 staging**——會寫正式 DB、發失敗的 LINE 呼叫。
- 壓測會讓 UptimeRobot 之類可能誤報，必要時先暫停告警再測。
- Stress test（T5）本來就是要把它壓到破——**別對正式環境跑到崩**，那種請在 staging 做。
- Flood（T3.5）open model **不自我節流**，對正式環境衝擊比 spike 大，`RATE` 從小開始、`DURATION` 縮短。

## 壓-10. 本次實測結論（2026-09-07/08）

### 極限地圖：`/health`（基礎設施）
```
  舒適 ≤500/s        膝蓋 ~640/s          超載 800/s
  0.17% 掉           1.2% 掉               1.5% 掉 + 大量排隊
  秒回               開始痛                尾巴爆(p95>1s)、發不出去
```
- **實際吞吐天花板 ~765/s**（餵 800、吐 765，其餘掉包）。
- **瓶頸是 Railway CPU**（`-w 2` + Python GIL，尖峰 ~2.3 vCPU），**不是** DB（Supavisor 保護、連線 <20）、**不是**記憶體（平穩 340MB）。
- **1.2~1.5% 的失敗是優雅降級的 503**（`/health` 誠實回報 DB 忙），非崩潰 → 韌性良好。

### 各資源是不是瓶頸
| 資源 | 結論 |
|---|---|
| Replicate | ✅ 建立上限 600/min、託管模型自動擴充，不是限制 |
| DB 連線 | ✅ 生成等待期間**不佔連線**（billing 頭尾各借一下）；Supavisor 保護 60 上限 |
| RAM | ✅ Railway Hobby 上限 **48GB**，實際用 <1%；隨生成容量線性成長仍極小 |
| CPU | ✅ 生成 I/O-bound（worker 多在等 Replicate），不吃 CPU |
| **真正的取捨** | 生成 pool 容量（`IMAGE_WORKERS`）＝ 排隊時間 vs Replicate 費用的平衡 |

### 生成路才是真瓶頸（不是 `/health` 的 765/s）
- 預設 pool **同時只跑 4（全站 8）**、容量 12（全站 24），第 25 人起收到「忙碌」。
- 建議上線值 **`IMAGE_WORKERS=8` / `IMAGE_QUEUE_LIMIT=16`**（全站同時 16、容量 48、~24 人/分）。
- **不要無腦開大**（例如 100/200）：會拆掉斷路器 → 同時上百筆 Replicate 帳單、DB 池被打爆、LINE 推播限流。「忙碌」訊息是**保護功能**。
- 更大規模的擴展路徑見 [擴展與運營文件](./OPERATION.md)。
