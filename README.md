# 銀爺爺 LINE Bot

專為長輩設計的 LINE Bot：用 AI 幫黑白老照片上色、依文字描述編輯圖片，並內建點數會員系統。可獨立運作（standalone），也可整合進 Altide 共用一套會員／錢包（platform）——由 `DEPLOY_MODE` 切換。

技術架構：**Flask + gunicorn**（Railway 部署）· **Supabase PostgreSQL** · **LINE Messaging API** · **Replicate**（AI 模型）· **Sentry**（錯誤追蹤）。

## 文件

| 文件 | 內容 |
|---|---|
| [部署](docs/DEPLOYMENT.md) | 上線步驟、環境變數、Alembic 自動 migration、營運與事故處理 |
| [測試環境](docs/TEST_ENVIRONMENT.md) | 本地 standalone / 整合測試、環境變數、本地資料庫與伺服器 |
| [開發日誌](docs/DEVELOPMENT_LOG.md) | 重要架構決策與里程碑編年 |
| [系統健檢](docs/HEALTH_CHECK.md) | 架構健康度、修復進度、待補強清單 |

## 功能

| 功能 | 觸發指令 | 說明 | 點數 |
|---|---|---|---|
| 功能選單 | `功能`、`使用說明` | Quick Reply 選單，引導其餘功能 | — |
| 照片意圖詢問 | **直接傳照片** | 沒先選功能就上傳的照片由它接住，Quick Reply 問要上色還是修改 | — |
| 修復老照片 | `修復老照片` | 上傳老照片 → AI 自動上色 | 10（可設定）|
| P圖大神 | `P圖大神` | 傳圖 → 點選（或自行輸入）編輯描述 → 確認後才扣點 | 5（可設定）|
| 照片動起來 | `照片動起來` | 傳圖 → 確認後產生約 5 秒微動影片 | 25（可設定）|
| 做完的下一步 | **成品後的按鈕** | 拿剛做好的照片直接做成影片或再修一次，不必重傳 | 依接手的功能 |
| 會員／點數 | `會員`、`點數`、`歷史` | 查詢點數餘額與交易記錄 | — |
| 兌換禮物卡 | `兌換` | 輸入家人買給他的卡號，點數入帳 | — |

> 指令刻意只留單一寫法（一個功能一個詞），不做同義詞比對；長輩主要靠 Quick Reply 按鈕操作。清單見 `src/features/feature_registry.py` 的 `GLOBAL_COMMANDS`，`test/test_commands.py` 有把關。

- **模型、點數、贈點都在 `config/settings.yml`**，換模型／調價不必改程式碼（見下方）。
- 新用戶加好友自動建立會員並贈送 `members.welcome_points`（預設 50）點。
- 圖片以背景非同步處理，不阻塞使用者；多用戶對話狀態持久化於 `grandpa_yin.bot_sessions`。
- **成品保留 30 天**：推送前轉存進自家 Storage，用戶往上滑對話紀錄重看都還在（模型端的網址約一小時就失效）。
- **主要入口是「直接傳照片」**：長輩的心智模型是「處理這張照片」而不是「進入某個功能」。傳統的「先打指令再傳圖」路徑同時保留。
- **做完不會斷線**：成品後面附一排「還要再做點什麼嗎」的按鈕，成品本身就是下一次的輸入（上色完可以直接做成影片）。輸出影片的功能不給（影片餵不回圖片模型），群組聊天也不給。
- P圖大神全程可用 Quick Reply 完成（免打字），且**確認之後才扣點**，在那之前可隨時取消或換圖。
- **禮物卡讓「付錢的人」和「用的人」分開**：子女在網頁上買（不必登入 LINE、電腦也能買），拿到一組卡號傳給長輩，長輩輸入「兌換」即可入帳。長輩過不了金流那關是這個產品最大的付費障礙，這條路繞過它。

## 開啟測試環境

完整說明見 [測試環境文件](docs/TEST_ENVIRONMENT.md)。快速版（本地開發，零雲端資料庫）：

### 1. 安裝依賴

```bash
pip install -r requirements.txt      # Python 套件
brew install --cask ngrok            # 對外隧道（LINE webhook 需要公網入口）
```

### 2. 設定 `.env`

```bash
cp .env.example .env
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
./start_local_server.sh
```

腳本會自動啟動 Flask（改碼熱重載）→ 啟動 ngrok（有 `NGROK_DOMAIN` 就用固定網址）→ **自動呼叫 LINE API 設定並驗證 webhook**，不需手動進 Console 填。

### 6. 首次 LINE Console 設定（只需一次）

Webhook **網址**由腳本自動設定，但以下幾項是 Console 專屬、API 改不了，第一次要在測試 channel 的 Messaging API 頁手動處理：

- **開啟「Use webhook」** — API 只能設網址，這個「是否真的把訊息送到 webhook」的開關要手動打開（打開後就一直有效）。
- **關閉「Auto-reply messages」** — 否則 LINE 官方罐頭回覆會插嘴。
- **加測試 Bot 為好友** — 掃 Console 的 QR code，才能對它傳訊息測試。
- **Webhook redelivery 維持關閉**（預設就是關的，別順手打開）— 打開會讓 LINE 重送失敗的 event，而目前的去重表在記憶體裡，`-w 2` 下會漏擋、重複扣點。原因與開啟前提見[部署文件](docs/DEPLOYMENT.md)。

設定完後，日常只要跑第 5 步即可，網址每次自動更新，不必再碰 Console。
> 未設 `NGROK_DOMAIN` 時 ngrok 為隨機網址，重跑腳本會變（腳本會自動重設 webhook）；設了固定網域則永久不變。

## 部署（Railway）

1. 在 [Railway](https://railway.app/) 建專案並連結此 repo。
2. 於 Variables 設定環境變數：`CHANNEL_ACCESS_TOKEN`、`CHANNEL_SECRET`、`REPLICATE_API_TOKEN`、`DATABASE_URL`（Supabase 連線字串），可選 `SENTRY_DSN` / `SENTRY_ENVIRONMENT`。模型與點數走 `config/settings.yml`，不必設變數。
3. Push 到 `main` 即自動部署（啟動指令見 `Procfile`：`gunicorn src.app:app -w 2 --threads 8`）。
4. 將 Railway 網域設為 LINE Webhook：`https://<your-app>.up.railway.app/webhook`。

### 部署模式：Platform / Standalone

本服務可以兩種模式運作，由環境變數 `DEPLOY_MODE` 決定（未設定時預設 `platform`）：

| | `platform`（整合進 Altide） | `standalone`（獨立） |
|---|---|---|
| 身份來源 | Altide `public.linked_identities` → `accounts` | 自有 `grandpa_yin.subjects` |
| 點數/交易 | Altide `public.accounts.points_balance` / `transactions` | 自有 `grandpa_yin.wallet_transactions` |
| 依賴 Altide | 是（需先有 `public.*` 共用層） | 否（只需 `grandpa_yin.*`，可完全獨立建起） |

切換點集中在 `src/services/account_backend.py`（`AccountBackend` port + 兩個 adapter）；業務邏輯（`member_service` / `user_state_manager`）只認 port，不直接碰帳號表。所以同一套程式碼能在有無 Altide 的環境各自運作。

> `standalone` 讓本產品能單獨開發、測試、demo；日後要接回 Altide 只需把 `DEPLOY_MODE` 設回 `platform`，並補上跨 schema 外鍵（見下方整合說明），不必改業務邏輯。

### 資料庫 schema 與自動 migration

schema 分兩層、各自管理：

- **共用層 `public.*`**（accounts / transactions / linked_identities）由 Altide 的 `altide-landing-page/supabase/schema.sql` 管理（含 `auth.*` / `storage.*` 依賴，僅適用於 Supabase）。本專案**不碰**。
- **產品層 `grandpa_yin.*`**（bot_sessions / usage_logs / user_profiles / payment_orders / gift_cards）由本專案的 **Alembic** 管理，migration 檔在 `alembic/versions/`。

每次部署，Railway 的 `preDeployCommand`（見 `railway.json`）會自動執行 `alembic upgrade head`，把 `grandpa_yin.*` 的 schema 更新到最新；失敗則中止部署（不會帶著壞 schema 上線）。

**改動流程**：改 `src/models/*.py` → `alembic revision --autogenerate -m "描述"` → 檢視產生的 migration → commit → push。部署時自動套用。

> **首次導入（線上 DB 已有 grandpa_yin.* 表時，只需做一次）**：因為表早已由 `schema.sql` 建好、但還沒有 Alembic 版本記錄，第一次啟用前要先把現況標記為 baseline，否則 `upgrade` 會嘗試重建已存在的表而失敗：
> ```bash
> DATABASE_URL=<線上連線字串> alembic stamp head
> ```
> 標記後，之後的部署才會只套用「新的」migration。

## 管理腳本

```bash
python scripts/add_points.py <名字> 50       # 為指定會員加點（加前會先確認）
python scripts/add_member.py                 # 互動式新增會員
python scripts/cleanup_user_states.py 24     # 清理超過 24 小時的舊對話狀態
python scripts/cleanup_storage.py            # 試跑：列出 Storage 中的孤兒暫存圖
python scripts/cleanup_storage.py --apply    # 實際刪除（建議掛 Railway cron 每日執行）
python scripts/trace_user.py <名字>          # 追查某會員的點數異動（排查用）
```

加點對象可以用**顯示名稱**（模糊查，同名會列清單讓你挑）或 **LINE userId**（精準）指定：

```bash
python scripts/add_points.py 王小明 50 --reason 朋友介紹   # 說明會出現在用戶的「歷史」
python scripts/add_points.py U1a2b3c... 50 --yes          # 跳過確認；此模式只吃 userId
```

## 專案結構

核心程式碼全部收在 `src/` 套件內，根目錄只留設定檔與開發用的啟動腳本。

```
src/                      ── 核心程式碼
  app.py                  主入口：webhook、組裝依賴、註冊功能
  core/                   跨切面基礎設施（不含領域知識，不依賴 services）
    app_logger.py         帶 request_id 的分級 logger
    error_tracking.py     Sentry 初始化與 context
    settings.py           讀 config/settings.yml（模型、點數、贈點）+ 驗證 CLI
    task_executor.py      背景工作的有界執行緒池
  features/               功能模組（繼承 base_feature.BaseFeature）
    context.py            FeatureContext：功能的依賴集合
    feature_registry.py   訊息路由與功能註冊
    photo_intent_feature.py  圖片路由 catch-all：先傳圖再問意圖
    animate_feature.py    照片動起來（唯一輸出影片的功能）
    menu / colorize / edit / member_feature.py
    gift_feature.py       禮物卡兌換的對話（「兌換」→ 問卡號 → 入帳＋收禮通知）
    replicate_feature.py  Replicate 圖片功能的共用對話面
  services/               外部系統與領域狀態的封裝
    line_client.py        LINE 收訊側（下載圖片、查名稱、載入動畫）
    message_publisher.py  LINE 發訊側（reply / push、重試退避）
    billing.py            計費背景任務：扣點 → 執行 → 失敗退點 → 滿載降級
    replicate_client.py   Replicate 模型呼叫與輸出解析
    result_archive.py     成品保存：轉存成品並換成 30 天有效的網址
    user_state_manager.py 對話狀態機（grandpa_yin.bot_sessions）
    member_service.py     會員服務層（點數、交易）
    member_directory.py   用名字／LINE userId 找會員（管理腳本用，唯讀）
    payment_service.py    儲值：建單 → 綠界回調 → 只發一次點／只開一張卡
    gift_card_service.py  禮物卡：開卡與兌換（卡號產生、正規化、只兌換一次）
    ecpay_client.py       綠界 CheckMacValue 產生與驗證（不連網）
    storage_service.py    Supabase Storage（圖片暫存）
    account_backend.py    AccountBackend port：standalone / platform 雙模式
  models/                 SQLAlchemy 模型（public.* 共用層 + grandpa_yin.* 產品層）
  templates/              付款／送禮／分享／完成頁（Flask 樣板，只有金流相關流程用到）

config/settings.yml       模型、點數、贈點、模型輸入欄位對應（部署前自動驗證）
start_local_server.sh     本地一鍵啟動（Flask + ngrok + 自動設定 webhook）
scripts/                  管理／排查腳本
test/                     pytest 套件（離線）＋ setup_test_db.py、test_local.py
docs/                     部署、測試環境、開發日誌、系統健檢
alembic/                  資料庫 migration（慣例與指令見 alembic/README.md）
  env.py                  範圍限定 grandpa_yin.*，連線字串取自 DATABASE_URL
  versions/               <時間戳>_<描述>.py，依時序排列
.env.example              環境變數範本（複製為 .env 後填值）
pytest.ini · alembic.ini · Procfile · railway.json · requirements.txt
```

依賴方向為單向：`features → services → models`，三者都可依賴 `core`，反向依賴不允許。**外部系統一律經 `services/` 呼叫**——功能層不直接碰 HTTP、SDK 或資料庫。

## 設定：模型、點數、贈點

營運上會想調的東西都在 **`config/settings.yml`** 一個檔案裡，不必改程式碼：

```yaml
features:
  edit:
    model: google/nano-banana-2
    cost: 5                     # 扣幾點
    loading_seconds: 45
    input:
      image_field: image_input  # 這個模型的圖片欄位叫什麼
      image_is_list: true       # 吃陣列還是單值
      prompt_field: prompt      # 描述放哪個欄位（不吃描述的填 null）
    extra_input:
      output_format: jpg

members:
  welcome_points: 50            # 新會員贈點，0 表示不送

payments:                       # 整段拿掉 = 關閉儲值
  provider: ecpay
  packages:
    - {id: s, points: 100, price_twd: 100, label: 100 點}
```

儲值另外需要四個環境變數（`ECPAY_MERCHANT_ID` / `ECPAY_HASH_KEY` / `ECPAY_HASH_IV` /
`ECPAY_API_URL`）與 `LINE_LOGIN_CHANNEL_ID`，見 `.env.example`。**沒設齊就自動停用**，
服務照常運作，只是沒有付款入口——密鑰不放 `settings.yml`，因為那個檔案在 git 裡。

點數只在 `/pay/ecpay/callback`（綠界的伺服器通知，且驗過 CheckMacValue）發放；
使用者付完款導回的 `/pay/done` 只顯示文字，不碰點數。同一筆訂單重複回調不會重複發點，
靠的是 `payment_orders.merchant_trade_no` 的唯一鍵加上 `credited_at` 的列鎖，而不是記憶體。

用戶端的流程是：輸入「儲值」或從「功能」選單點「➕ 加購點數」→ 收到付款頁連結 →
在 LIFF 頁選方案 → 綠界付款 → 幾秒後點數入帳，「歷史」看得到訂單編號。

**金流與 LIFF 少了任何一半，bot 就完全不提儲值**：選單不出現加購按鈕、「點數」不提示、
輸入「儲值」會明講「還沒開放」。給長輩一個按了沒反應的連結，比不提還糟。

### 禮物卡（家人買、長輩兌換）

長輩過不了金流那一關，是這個產品最大的付費障礙——所以**買的人可以不是用的人**：

```
子女開 /gift（一般網頁，不必登入 LINE）→ 選方案 → 綠界付款 → 拿到卡號
  → 「用 LINE 選家人傳過去」→ LINE 原生好友選擇器挑人 → 卡片送達
長輩收到卡片 → 點一下 → bot 開啟且訊息已填好 → 按送出
  → 點數入帳，bot 跳出「🎁 您收到一份禮物」
```

長輩全程**零打字**。不想用選擇器（或在電腦上付款）也可以複製卡號自己傳，
長輩再輸入「兌換」手動貼上。

- **購買頁刻意不走 LIFF**：買的人多半是子女，可能坐在電腦前、甚至不是 LINE 用戶。不記名的卡不需要知道買家是誰，多一道 LINE 登入只會多一個放棄點。分享頁（`/gift/share`）才是 LIFF——只有那一步需要 LINE。
- **「選好友送出」用 `liff.shareTargetPicker`，不是我們自己做的選單**：LINE **沒有**讀取好友清單的 API，挑中的是誰我們自始至終不會知道，訊息也是由買家自己的帳號送出的。這是 LINE 的隱私設計，繞不過去——能做的就是把卡片組好交給 LINE 的原生選擇器。
- **分享是獨立一頁，不是付款完成頁的一部分**：綠界的付款流程會把瀏覽器帶離本站再帶回來，LIFF 的執行環境不保證撐得過那一趟。獨立一頁等於重新啟動一次 LIFF，也讓買家能在電腦上付完款、改用手機開連結分享。
- **兌換深連結只能出現在「送出去的卡片」裡，不能出現在買家自己看的頁面上**：那個連結會開啟 bot 並帶入兌換訊息，放在完成頁等於請買家把禮物兌換給自己。`test_gift_command.py` 有測試釘住這件事。
- **卡只在驗過簽章的綠界回調裡開立**，跟發點數同一條路徑、同一個 `credited_at` 保護；重送回調不會多開一張卡（`gift_cards.order_id` 唯一）。
- **兌換只會成功一次**：`code` 唯一鍵 + 兌換時鎖列 + `redeemed_at`，不靠記憶體，多 worker 也擋得住。
- **卡號用 Crockford base32**（沒有 I/L/O/U），輸入時大小寫、空白、減號都可以，打成 I/L/O 也會自動當成 1/0——打字的人常常是 80 歲、對著孫子傳來的截圖看。
- **卡不設有效期**：台灣商品禮券不得記載使用期限，點數卡適不適用有灰色地帶，保守處理的成本很低。
- 兌換只需要資料庫，**與金流是否開著無關**：關掉金流之後，已經賣出去的卡還是兌換得了。

需要的環境變數：

| 變數 | 沒設會怎樣 |
|---|---|
| `PUBLIC_BASE_URL` | bot 完全不提禮物卡（它在 HTTP request 之外組訊息，沒有這個就拼不出送禮頁網址）。已賣出的卡仍兌換得了 |
| `LIFF_ID` | `/gift/share` 回 503，完成頁不出現「用 LINE 選家人傳過去」，只能複製卡號手動傳 |
| `LINE_BASIC_ID` | 卡片裡沒有「點這裡收下」按鈕，改為附上卡號與文字說明（長輩得自己輸入「兌換」）|

> `LIFF_ID` 與付款頁共用同一個 LIFF app。因為一個 LIFF app 只能設一個 Endpoint URL，而這裡有
> 兩頁要在 LINE 裡開，所以 **Console 的 Endpoint URL 要填服務根網址**（`https://<app>/`），
> 路徑由連結自己帶：付款是 `liff.line.me/<LIFF_ID>/pay`，分享是
> `liff.line.me/<LIFF_ID>/gift/share?no=<訂單編號>`。Endpoint 若填成 `.../pay`，分享連結會開到
> 付款頁去。另外要在該 LIFF app 開啟 **`shareTargetPicker`** 權限，否則分享頁會退回「自己複製卡號」。

換模型時**光改 `model` 不夠**：不同模型的欄位名稱不一樣（`nano-banana` 的圖片欄位叫 `image_input` 且吃陣列，`restore-image` 叫 `input_image` 吃單值），`input` 區段要一起調。檔案裡的註解附了幾個常用模型的對應可直接抄。

### 改設定的流程

```bash
# 1. 改 config/settings.yml
# 2. 先驗（印出實際生效的值，設定寫錯會指出哪裡錯）
python3 -m src.core.settings
# 3. push，Railway 自動部署，約 1～2 分鐘後生效
git commit -am "調整P圖大神點數" && git push
```

設定寫錯**不會上線**，有三道防線：本地的 `python3 -m src.core.settings`、CI（PR 會被擋下）、Railway 的 `preDeployCommand`（中止部署）。**換模型另外務必在本地實測一次**——欄位對應寫錯的話設定本身是合法的，驗不出來，但線上每次處理都會失敗。

> 不想部署也能臨時改：Railway 的 Variables 設 `EDIT_COST` / `COLORIZE_COST` / `EDIT_MODEL` / `COLORIZE_MODEL` / `WELCOME_POINTS` 會**覆寫**設定檔，重啟即生效。代價是之後改設定檔會看不出效果——臨時調完記得把變數移除。`python3 -m src.core.settings` 印的是套用覆寫後的實際值。

## 測試

```bash
pip install -r requirements-dev.txt
pytest
```

整套測試完全離線：資料庫、LINE、Replicate、Supabase 全以 fake 取代（見 `test/conftest.py`），不需要 `.env`、不會連外。push 與 PR 會由 GitHub Actions 在 Python 3.9 / 3.12 上跑一次。

`test/test_local.py` 是對著執行中的伺服器發 HTTP 的手動煙霧測試，不在自動化套件內。

## 新增功能

繼承 `src/features/base_feature.py` 的 `BaseFeature`，實作 `name` / `can_handle` 與訊息處理方法，再於 `src/app.py` 初始化區以 `feature_registry.register(MyFeature(ctx))` 註冊即可。

功能透過建構子拿到的 `FeatureContext`（`src/features/context.py`）取得所有協作對象：

| 欄位 | 用途 |
|---|---|
| `line` | LINE 收訊（下載圖片、查名稱、載入動畫） |
| `publisher` | 發送訊息（reply / push） |
| `state_manager` | 對話狀態 |
| `billing` | 需要扣點的背景工作 |
| `replicate` | 呼叫 AI 模型 |
| `member_service` / `storage_service` | 會員與圖片暫存（可能為 `None`，需自行判斷） |

要讓功能用到新的服務時，加一個欄位在 `FeatureContext`、在 `app.py` 組裝時填入即可，不必動其他功能的建構子。

**要收費的功能**不必自己寫金流——把工作交給 `self.billing.submit(...)`，扣點、失敗退點、執行緒池滿載降級都由 `BillingService` 處理。這條路徑與 Replicate 無關，接任何外部服務都適用。

> 註冊順序即路由優先序。`PhotoIntentFeature` 是圖片的 catch-all，必須維持在最後註冊（`test/test_routing.py` 有把關）。
