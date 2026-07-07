# 銀爺爺 LINE Bot — 架構 Q&A 筆記

> 日期：2026-07-08
> 背景：完成 5 個高危險問題修復（見 `CODE_HEALTH_CHECK.md`）後的問答整理

---

## Q1. 這次修改有需要在後台設定的嗎？

幾乎不用，大部分改動都有預設值、push 後自動生效。

### 必須確認：Railway 的啟動指令

`Procfile` 已改為：

```
web: gunicorn app:app -w 2 --threads 8 --timeout 60 ...
```

Railway 規則：**如果曾在後台 Settings → Deploy → Custom Start Command 手動填過啟動指令，它會覆蓋 Procfile**。

- Start Command 是空的 → 不用動，自動用新的 Procfile
- 有填舊指令 → 清空它，或更新成和新 Procfile 一致

驗證方式：部署後看 log，gunicorn 印出 `Booting worker with pid ...` **兩次**（兩個 worker）即生效。

### 可選設定（不設也能正常運作）

| 變數 | 用途 |
|---|---|
| `SENTRY_DSN` | 啟用 Sentry 錯誤追蹤（到 sentry.io 開免費帳號取得 DSN） |
| `LOG_LEVEL` | 預設 `INFO`；上線初期可暫設 `DEBUG` 看詳細路由 log |
| `IMAGE_WORKERS` / `IMAGE_QUEUE_LIMIT` | 圖片處理併發上限，預設 4 / 8 |

### 完全不用動

- Supabase：無 schema 變更（`usage_logs.status='failed'` 是既有欄位的合法值）
- LINE Developers Console：webhook URL、channel 設定不變
- `sentry-sdk`：Railway 部署時自動從 requirements.txt 安裝

---

## Q2. 未來要擴充 Replicate 功能，現有架構好擴充嗎？

**骨架好擴充，但肉還沒抽出來。**

- **好的部分**：Registry + BaseFeature 插件設計沒問題——新功能繼承 `BaseFeature`、在 `app.py` 註冊一行即可，路由、狀態管理、扣點服務都是現成的。
- **不好的部分**：`ColorizeFeature` 和 `EditFeature` 約七八成重複——`_start_loading_animation` 兩份、`_is_global_command` 三份、「扣點 → 呼叫 Replicate → 失敗退點 → 推送結果」流程各寫一次、Replicate 回傳值解析也重複。現在加第三個功能等於複製 300 行改模型名稱，改一個 bug 要改三處。

**建議的前置作業**：抽一層中間類別 `ReplicateImageFeature(BaseFeature)`，收進載入動畫、執行緒池提交、扣點/退點流程、output URL 解析；子類別只定義「功能名稱、模型 ID、點數費用、input 參數、文案」。做完後每個新功能約 30~50 行。

---

## Q3. Sentry 是什麼？和 print / log 差在哪？

Sentry 是**錯誤追蹤服務**（SaaS，有免費方案）。定位與 log 互補：

| | print / log | Sentry |
|---|---|---|
| 性質 | 流水帳，所有事件依時間排列 | 只收集「錯誤事件」，自動整理成儀表板 |
| 出錯時 | 要**主動**去 Railway 翻 log | **主動通知你**（email），客訴前就知道出事 |
| 重複錯誤 | 同一 bug 發生 500 次 = 500 行 log | 歸併成 1 個 issue，顯示次數、影響用戶數、起始版本 |
| 上下文 | log 裡寫了什麼才有什麼 | 自動附完整 stack trace、request 內容、環境資訊 |
| 保存 | Railway log 有保留期限 | issue 保留到標記 resolved |

一句話：**log 查「事情經過」，Sentry 知道「出事了」**。對長輩用戶產品特別有價值——他們遇錯通常不回報，只會默默不用。

目前整合方式：設定 `SENTRY_DSN` 環境變數即啟用；`logger.exception` 的錯誤會同時進 log（帶 request_id）和 Sentry。

---

## Q4. 扣點會因網路問題失敗嗎？用戶能竄改點數嗎？

### 扣點失敗：會，但現在是 fail-closed（失敗就不服務），不會虧錢

- DB 斷線時 `deduct_points` → exception → 回傳 False → **不處理圖片**，用戶收到「扣點失敗，本次未處理」。用戶沒損失點數，也沒消耗 Replicate 費用。
- 剩餘的小風險窗口（機率低，有完整稽核記錄可人工補償）：
  1. 扣點成功 → Replicate 失敗 → **退點時 DB 又剛好掛**：點數被扣未退；`transactions` 有扣款記錄、log/Sentry 有退點失敗記錄，可人工補退
  2. 扣點成功 → 處理完成 → **推送結果重試 3 次仍失敗**：被扣點沒收到圖，同樣可查記錄人工處理
  3. **處理中 Railway 重啟／部署**：背景執行緒消失，已扣點沒出圖；徹底解決需 job 表＋重啟續跑，目前規模先靠稽核記錄人工補償

### 竄改點數：基本上不能

- 點數只存 Supabase，用戶唯一輸入管道是 LINE 訊息文字；SQLAlchemy 全程參數綁定，無 injection 空間
- LINE 簽名驗證（`X-Line-Signature`）涵蓋整個 request body，沒有 Channel Secret 偽造不了 userId
- 併發重複消費被 `SELECT ... FOR UPDATE` row lock 擋住，餘額不會扣成負數
- **唯一剩餘缺口**（健檢 🟡9）：快速「封鎖→解除封鎖」在極短時間內觸發兩個 follow event，有機會領兩次歡迎點數（50 點）。低風險；若調高 `WELCOME_POINTS` 應先修
- 真正能改點數的是**拿到 `DATABASE_URL` 的人**（直連繞過 RLS）——Railway 環境變數與 Supabase 密碼才是真正防線，`.env` 絕不可進 git

---

## Q5. 改成 LINE LIFF 和現有聊天式相比，體驗差異？

**結論：對長輩來說，聊天式操作是優點不是限制；LIFF 適合補聊天做不好的事，不是取代聊天。**

| 面向 | 純聊天 Bot（現況） | LIFF（LINE 內網頁） |
|---|---|---|
| 學習成本 | 幾乎零——和傳照片給家人同一動作 | 要學新介面 |
| 操作方式 | 打字／Quick Reply，一次一步 | 完整 GUI：表單、預覽、相簿 |
| 功能發現 | 弱——要記指令，Quick Reply 滑掉就不見 | 強——功能攤在畫面上 |
| 看歷史/點數 | 純文字列表，多筆難讀 | 可做報表、卡片、圖表 |
| 圖片編輯 | 要打字描述效果（長輩痛點） | 可做成點選預設效果按鈕 |
| 成果呈現 | 圖片散在對話裡 | 可做「作品集」相簿 |
| **金流／儲值** | 做不了，只能貼外部連結 | **最大優勢**——LINE Pay／信用卡在 LINE 內完成 |
| 失敗模式 | 打錯字 Bot 不理 | 載入慢、誤觸關閉、舊手機跑不動 |

### 建議：混合式

1. **留在聊天**：彩色化上傳、結果推送、簡單點數查詢
2. **交給 LIFF**：儲值付款頁（接金流幾乎必須）、圖片編輯效果選擇頁、交易記錄／作品集
3. **入口用 Rich Menu**（聊天室底部常駐大圖選單）連到 LIFF——順便解決「功能發現靠打指令」問題，LINE 後台可設不用寫程式

### 架構已就緒

`accounts` + `linked_identities` 就是為多入口設計的——LIFF 拿到的 LINE user ID 與 webhook 的 userId 相同，直接對到同一 account 與點數錢包；`accounts.auth_user_id` 也預留了 Supabase Auth 的位置。只需加一層給 LIFF 的 API（**必須驗 LIFF ID token**，不能只信前端傳的 userId）。

第一個 LIFF 頁面建議做「儲值付款」：商業價值最高、又是聊天完全做不到的事。

---

## Q6. 100 人同時傳訊息，能同時處理嗎？

**能，不是一個一個排隊——但文字和圖片答案不同。**

### 目前的三條併發通道

```
① Webhook 接收：2 workers × 8 threads = 同時 16 個請求
② DB 連線池：每 worker 5+10 = 15 條
③ 圖片處理池：全機 8 張同時處理 + 16 張排隊（IMAGE_WORKERS=4 × 2）
```

### 100 人同時傳文字（查點數、開選單）

沒問題。每則約 100~300ms，16 個一批：**2~3 秒內全部回完**。（修復前 1 個 sync worker 才是真排隊：約 50 秒，後面的人等到 LINE 逾時重送。）

### 100 人同時傳圖片

- 「已收到照片」的確認回覆：10~15 秒內全部發出
- AI 處理：8 張同時＋16 張排隊，**第 25 位之後收到「目前使用人數較多，請稍後再試」**——這是刻意的保險絲設計，防止 100 條執行緒耗盡記憶體與 DB 連線拖垮整台
- 排隊的圖每張 10~30 秒，一兩分鐘內消化完

### 流量再大時的調整順序

1. 調環境變數：`IMAGE_WORKERS=8`、`IMAGE_QUEUE_LIMIT=20`（同時確認 Railway 記憶體）
2. Railway 多 instance——注意 webhook 去重是記憶體內的，多 instance 需改 DB/Redis 去重
3. 終極解：圖片處理拆獨立 worker＋佇列（Redis + RQ），webhook 只收單

---

## Q7. IMAGE_WORKERS 最高能調多少？極限服務量？

### 四道牆（依序會撞到）

| 瓶頸 | 目前的量 | 說明 |
|---|---|---|
| DB 連線池 | 每 worker 15 條 | `IMAGE_WORKERS` 超過 ~10 應同步調大 pool |
| Supabase 連線上限 | Free/Micro 直連約 60 條 | 目前 2×15=30 有餘裕；pool 調大或多 instance 時改用 pooler（port 6543） |
| Railway 記憶體 | 依方案 | 每執行緒抓一張圖（1~10MB）；512MB 約撐 15~20 個併發任務 |
| Replicate 帳號限制 | 依帳號等級 | 衝太高收 429（會走退點路徑，用戶無損失但體驗差） |

### 實際建議

- 舒服上限：`IMAGE_WORKERS=8~10`（全機 16~20 張併發），同時 `pool_size` 調到 10
- 換算吞吐量（每張 15~30 秒）：併發 16 張 → **每分鐘 30~60 張圖**，一小時 2000+ 張
- 1000 註冊用戶、尖峰數十人在線的目標**完全在能力範圍內**（用戶不會同一秒都傳圖）
- 若「一分鐘湧入 100+ 張圖」是常態（行銷活動），才需要換佇列架構
- 文字訊息容量與 `IMAGE_WORKERS` 無關，足以應付數百人同時操作

---

## Q8. 改資料庫欄位，push 程式碼後 Supabase 會自動更新嗎？

**不會，完全不會。**

- 本 repo 的 `models/*.py` 只是 SQLAlchemy 的「對照表」，不會建立或修改資料表（沒有 `create_all()`、沒有 Alembic）
- 真正的 schema 由**另一個 repo**（`altide-landing-page/supabase/migrations/`）的 migration 管理

### 正確流程（有順序）

```
加欄位（安全方向）：
  ① 先在 altide-landing-page 寫 migration → supabase db push（或後台 SQL Editor）
  ② 再改本 repo 的 model → push 部署
  （順序反了會炸：程式引用 DB 還沒有的欄位 → column does not exist）

刪欄位（危險方向）：
  ① 先改程式，移除所有用到該欄位的程式碼 → 部署
  ② 確認正常後，才在 DB 執行 DROP COLUMN
```

### 現有資料的影響

| 改動 | 影響 |
|---|---|
| 新增欄位（nullable 或有 default） | ✅ 零影響，瞬間完成，舊資料自動 NULL/預設值 |
| 新增資料表、加索引 | ✅ 零影響（大表用 `CREATE INDEX CONCURRENTLY` 避免鎖表） |
| 改欄位型別 | ⚠️ 可能重寫整表、鎖表；資料可能轉換失敗 |
| 改名／刪欄位 | 🔴 不可逆，舊程式碼還在跑的瞬間全面報錯 |

### 兩個保命習慣

1. 動 schema 前確認 Supabase 備份狀態（免費方案僅每日備份，Pro 才有 PITR）
2. 盡量只做「加法」——要「改」欄位時，寧可加新欄位→搬資料→之後再刪舊的，每步可回退
