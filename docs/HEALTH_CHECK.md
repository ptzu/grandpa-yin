# 系統健檢報告

> 最近更新：2026-08-09
> 範圍：src/（app.py、core/、features/、services/、models/）、Procfile、alembic/
> 部署環境：Railway（Flask + gunicorn `-w 2 --threads 8`）、Supabase（PostgreSQL + Storage）
> 前次健檢：2026-07-07（發現 5 大高危 + 8 項中低），修復進度見下方。

---

## 一、總體評價

架構為「Flask + Railway 單機 + Supabase」的插件式（Feature Registry + BaseFeature）設計，方向正確、擴充成本低。經 2026-07 一輪修復後，**先前的金流漏洞與效能瓶頸已堵住**；2026-08 的解耦讓產品可獨立於 Altide 運作。

**現況結論**：完成 2026-07 修復後，此架構支撐 1000 名註冊用戶、數十人同時在線沒有問題。要再往上（同時 100+ 張圖處理）才需把圖片處理拆成獨立 worker + 佇列。

**強項**
- 資料層全程 SQLAlchemy 參數綁定，無 SQL Injection；`deduct_points` 用 `with_for_update()` row lock、`balance_after` 留帳。
- 機密全走環境變數，`.env` 已 gitignore，repo 無洩漏金鑰。
- 分級 logging（帶 request_id）+ Sentry；失敗路徑寫 `usage_logs(status='failed')`，可觀測性到位。
- schema 分層（`public.*` / `grandpa_yin.*`）+ Alembic 自動 migration，邊界清楚。
- **Ports & Adapters 解耦**：`AccountBackend` 讓業務邏輯不綁定 Altide，可 standalone / platform 雙模式。

---

## 二、前次高危問題修復進度

| # | 問題 | 狀態 | 備註 |
|---|---|---|---|
| 1 | 先服務後扣點 + 無冪等 → 金流漏洞 | ✅ 已修 | 改「先扣點→處理→失敗退點」；`webhookEventId` 去重；DB 掛拒絕服務 |
| 2 | `route_image_message` 雙重執行 | ✅ 已修 | 新增 `can_handle_image()`，判斷與執行分離 |
| 3 | 每則訊息多次阻塞 API + 單 sync worker | ✅ 已修 | 刪多餘 `get_profile`、`get_user_name` 讀 DB、`-w 2 --threads 8` |
| 4 | 外部呼叫無 timeout、背景執行緒無上限 | ✅ 已修 | 全加 timeout、有界 `ThreadPoolExecutor`、push 重試退避、滿載降級 |
| 5 | `print` 當日誌、失敗無軌跡 | ✅ 已修 | 分級 request-scoped logger + Sentry；失敗寫 `usage_logs` |
| 6 | raw exception 回傳終端用戶 | ✅ 已修 | 對用戶固定文案，stack trace 進 log/Sentry |
| 7 | `_is_valid_user` 測試後門在生產路徑 | ✅ 已修 | 移除，發送失敗改 try/except |
| 8 | 整張圖 base64 存 JSONB | ✅ 已修 | 改存 Supabase Storage，state 只留 key |
| 9 | follow event 歡迎點數 TOCTOU race | ✅ 已修 | 註冊獎勵改冪等（row lock + 唯一性檢查） |
| 10 | 全局命令清單複製三份、`features[0]` 偷 state | ✅ 已修 | Registry 注入 state_manager |

---

## 三、仍待補強（依優先序）

以下多為「韌性 / 營運」層級，非阻斷性，建議排入：

1. 🟠 **`/health` endpoint + 外部監控**——服務掛掉目前只能靠用戶回報。加檢查 DB 連線的 `/health` 配 UptimeRobot。
2. 🟠 **跨 process 的 webhook 去重**——去重表在**記憶體**，`-w 2` 下同一 event 重送打到另一 process 會漏擋，可能重複扣點。改用 Postgres `processed_events` 表或 Redis。（金流相關，優先）
3. 🟡 **推送失敗的自動退點**——「處理成功但 push 失敗」用戶會白扣點，目前靠人工補償。
4. 🟡 **Supabase connection pooler（port 6543）**——根治連線池上限（連線總量 = 2 processes × pool + 圖片執行緒）。
5. 🟡 **`transactions` 加 `source/created_by`**——帳目稽核時分辨寫入來源（linebot / admin / 手動 SQL）。
6. ✅ **Storage bucket 生命週期清理**（2026-08-10 完成）——兩層：狀態轉換時即時刪掉被取代的暫存圖；`scripts/cleanup_storage.py` 每日掃除「超過 24 小時且無 session 引用」的孤兒物件。Supabase 沒有原生 lifecycle policy，只能自己排程，設定見[部署文件](./DEPLOYMENT.md)的「排程維運」。**尚需在 Railway 建立 cron service 才會真正生效。**
7. 🟢 **Replicate model ID 改環境變數 + `run_replicate` 加 timeout**——換模型免部署、避免 worker 長時間吊死（`replicate.run()` 無 client-side timeout）。

---

## 四、解耦架構的健康度（2026-08 新增）

**優點**：`member_service` / `user_state_manager` 只依賴 `AccountBackend` port，換帳號來源不動業務邏輯；standalone 已驗證可在零 Altide 表下運作。

**目前限制與注意**：

1. ✅ **standalone 已接 Alembic**（Phase 5 完成）——核心 migration 無跨 schema FK、`f1a2b3c4d5e6` 建 standalone 表、`a7b8c9d0e1f2` 為 platform 冪等補 FK。standalone 與 platform 皆可 `alembic upgrade head`。`setup_test_db.py` 的 `create_all` 仍保留作為本地快速路徑。
2. ✅ **platform 的資料庫層 FK 由整合 migration 補回**（Phase 5）——`a7b8c9d0e1f2` 在 `public.accounts` 存在且 FK 未建時補上；既有環境（schema.sql 已建 FK）會被偵測跳過。
3. 🟢 **`scripts/trace_user.py` 仍直接查 Altide 表**——platform 專用診斷工具，維持原樣合理；standalone 環境下不適用。

---

## 五、「1000 人撐不撐得住」的直接回答

**撐得住。** 瓶頸不在 Supabase 或 Railway，而在先前的「單 sync worker、每訊息多次阻塞呼叫、無界執行緒」——這三項已於 2026-07 修復。目前架構支撐 1000 名註冊用戶、數十人同時在線無虞。再上一個量級（持續 100+ 併發圖片處理）時，才需要獨立圖片 worker + 佇列（Redis + RQ），並優先完成第三章第 2、4 項。
