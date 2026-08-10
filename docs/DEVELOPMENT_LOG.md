# 開發日誌

> 依時間軸記錄重要架構決策與里程碑。細節以 git 歷史為準，此處記「為什麼」。
> 相關文件：[部署](./DEPLOYMENT.md)、[測試環境](./TEST_ENVIRONMENT.md)、[系統健檢](./HEALTH_CHECK.md)

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
