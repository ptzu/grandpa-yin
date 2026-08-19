# 開發日誌

> 依時間軸記錄重要架構決策與里程碑。細節以 git 歷史為準，此處記「為什麼」。
> 相關文件：[部署](./DEPLOYMENT.md)、[測試環境](./TEST_ENVIRONMENT.md)、[系統健檢](./HEALTH_CHECK.md)

---

## 2026-08-19 — 成品之後的下一步（follow-up）

**背景**：成品推出去、狀態就清掉，對話到此結束。用戶想把剛上好色的全家福做成
影片，得從頭再傳一次照片——而「從頭再來」正是長輩最容易在半路放棄的地方。實務
上這也是最順的加購路徑：彩色化剛做完的那一刻，人最想看它動起來。

**做法**：新增 `FollowUpFeature`。推送成品時另附一則帶 Quick Reply 的訊息
（「做成影片」／「再修一下」／「不用了」，點數直接寫在按鈕上），並記下成品網址；
用戶按下去就把成品抓回來當新輸入，交棒給對應功能——跟 `photo_intent` 交棒給
colorize / edit 是同一套 `accept_handoff` 機制，沒有新造路徑。

**兩個關鍵取捨**：

- **只記 `result_url`，不記 `image_key`**。成品放在 `results/` 要留 30 天，而
  state 裡的 `image_key` 會在狀態轉換時被 `_discard_superseded_image` 自動回收
  ——成品寫進那個欄位，用戶按下一步的瞬間就會把自己的成品刪掉。交棒時是「把成品
  複製一份成新的暫存圖」，原件不動。`TestResultSurvives` 兩個測試釘住這件事
  （以 mutation 驗證過會咬合）。
- **`can_handle` 只認自己的按鈕文字**。follow-up 狀態沒有明確的結束動作，會留在
  對話裡；若比照其他功能「狀態屬於我就全收」，會把用戶接下來想對別的功能講的話
  一併吃掉。認不得的文字一律放行，讓路由繼續往下找。

**不提供 follow-up 的三種情況**：成品是影片（餵不回圖片模型）、沒設定成品保存
（待會取不回成品）、群組聊天（follow-up 狀態掛在個人身上，群組裡誰接著講話都會
踩到它）。推送失敗而退點時也不留狀態。

**未做**：「再做一次」。它要的是**原始輸入圖**，而原圖在處理開始時就已刪除，得
另外拉長它的生命週期；那與「不滿意免費重做一次」是同一個題目，留待一起做。

---

## 2026-08-10 — 擴充性重整：測試安全網、依賴注入、金流下放

**背景**：架構健檢一直問「會不會壞」，這次改問「加功能會不會痛」。用「假設要加 X」實測，結論很不對稱——**沿著既有那條路（再一個 Replicate 圖片功能）非常順，跨出去就撞牆**：

- 非 Replicate 的付費功能：金流編排（扣點 → 執行 → 失敗退點 → 滿載降級）寫在 `ReplicateImageFeature` 裡，等於只有「Replicate 圖片功能的子類別」拿得到，其他只能整段複製。
- 功能需要新依賴：建構子吃五個位置參數，加一個要改 `BaseFeature` + 每個子類別 + `app.py` 五處註冊，共七處散彈式修改。
- 分層宣稱 `features → services → models`，但功能層自己持有 `LineBotApi` 呼叫 `get_profile` / `get_message_content`，`replicate_feature` 還直接 `requests.post` 打 LINE 的 loading API、直接 `replicate.run()`——**HTTP 與 SDK 呼叫出現在功能層**。

**做法**（分三步，每步都先跑測試）：

1. **先補安全網**。`test_image_flow.py` 從 script 式斷言改寫成 pytest 套件（fakes 抽到 `conftest.py`），再補 `test_routing.py` 把原本只有註解在保護的路由契約寫成測試：註冊順序即優先序、`photo_intent` catch-all 必須最後、全局命令可在流程中途穿透且不破壞流程。加 GitHub Actions（3.9 + 3.12，全程離線）。**沒有這一步，後面兩步不敢動。**
2. **`FeatureContext` 取代五個位置參數**，並新增 `services/line_client.py` 收下 LINE 收訊側（`MessagePublisher` 本來就是發訊側）。加新依賴從「改七處」變成「加一個欄位 + `app.py` 一行」。
3. **金流與模型呼叫下放**到 `services/billing.py` 與 `services/replicate_client.py`，`ReplicateImageFeature` 瘦身成純委派，只留這類功能的對話面。

**副作用（正面的）**：退點路徑首度有測試。邏輯埋在 feature 裡時測不到——要偽造「Replicate 失敗」得 monkeypatch 一個方法；抽成服務 + 注入 fake client 之後，`FakeReplicateClient(fail_with=...)` 就自然涵蓋失敗退點、扣不到點不動用外部資源、池滿降級、失敗也要清狀態。

**決策取捨**：路由層的收斂（`GLOBAL_COMMANDS` 硬編清單、單一 `route(event)`、postback 支援）這次**沒做**。它會動到 `BaseFeature` 的公開介面，範圍比前三步大一個量級，而前三步已經解鎖了最貴的那個限制（非 Replicate 功能）。「每則訊息只查一次狀態」的測試先以 `xfail` 寫著，修好時拿掉標記就有回歸保護——待辦清單見[系統健檢](./HEALTH_CHECK.md)第三之二章。

**驗證**：50 項測試（含 2 項刻意 xfail）全通過；`import src.app` 走完整初始化、五個功能正常註冊。行為零改變。

---

## 2026-08-10 — 本地 standalone 測試環境名實相符

**背景**：整理 Alembic 時順手檢查本地 dev DB，發現 `alembic current` 停在 baseline，落後 head 兩版。追下去發現的不只是版本落後：

`setup_test_db.py` 用 `Base.metadata.create_all()` 建**所有** model 的表，包含 Altide 共用層 `public.accounts` / `transactions` / `linked_identities`——**即使 `DEPLOY_MODE=standalone`**。兩個後果：

1. **本地的「standalone 環境」從來沒在測 standalone 隔離**。文件宣稱「standalone 在零 Altide 表下跑完整流程通過」，但本地環境一直都有那三張表。
2. **整合 migration `a7b8c9d0e1f2` 的模式偵測會誤判**。它以「`public.accounts` 是否存在」作為「是否 platform 模式」的代理判斷；本地 standalone 庫有 `accounts`，於是一旦在該庫執行 `downgrade` 後再 `upgrade`，就會補上 `grandpa_yin.*.account_id → public.accounts.id` 的外鍵。但 standalone 模式下 `account_id` 存的是 `subjects.id`，加了外鍵之後所有寫入都會違反約束。

**做法**：

- `setup_test_db.py` 改為 **mode-aware**：`standalone` 只建 `grandpa_yin.*`，`platform` 才連 `public.*` 一起建。代理判斷因此自然成立，「零 Altide 表」的宣稱也才成真。
- 加 `_warn_if_stray_platform_tables()`：standalone 模式下若偵測到殘留的 Altide 表，印出清除指令。**只提醒不刪除**——刪表是破壞性操作。
- `OWNED_SCHEMA` 常數收進 `src/models/database.py`，`alembic/env.py` 與 `setup_test_db.py` 共用同一份定義（原本各寫各的字串）。

**本地 DB 的處置**：用 `alembic stamp head` 而非 `upgrade head`。表早就被 `create_all` 建好了，跑 `upgrade` 會撞 `CREATE TABLE ... already exists`；`stamp` 只更新版本記錄。事後 `alembic check` 回報「No new upgrade operations detected」，證明資料庫結構與 model 確實一致。

**驗證**（全部用臨時資料庫，測完刪除）：standalone 建表 → 只有 5 張 `grandpa_yin.*`、零 `public.*`；platform 建表 → 8 張含 3 張 `public.*`；**真 standalone 空庫直接 `alembic upgrade head`** → 三支 migration 依序執行、誤加的 FK 數量為 0；手動塞一張 `public.accounts` 後重跑腳本 → 殘留警告正確觸發。

---

## 2026-08-10 — `.env.example` 與 Alembic 整理（對齊 jotta 的慣例）

參照隔壁 `jotta` 專案（pnpm monorepo + Prisma）的做法收斂命名與配置。jotta 的 `apps/api/` 是 `.env.example` + `src/` + `scripts/` + `prisma/`（schema 與 migrations 同一個目錄），與本專案重整後的結構同構，`alembic/` 對應的正是 `prisma/` 的位置——所以目錄位置不動，只整理內容。

**`env_example.txt` → `.env.example`**：與 `.env` 相鄰排序、一眼看出是範本；`.gitignore` 的 `.env` 只精確匹配該檔名，不會誤傷。連帶更新 5 處引用（README、測試環境文件、`start_local_server.sh`、`test/test_local.py` ×2）。

**Alembic**：

- **migration 檔名改為 `<時間戳>_<描述>.py`**——`alembic.ini` 加 `file_template`，對齊 Prisma 的 `20260703063749_init` 形式，`versions/` 目錄天然依時序排列。現有三支一併回填時間戳：baseline 取自檔內 `Create Date`，另兩支取自 git 首次 commit 時間（同一個 commit，秒數差 1 以保留 `down_revision` 順序）。**改檔名不影響行為**——Alembic 認的是檔案裡的 `revision`，不是檔名；三個 revision id 完全未變，文件中的引用也不受影響。
- **`alembic.ini` 從 146 行精簡到 60 行**——原本九成是 `alembic init` 產生、本專案沒有啟用的選項註解（post_write_hooks、version_locations、sourceless…）。只留實際生效的設定，並替每一項寫上為什麼這樣設。
- **`alembic/README` 換掉**——原本只有 stock 的一行 "Generic single-database configuration."。改寫為 `README.md`：檔案職責、命名慣例、以 revision id 反查檔案的方法、常用指令，以及三項容易踩到的注意事項（autogenerate 偵測不到欄位改名／本地缺 RLS 與 trigger／新 migration 碰到 `public.*` 時要寫成 mode-aware）。

**驗證**：`alembic history --verbose` 正確讀出新檔名與完整鏈；離線模式 `alembic upgrade a6e5ccf71d56:head --sql` 產得出正確 DDL（不連線、不改資料庫）；實際跑一次 `alembic revision` 確認 `file_template` 產出 `20260810110606_naming_convention_probe.py`（探測檔已刪除）。

**順帶發現**：本地 dev DB 的 `alembic current` 停在 baseline `a6e5ccf71d56`，落後 head 兩版（缺 `subjects` / `wallet_transactions`）。這是既有狀態、與本次改動無關，本地要跑 standalone 模式前需補 `alembic upgrade head`。

---

## 2026-08-10 — 本地啟動器改寫為 shell script

`start_local_server.py`（300+ 行）→ `start_local_server.sh`。它做的四件事——檢查依賴、起 Flask、起 ngrok、呼叫 LINE API 設定 webhook——本來就是流程編排，用 shell 表達更直接，也不再需要為了啟動 Python 而先跑一個 Python。

**順帶修掉原版的問題**：

- **固定 `sleep` 改成輪詢**——原版 `sleep(3)` 等 Flask、`sleep(5)` 等 ngrok，機器慢就誤判失敗。現在輪詢到 `/webhook` 回 405（只收 POST，405 即代表就緒）為止。
- **收拾子行程**——Flask debug 模式會 fork 出 reloader 子行程，原版只 `terminate()` 父行程，留下孤兒佔著 port。現在 `pkill -P` 連子行程一起收。
- **`PORT` 實際生效**——原版讀了 `PORT` 卻在探測與開隧道時寫死 5000，`PORT` 一改就壞。
- **移除死碼**——`run_test()` 從來沒有被 `start()` 呼叫過。
- **新增 port 佔用預檢**——macOS 的「隔空播放接收器」預設佔用 5000，是最常見的啟動失敗原因，現在會直接指出佔用者。

**踩到的三個坑（都已修並驗證）**：

1. **`.env` 的行內註解**——`LOG_LEVEL=DEBUG    # 測試時看詳細 log` 整串被當成值，Flask 一啟動就 `ValueError: Unknown level`。python-dotenv 會砍掉「空白 + `#`」之後的內容，我第一版漏了。規則有分支：引號內的 `#` 要保留、`abc#def` 這種沒有前導空白的也不算註解。**這個坑是實跑才發現的**——前一輪只測了自己想得到的案例，沒拿真實 `.env` 對照，教訓是解析器類的東西一定要用真實輸入覆核。
2. **Python 3.9 的 f-string 表達式不能含跳脫引號**——內嵌的 `python3 -c` 用 `f"{result.get(\"statusCode\")}"` 會直接 SyntaxError。改用 `%` 格式化。
3. **bash 3.2 會把全形字元吃進變數名**——macOS 內建 bash 3.2，在非 UTF-8 locale 下 `"$missing（請檢查）"` 會被解讀成變數 `missing\xef…` 而報 unbound variable。凡是變數後面直接接中文，一律加大括號 `${missing}`。這條路徑正是新手最先踩到的「缺環境變數」錯誤訊息。

**`.env` 讀取用逐行解析而非 `source`**：`source` 會把 `.env` 當 shell 執行（`INJECT=$(echo pwned)` 真的會跑），且會覆蓋呼叫端已設定的環境變數。自訂的 `load_dotenv` 比照 python-dotenv 語意——**既有環境變數優先**、值不做命令替換、去除成對引號、砍掉行內註解。

**驗證**：以 bash 3.2 做語法檢查；`.env` 解析對照 export/引號/空值/行內註解/含特殊字元的 URL/注入嘗試/`KEY = value` 等案例，並以**真實 `.env`** 覆核；四條 guard 路徑（缺 .env、缺變數、port 佔用、Flask 起不來）皆實測正確且會清理行程。設定 webhook 之後的段落未實跑——它會真的改動測試 channel 的設定。

---

## 2026-08-10 — 專案結構重整（核心程式碼收進 `src/` 套件）

**背景**：根目錄同時躺著 `app.py`、`app_logger.py`、`error_tracking.py`、`message_publisher.py`、`task_executor.py`、`user_state_manager.py` 與 `features/` `models/` `services/` 三個套件，加上設定檔、腳本、文件目錄，看不出哪些是核心程式碼、哪些是周邊工具，也沒有可依循的依賴方向。

**做法**：

- 核心程式碼全部移進 `src/` 套件。取 `src` 而非 `app`，是因為 `app` 會三重撞名：`app/app.py`、`gunicorn app.app:app`、Flask 實例本身也叫 `app`。
- 根目錄散落的模組依職責歸位，形成四層：
  - `core/` — `app_logger` / `error_tracking` / `task_executor`：跨切面基礎設施，不含任何領域知識。
  - `models/` — 資料層（不動）。
  - `services/` — 併入 `message_publisher`（LINE 發送）與 `user_state_manager`（對話狀態）。它們本來就是「對外部系統／領域狀態的封裝」，與 `member_service` / `storage_service` / `account_backend` 同一層，散在根目錄只是歷史遺留。
  - `features/` — 功能模組（不動）。
- 依賴方向定為單向：`features → services → models`，三者皆可依賴 `core`，反向不允許。
- 啟動器從 `test/start_local_server.py` 移到根目錄並改寫為 shell script `start_local_server.sh`——它是開發入口，不是測試案例，放在 `test/` 底下還要 `cd test` 才跑得動。詳見下方同日條目。
- 連帶更新：`Procfile`（`gunicorn src.app:app`）、`alembic/env.py`、`scripts/*`、`test/*`、全部文件的路徑引用。

**決策取捨**：`scripts/` 與 `test/` 留在根目錄不併入套件——它們是工具而非產品程式碼，且都以獨立腳本方式執行。套件內一律用絕對匯入（`src.*`），只有 `features/` 內部彼此引用維持相對匯入。

**驗證**：全部檔案以 `git mv` 搬移，git 判定為 rename、歷史完整保留。`gunicorn` 進入點、四支管理腳本、`alembic/env.py` 匯入皆實測通過；離線測試 72 項全數維持通過。

---

## 2026-08-10 — 圖片流程對長輩友善化（先傳圖 → 選功能）

**背景**：評估「要不要做 LIFF 讓上傳更友善」，結論是**上傳不是痛點**——長輩在 LINE 傳照片已經很熟練，LIFF 的 `<input type=file>` 反而多一層。真正的痛點在對話流程本身：

1. 沒先打功能指令就上傳的照片被**靜默丟棄**（`edit_feature` / `colorize_feature` 的 `handle_image` 直接 return），用戶零回饋。
2. 編輯描述**必須打字**，對客群是最大門檻。
3. 描述送出**立刻扣點**，打錯字也照扣，不滿意只能整套重來。

**做法**（都在既有的 Feature Registry + Quick Reply 機制內，不引入前端）：

- **新增 `PhotoIntentFeature`**：圖片路由的 catch-all，接住沒人處理的照片 → 暫存 → Quick Reply 問「上色／修改／取消」→ 用 `accept_handoff()` 把 stash 交棒給真正的功能（不重新上傳）。必須最後註冊。群組聊天不主動搭話。
- **`route_image_message` 改用 `can_handle_image()` 判斷**：原本只要 state 有 feature 就無條件 dispatch，狀態對不上時會被該 feature 靜默吃掉；現在接不住就往下走 fallback。
- **編輯描述改 Quick Reply**：五個預設效果 + 「我自己描述」+「取消」，全程免打字。
- **新增 `waiting_confirm` 階段**：`waiting_image → waiting_description → waiting_confirm → processing`。確認前不扣點，可取消、可換圖、可重新描述（照片保留）。
- **`_is_other_trigger_command()`**：卡在某功能流程中途時輸入別的功能指令會正確切換，不再被當成編輯描述吃掉。
- **`download_image` / `stash_image` 系列上移到 `BaseFeature`**：圖片下載與暫存是通用建材，非 Replicate 專屬。

**決策取捨**：不做 LIFF。除了上傳不是痛點外，改用 LIFF 直傳會**失去 LINE 代做的前處理**（HEIC→JPEG、EXIF 旋轉、壓縮），且前端不能持有 service role key、需另開 LINE Login channel、多一條這個 repo 目前沒有的前端部署管線。LIFF 留給「上傳＋預覽＋before/after＋微調重跑」那類聊天介面真的做不到的場景。

**驗證**：新增 `test/test_image_flow.py`——零外部依賴的狀態機測試（fake 掉 DB／LINE／Storage／Replicate），涵蓋完整路徑與取消／換圖／重新描述／點數不足／群組靜默／中途切功能，51 項全通過。這是本 repo 第一支可離線執行、可直接接 CI 的測試。

**接續處理**：暫存圖的孤兒物件清理，見下方同日條目。

---

## 2026-08-10 — Storage 暫存圖清理（孤兒物件）

**背景**：檢討圖片流程時發現，孤兒暫存圖的大宗不是「流程中途切換功能」，而是**用戶傳了照片就不再回覆**——`cleanup_old_states` 24 小時後刪掉 session row，卻不管它引用的圖。這在本次改動之前就一直在發生，`cleanup_user_states.py` 本身就是製造者。Supabase Storage **沒有** S3 那種 lifecycle policy 可設，只能自己掃。

**做法**：兩層。

- **層 1（即時）**——`BaseFeature._discard_superseded_image()`：`set_state` / `clear_state` 改為回傳被覆蓋／被清掉的 state data，`set_user_state` / `clear_user_state` 據此刪掉不再被引用的 `image_key`（同一張圖延用到下個狀態時不刪）。收攏在狀態轉換這一層，而不是讓 feature 互相清理對方的狀態——所有路徑都會經過這兩個入口，沒有漏網。
- **層 2（兜底）**——`scripts/cleanup_storage.py`：刪「超過 `--hours`（預設 24）**且** 沒有任何存活 `bot_session` 引用」的物件。兩個條件並存讓它與 `cleanup_user_states.py` 的執行順序無關，也不會誤刪流程中用戶的照片。預設試跑，要加 `--apply` 才真的刪。掛 Railway cron 每日執行（需另建 cron service，設定見部署文件）。
- `StorageService` 補上 `list_folders` / `list_objects`（自動翻頁）與 `delete_images` 批次刪除；`delete_image` 改為冪等——404 視為已達成目標，不再記 warning。

**驗證**：測試從 51 項擴充到 72 項。新增「各種中斷路徑都不留孤兒圖」——走完流程／三種階段取消／連傳三張／換圖／重新描述／中途切功能，八條路徑跑完都斷言 Storage 為空；以及 `parse_timestamp` 對 Supabase 超微秒精度時間戳的解析（Python 3.9 的 `fromisoformat` 吃不下，會靜默讓物件永遠不被清理）。

**尚未完成**：Railway cron service 要在 dashboard 手動建立，程式碼這邊已就緒。

---

## 2026-08-09 — Phase 5：Alembic migration 拆分（解耦後的正規部署）

把 migration 補到與解耦後的 model 一致，讓兩種模式都能走 `alembic upgrade head`：

- **改寫 baseline `a6e5ccf71d56`**：三張產品表拿掉跨 schema 外鍵，`account_id` 為純 UUID（線上是 stamp 過的、body 未執行，改它安全）。
- **`f1a2b3c4d5e6` add standalone identity and wallet**：把 `subjects` / `wallet_transactions` 納入 Alembic，standalone 不再只能靠 `create_all`。
- **`a7b8c9d0e1f2` platform integration account fk**：冪等且 mode-aware——`public.accounts` 不存在（standalone）就跳過；已有 FK（schema.sql 建的）也偵測後跳過；否則補上 `grandpa_yin.* → public.accounts` 外鍵。單一線性鏈在兩種模式都安全。

**驗證**：standalone 空庫 upgrade → 5 表、0 FK、整合層跳過；platform（有 accounts）→ 補 3 FK；模擬 schema.sql 既有 FK → 偵測跳過不重複；`alembic check` 回報 model↔migration 無 diff。

---

## 2026-08-09 — grandpa_yin 與 Altide 解耦（standalone / platform 雙模式）

**背景**：`grandpa_yin.*` 產品表對 Altide 的 `public.accounts` 有跨 schema 外鍵，且業務邏輯直接讀寫 `accounts.points_balance` / `transactions` / `linked_identities`——導致本產品**無法脫離 Altide 獨立建置、測試、demo**。

**做法**：引入 **Ports & Adapters（六角架構）**。

- 新增 `AccountBackend` port（`services/account_backend.py`），把「身份 + 點數 + 交易帳」抽象化。
- 兩個 adapter，由 `DEPLOY_MODE` 選用：
  - `PlatformAccountBackend` — 走 Altide `public.*`（維持既有線上行為）。
  - `StandaloneAccountBackend` — 走本產品自有的 `grandpa_yin.subjects` / `wallet_transactions`。
- `member_service` / `user_state_manager` 改成只認 port，移除各自的 `_resolve_account`。
- 新增 `Subject` / `WalletTransaction` model；移除三張產品表對 `accounts.id` 的 ORM 跨 schema 外鍵，`account_id` 改為純 UUID 邏輯參照。

**決策取捨**：
- standalone 自帶簡易 wallet（而非「無點數」），保留完整扣點語意。
- 整合時的資料庫層外鍵，留待「整合 migration」在 platform 環境補回（換取獨立性 vs 強制完整性的平衡）。
- `DEPLOY_MODE` 預設 `platform`，確保線上行為零變動。

**驗證**：standalone 在**零 Altide 表**下跑完整流程通過；platform（建齊所有表）回歸通過。

**下一階段**：Alembic migration 拆分——已於同日完成，見上方 Phase 5。

---

## 2026-08-09 — 導入 Alembic 管理 grandpa_yin schema

- 新增 `alembic/`（`env.py` 範圍限定 `grandpa_yin.*`，`public.*` 交給 Altide 的 `schema.sql`）+ baseline migration。
- `railway.json` 的 `preDeployCommand` 在部署前自動 `alembic upgrade head`，失敗即中止部署。
- `setup_test_db.py` 建表後 `stamp` 到 head，讓本地 DB 與 migration 一致。
- 兩層 schema 各自管理的邊界正式定型（見[部署文件第六章](./DEPLOYMENT.md#六資料庫-schema-與自動-migration)）。

---

## 2026-07-21 — 一鍵本地測試環境 + webhook 簽章防護

- `test/setup_test_db.py` + `start_local_server.py`：本機 Postgres 一鍵建表、自動起 Flask + ngrok + 設定 LINE webhook。
- webhook 加上簽章驗證保護。
- 產出[測試環境文件](./TEST_ENVIRONMENT.md)。

---

## 2026-07-07 ~ 07-08 — 架構健檢與五大高危修復

一輪完整健檢（見[系統健檢報告](./HEALTH_CHECK.md)）後，依優先序修掉：

1. **堵金流洞**：改「先扣點 → 處理 → 失敗退點」；webhook 加 `webhookEventId` 去重；DB 掛掉改為**拒絕服務**而非免費放送。
2. **修 `route_image_message` 雙重執行**：新增 `can_handle_image()`，判斷與執行分離。
3. **降低每則訊息外部呼叫**：刪 `_is_valid_user` 的 `get_profile`、`get_user_name` 改讀 DB、`Procfile` 改 `-w 2 --threads 8`。
4. **韌性基本盤**：外部呼叫加 timeout、裸 Thread 換有界 `ThreadPoolExecutor`、push 重試（指數退避）、滿載優雅降級。
5. **可觀測性**：`print` 全面換成分級 + request-scoped logger（`app_logger`）；接 Sentry（`error_tracking`）帶 request/user context；失敗路徑寫 `usage_logs(status='failed')`。

其他：`ReplicateImageFeature` 基底類別抽取去重、註冊機制改注入 `state_manager`、註冊獎勵改為冪等、圖片改存 Supabase Storage（不再 base64 塞 JSONB）、新增 `scripts/trace_user.py` 診斷工具、產出事故手冊（已併入部署文件營運章節）。

---

## 2026-04-17 — 統一帳號 schema

- 遷移到 `accounts` / `linked_identities` 的統一帳號設計，與 Altide web 端共用同一套帳號與點數（platform 模式的基礎）。

---

## 2025-10 ~ 2026-01 — 早期建置

- LINE Bot 群組支援（回覆正確回到群組而非私訊）。
- 導入 PostgreSQL 會員系統（點數、交易記錄、會員狀態）。
- 修一系列 SQLAlchemy `DetachedInstanceError`、`MessagePublisher` 回覆方法、feature 的 member dict 存取。
- 修 re-follow 重複發放歡迎點數的漏洞。
- Railway 部署 + `Procfile` 詳細 log 參數。
