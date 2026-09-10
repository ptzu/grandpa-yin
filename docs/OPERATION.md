# 擴展與運營文件（Operations & Scaling Guide）

> 目的：隨著使用者從「幾十人」長到「大量並發」，該**在什麼訊號出現時、做什麼調整**，才撐得住。
> 適用架構：Railway（Flask + gunicorn）＋ Supabase（PostgreSQL + Storage）＋ LINE Messaging API ＋ Replicate。
> 相關文件：[測試（含壓力測試計畫與結果）](./TEST.md)、[部署](./DEPLOYMENT.md)、[系統健檢](./HEALTH_CHECK.md)。

---

## 〇、先搞懂「瓶頸在哪」

壓測（見 [測試文件](./TEST.md) 壓-10 節）證實：**收訊/基礎設施的餘裕遠大於生成**。

| 路徑 | 實測天花板 | 是不是瓶頸 |
|---|---|---|
| 收訊 `/health`（HTTP + DB 連線） | ~765 req/s | ❌ 真實流量到不了 |
| DB 連線 | Supavisor 保護，<20/60 | ❌ 不是限制 |
| RAM | Railway 上限 48GB，用 <1% | ❌ 不是限制 |
| **圖片生成（有界 worker pool）** | **同時 8～16 張** | ✅ **先撞到的就是這個** |

> **結論：擴展這個服務 ≈ 擴展「同時能生幾張圖」。** 其他都有大量餘裕。
> 唯一擺脫不了的天花板是 **Replicate 費用**（做大 = 生成多 = 花費高，靠[定價](./PRICING.md)解決，不是技術問題）。

**衡量規模的指標**：不是「總用戶數」，而是 **生成請求的到達速率（人/分）** 與 **尖峰同時湧入人數**。

---

## 一、擴展的四個階段

每一階都對應「什麼訊號該升級、做什麼、花多少錢」。**不要跳級或過早升級**——複雜度是有代價的。

### 階段 0 / 1 — 起步 / 小社團正式上線 ⭐
**規模**：偶發到偶爾幾十人在幾分鐘內湧入。
**架構**：repo 出廠預設即為上線甜蜜點。
```
config/settings.yml → worker_pool: max_workers 8 / queue_limit 16
  （全站 -w 2：同時生 16 張、容量 48、~24 人/分可持續）
Railway Hobby ($5) · Supabase Nano→Pro · gunicorn -w 2 --threads 8
```
**動作**：
- 生成池大小改 **`config/settings.yml` 的 `worker_pool`**（跑 `python3 -m src.core.settings` 驗證後 commit → 自動部署）。臨時調整可用 Railway 變數 `IMAGE_WORKERS` / `IMAGE_QUEUE_LIMIT` 覆寫，免改檔。
- **升 Supabase Pro**（上線前置；Nano 閒置就吃 46% RAM，Pro 更穩且有備份）。
- 確認 **Sentry 告警**、**Replicate spend 上限**已設好。
**成本**：Railway 用量微增（生成是 I/O-bound，幾乎不多吃 CPU）；Supabase Pro ~$25/月。

### 階段 2 — 成長期 / 多社團同時活躍
**規模**：持續性負載，常態幾十人/分，尖峰上百人湧入。
**訊號**：`worker_pool` 8/16 也常常滿；排隊等待變長；CPU 在收訊尖峰接近 2 vCPU。
**動作**：
1. **水平擴展 web/worker**：Railway 加 **replicas**（每個 replica 各有自己的 pool → 容量倍增）。
   - 注意：目前 pool 是 **in-process**，各 replica 獨立、不共享佇列。容量會加成，但無法「全域公平排隊」。
2. **提高 DB 池**：若加 replica/worker，同步調 `database.py` 的 `pool_size` / `max_overflow`，並確認 Supabase 連線數仍 < 上限（Supavisor 有保護，但 direct 連線要留意）。
3. **升 Supabase compute**（Nano → Small/Medium）：更多連線、更快查詢。
4. 適度再調 `worker_pool.max_workers`（但別超過 DB 池能負荷的頭尾借還量；壓測顯示 16/32 已接近 DB 池 30 的舒適上限）。
**成本**：replicas 與 compute 按用量線性上升；開始需要認真看 Railway/Supabase 帳單。

### 階段 3 — 規模化 / 大量並發（架構升級）
**規模**：持續數百人/分，或需要「永不掉單、可無限加機器」。
**訊號**：加 replica 邊際效益遞減；in-process 池的「各自為政」造成 Replicate 過度並發或排隊不公；重啟會掉正在處理的任務。
**動作**：**把生成從 in-process 執行緒池，改成外部任務佇列 + 獨立 worker 服務**。
```
現在：webhook 收到 → 自己開執行緒跑生成（綁在 web 程序、重啟會掉）

階段 3：webhook 收到 → 丟進佇列（Redis/Celery/RQ 或 DB-backed queue）→ 秒回
                          │
                          └─► 一群獨立的 worker 容器來撈、跑、推結果
                                └ 要更多產能？加 worker 容器（可 autoscale）
```
**好處**（in-process 做不到的）：
- **worker 獨立擴展**，不綁 web server。
- **中央控管 Replicate 並發**，避免各 replica 各自爆量/超支。
- **重啟不掉單**（任務存佇列，不在記憶體）。
- **真實排隊資訊**給用戶：「你排第 N 位，約 X 分鐘」。
**成本 / 代價**：架構複雜度大增（要維護佇列、worker 部署、重試/死信）。**沒到這個規模別做。**

---

## 二、設定旋鈕總覽（Knobs）

| 旋鈕 | 在哪 | 控制什麼 | 何時調 |
|---|---|---|---|
| `worker_pool.max_workers` | `config/settings.yml`（env `IMAGE_WORKERS` 可覆寫） | 同時生幾張（per process） | 階段 1、2 |
| `worker_pool.queue_limit` | `config/settings.yml`（env `IMAGE_QUEUE_LIMIT` 可覆寫） | 可排隊幾個，超過即「忙碌」 | 階段 1、2 |
| `-w` / `--threads` | `Procfile` | HTTP 併發 & 程序數 | 階段 2 |
| `pool_size` / `max_overflow` | `src/models/database.py` | 每 process 的 DB 連線 | 加 worker/replica 時同步 |
| Replicas | Railway → Settings | 水平擴展（容量加成） | 階段 2 |
| Railway 方案 | Railway | 資源上限（Hobby 48GB/48vCPU）、支援 | 通常用量而非上限先到 |
| Supabase compute | Supabase | DB 連線數、查詢速度 | 階段 1（Pro）、2（升級） |
| Replicate spend 上限 | Replicate 帳號 | **費用護欄** | **上線前必設** |

> **黃金原則**：`IMAGE_WORKERS` 不是越大越好。它是**斷路器**——到極限時優雅回「忙碌」，保護你不被 Replicate 費用、DB 連線、LINE 推播限流反噬。放大它時，要同步確認下游（DB 池、Replicate 費用、LINE 推播）撐得住。

---

## 三、升級決策訊號（何時往上一階）

| 觀察到 | 代表 | 該做 |
|---|---|---|
| 少數人偶爾收到「忙碌」 | 正常，尖峰略超容量 | 先觀察，或階段 1 調 8/16 |
| **常態**很多人「忙碌」、排隊久 | 生成容量不足 | 階段 2：加 replica / 調 pool |
| Railway CPU 收訊尖峰貼 2 vCPU | HTTP 層開始吃緊 | 階段 2：加 replica 或調 `-w` |
| DB 連線逼近上限 / 出現 pool timeout | DB 連線不夠 | 升 compute + 調 `pool_size` |
| 加 replica 效益遞減 / 重啟掉單 / 超支 | in-process 池到頭 | 階段 3：外部佇列 + 獨立 worker |
| Replicate 帳單失控 | 用量超過定價能 cover | 檢視[定價](./PRICING.md)、加費用護欄 |

---

## 四、上線前最低配置（Checklist）

- [ ] `config/settings.yml` 的 `worker_pool` = 8 / 16（出廠預設，階段 1 甜蜜點）
- [ ] **Supabase Pro**（穩定性 + 備份）
- [ ] **Replicate spend 上限**（費用護欄，避免濫用燒錢）
- [ ] Sentry 告警、UptimeRobot（或等效）健康監測
- [ ] 壓測 Go-Live 判準通過（見 [測試文件](./TEST.md) 壓-8 節）
- [ ] 已知極限距離預期尖峰有 **2～3 倍餘裕**

---

## 五、一句話總結

> 這個服務**做得大**，但「做大」是**分階段換架構**（調 pool → 加 replica → 外部任務佇列 + 獨立 worker），不是把某個數字調到 100。瓶頸永遠是「同時能生幾張圖」；其餘（收訊、DB、RAM）都有大量餘裕。唯一擺脫不了的天花板是 **Replicate 費用**，那是靠定價解決的生意問題，不是技術牆。
