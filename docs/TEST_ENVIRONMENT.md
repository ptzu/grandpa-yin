# 測試環境文件

> 目的：開發與測試時完全不影響線上用戶（真實會員、點數、交易）。
> 相關文件：[部署](./DEPLOYMENT.md)、[系統健檢](./HEALTH_CHECK.md)

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
| `test_image_flow.py` | 先傳圖問意圖 → 交棒 → 選描述 → 確認扣點的完整路徑，以及取消／換圖／重新描述／點數不足／群組靜默／中途切功能等岔路，另含八條中斷路徑都不留 Storage 孤兒圖 |
| `test_routing.py` | 路由層契約：註冊順序即優先序、`photo_intent` catch-all 必須最後、全局命令可在流程中途穿透且不破壞流程、其他功能的觸發指令不被當成輸入吃掉 |
| `test_billing.py` | 金流：扣點後才執行、失敗退點並清狀態、扣不到點就不動用外部資源、執行緒池滿載時降級 |
| `test_cleanup_storage.py` | 清理腳本的時間戳解析（含 Supabase 的超微秒精度格式） |

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
| **Supabase Storage** | 圖片編輯暫存圖 | 用測試專案自己的 bucket |
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

## 九、常見坑

1. **絕不要把測試 `DATABASE_URL` 指到線上 DB**——唯一會釀成災難的錯誤。用第八章先驗證。
2. **做法 B 的測試 DB 要套用相同 schema**——否則報 `column/table does not exist`。
3. **webhook 一個 channel 只能填一個 URL**——這就是測試必須用獨立 channel 的原因。
4. **Replicate 共用 token 會計費**——量大時考慮 mock 或獨立低額度帳號。
5. **`.env` 不可 commit**——已在 `.gitignore`。
