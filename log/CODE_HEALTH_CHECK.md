# 銀爺爺 LINE Bot 程式碼與架構健檢報告

> 健檢日期：2026-07-07
> 範圍：app.py、features/、services/、models/、message_publisher.py、user_state_manager.py、Procfile
> 部署環境：Railway（Flask + gunicorn）、Supabase（PostgreSQL）

---

## 一、總體評價

### 優點

- **Feature Registry + BaseFeature 的插件式設計方向正確**，新增功能的擴充成本低
- **資料層用 SQLAlchemy ORM 全程參數綁定，沒有 SQL Injection 風險**；`deduct_points` 有用 `with_for_update()` 做 row lock、`balance_after` 留帳，這兩點做得比多數同規模專案好
- **機密資訊全走環境變數**、`.env` 有進 `.gitignore`，repo 內沒有洩漏的金鑰
- 新的 accounts / linked_identities 統一帳號 schema 設計合理

### 缺點（一句話總結）

目前的架構是「**單一 sync worker + 每則訊息 2~3 次阻塞式 LINE API 呼叫 + 無上限的背景執行緒 + 先服務後扣點**」，這個組合在 1000 名活躍用戶下**會撐不住**，而且金流上有可被利用的漏洞。好消息是問題都集中在少數幾個點，不需要重寫，逐項修就能到位。

---

## 二、關鍵問題與建議

### 🔴 高危險 1：先服務、後扣點 ＋ 無冪等性 → 點數（金流）漏洞

**問題描述與影響：**

三個獨立的洞，疊起來就是「用戶可以免費用、或被重複扣點」：

1. `colorize_feature.py:118-129` 和 `edit_feature.py:221-232`：**圖片先處理完才扣點**，扣點失敗只印一行 log 就放行（`⚠️ 扣點失敗，但圖片已處理完成`）。Replicate 的成本已經花掉了。
2. `app.py:170-186`：**完全沒有處理 LINE webhook 的重送（redelivery）**。LINE 在 webhook 回應太慢或非 200 時會重送同一個 event，目前同一張圖會被處理兩次、扣兩次點（或免費處理兩次）。
3. `app.py:56-58, 84-88`：資料庫初始化失敗時 `member_service = None`，服務**繼續運作但完全不扣點**——DB 掛掉的期間等於免費放送 Replicate 額度。

**具體修改建議：** 改成「先扣點（含檢查）→ 處理 → 失敗退點」，並用 LINE 的 `webhookEventId` 做冪等：

```python
# app.py webhook 內，處理每個 event 前
for event in events:
    # LINE 重送的 event 會帶 deliveryContext.isRedelivery = true
    if event.get('deliveryContext', {}).get('isRedelivery'):
        event_id = event.get('webhookEventId')
        if _already_processed(event_id):  # 查 processed_events 表或 Redis
            continue

# colorize/edit 的背景處理改為：
def process_image_async():
    # 1. 先扣點，扣不到就直接結束
    if self.member_service:
        if not self.member_service.deduct_points(user_id, self.required_points,
                                                 feature_type='colorize'):
            self.publisher.process_push_message(
                user_id, TextSendMessage(text="點數不足或扣點失敗，本次未處理。"), event)
            return
    try:
        output_url = self._colorize_image(image_bytes)
        self.publisher.process_push_message(user_id, ImageSendMessage(...), event)
    except Exception:
        # 2. 處理失敗 → 退點，並把 usage_log 標成 failed
        if self.member_service:
            self.member_service.add_points(user_id, self.required_points,
                                           description='彩色化失敗退點')
        ...
```

另外 `member_service = None` 的降級應該改成「**拒絕服務**」而不是「免費服務」：features 檢查不到 member_service 時回覆「系統維護中」。

---

### 🔴 高危險 2：`route_image_message` 會把 `handle_image` 執行兩次

**問題描述與影響：** `feature_registry.py:96-98`：

```python
if hasattr(feature, 'handle_image') and feature.handle_image(event) is not None:
    print(f"路由圖片到功能: {feature.name}")
    return feature.handle_image(event)   # ← 第二次執行！
```

這裡用「實際執行 handle_image」來測試「能不能處理」，只要回傳非 None 就會**再執行一次**：圖片下載兩次、回覆兩次、背景執行緒開兩條、扣點兩次。目前多數路徑靠「無狀態時回傳 None」僥倖沒炸，但這是顆地雷。

**具體修改建議：** 把「判斷」和「執行」分開，比照文字訊息的 `can_handle`：

```python
# base_feature.py 新增
def can_handle_image(self, user_id: str) -> bool:
    return False

# colorize_feature.py
def can_handle_image(self, user_id: str) -> bool:
    return self.is_user_in_state(user_id, "waiting")

# feature_registry.py
for feature in self.features:
    if feature.can_handle_image(user_id):
        return feature.handle_image(event)
return None
```

---

### 🔴 高危險 3：每則訊息 2~3 次阻塞式 `get_profile` ＋ 單一 sync worker → 1000 人撐不住

**問題描述與影響：** 這是「能不能支撐 1000 人」的核心答案。目前一則文字訊息的同步路徑是：

1. 各 feature 的 `handle_text` 開頭無條件呼叫 `get_user_name()` → **LINE API 往返一次**（`base_feature.py:61-68`，連用不到名字的分支也會呼叫）
2. `publisher.process_reply_message` 又呼叫 `_is_valid_user()` → **再一次 `get_profile`**（`message_publisher.py:205`）
3. 路由過程中 registry 查一次 state、各 feature 的 `can_handle` 又各查一次 → **同一個 DB 查詢重複 3+ 次**

而 `Procfile` 是 `gunicorn app:app`（預設 **1 個 sync worker**），意思是同一時間**只能處理一個請求**，上面每次 100~300ms 的 LINE API 往返會直接串成佇列。10 個用戶同時傳訊息，第 10 位要等好幾秒；LINE 等不到回應就重送，又觸發問題 1 的重複扣點。另外 `get_profile` 有 rate limit，1000 人的量級下會開始吃 429。

**具體修改建議：**

```python
# 1. Procfile：改用 threaded workers
web: gunicorn app:app -w 2 --threads 8 --timeout 60 --log-file - --error-logfile -

# 2. 刪掉 _is_valid_user 的 get_profile 驗證（見 🟡 問題 7），
#    reply_message 本身失敗就足以告訴你用戶無效

# 3. get_user_name 不要每則訊息都打 LINE API——
#    display_name 已經存在 grandpa_yin.user_profiles 了，優先讀 DB：
def get_user_name(self, user_id: str) -> str:
    if self.member_service:
        member = self.member_service.get_member_info(user_id)
        if member and member['display_name'] != '使用者':
            return member['display_name']
    try:
        return self.line_bot_api.get_profile(user_id).display_name
    except Exception:
        return "使用者"

# 4. 路由時把 user_state 查一次後傳下去，不要每個 can_handle 各查一次
result = feature.handle_text(event, user_state=user_state)
```

搭配 pool 設定（目前 `pool_size=3, max_overflow=5` 只有 8 條連線）建議調到 `pool_size=5, max_overflow=10`，並考慮改用 Supabase 的 connection pooler（port 6543）連線字串。

---

### 🔴 高危險 4：外部呼叫沒有 timeout、背景執行緒無上限（韌性 / 連鎖失效）

**問題描述與影響：**

- `requests.post(url, headers=headers, json=data)`（loading animation，`colorize_feature.py:226`）**沒有 timeout**——LINE API 卡住時這條執行緒永遠不會回來
- `replicate.run()` 沒有設 timeout，Replicate 慢的時候執行緒堆積
- `threading.Thread(target=...).start()` **無上限**：Replicate 一變慢，100 個用戶就是 100 條執行緒，每條又佔 DB 連線（pool 只有 8 條）→ 連線耗盡 → 整個服務對所有人卡死。這就是 cascading failure，目前的架構會發生。
- 全專案**沒有任何 retry / exponential backoff**

**具體修改建議：** 用有界的 ThreadPoolExecutor 取代裸 Thread，所有外部呼叫加 timeout：

```python
# app.py 初始化一次，注入給 features
from concurrent.futures import ThreadPoolExecutor
image_executor = ThreadPoolExecutor(max_workers=4)  # 同時最多 4 張圖在處理

# feature 內
try:
    self.executor.submit(process_image_async)
except RuntimeError:
    # 佇列滿：優雅降級，而不是拖垮整台
    self.publisher.process_push_message(user_id,
        TextSendMessage(text="目前使用人數較多，請稍後再試 🙏"), event)

# 所有 requests 呼叫
response = requests.post(url, headers=headers, json=data, timeout=(3, 10))

# 重試（可用 tenacity 套件）
from tenacity import retry, stop_after_attempt, wait_exponential
@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
def _push_with_retry(...): ...
```

---

### 🔴 高危險 5：整個專案用 `print()` 當日誌，出事時無法查修（可觀測性）

**問題描述與影響：** 沒有 logging module、沒有分級、沒有 request id / user id 的結構化欄位。客戶回報「我扣了點但沒收到圖」時，只能在 Railway 的 stdout 裡大海撈針，而且多執行緒下 print 會交錯。另外 `message_publisher.py:206` 把整包 `validation_result`（含 displayName、statusMessage 等 **PII**）印進 log。`usage_logs.status` 定義了 `processing/completed/failed` 但程式只寫 `completed`——**失敗的操作在 DB 完全沒有軌跡**。

**具體修改建議：** 建一個 `logger.py`，全專案取代 print：

```python
# logger.py
import logging, sys, uuid
from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

class ContextFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id_var.get()
        return True

def get_logger(name):
    logger = logging.getLogger(name)
    if not logger.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"))
        h.addFilter(ContextFilter())
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
    return logger

# app.py webhook 開頭
@app.route("/webhook", methods=["POST"])
def webhook():
    request_id_var.set(str(uuid.uuid4())[:8])
    ...
```

搭配 Sentry 只要三行（Railway 上很好接）：

```python
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration
sentry_sdk.init(dsn=os.getenv("SENTRY_DSN"), integrations=[FlaskIntegration()])
```

失敗路徑補寫 `UsageLog(status='failed', log_metadata={'error': ...})`，這樣 DB 出現 bad data 時能對得回來。

---

### 🟡 中等 6：raw exception 訊息直接回傳給終端用戶（資訊洩漏）

`colorize_feature.py:147`、`edit_feature.py:250` 等多處：`TextSendMessage(text=f"處理圖片時發生錯誤: {str(e)}")`。exception 內容可能含內部 URL、model 名稱、DB 錯誤細節，全部推播給用戶（還是長輩用戶）。

**建議：** 對用戶固定回「處理失敗，點數已退還，請稍後再試」，完整 stack trace 進 log/Sentry。

---

### 🟡 中等 7：`_is_valid_user` 的「單元測試邏輯」混進正式流程

`message_publisher.py:207-211`：驗證失敗時把**原本要發的訊息包成 JSON 回給 webhook caller**，註解寫著「(unit test)」。這是測試用的後門留在生產路徑：多耗一次 `get_profile`（見問題 3），且行為詭異——用戶封鎖 Bot 時 LINE 收到一包訊息 JSON。

**建議：** 刪除這個機制，測試改用 mock `line_bot_api`；發送失敗直接 try/except `LineBotApiError` 即可。

---

### 🟡 中等 8：整張圖片以 base64 存進 `bot_sessions.state_metadata`（JSONB）

`edit_feature.py:101-103`：一張照片 base64 後 1~7MB，塞進 JSONB 造成 row 膨脹、每次 `get_state` 都把整張圖從 DB 拉回來（而路由時 `can_handle` 會查好幾次 state，等於一則文字訊息搬好幾 MB）。

**建議：** 圖存 Supabase Storage（或至少本機暫存檔），state 只存 path/key；佈署既然在 Railway 單機，暫存檔即可：

```python
tmp_path = os.path.join(tempfile.gettempdir(), f"edit_{user_id}_{uuid.uuid4().hex}.jpg")
with open(tmp_path, 'wb') as f:
    f.write(image_bytes)
self.set_user_state(user_id, "waiting_description", {"image_path": tmp_path})
```

---

### 🟡 中等 9：follow event 的 TOCTOU race（歡迎點數）

`app.py:236-273`：「查 existing_member → 建立 → 加點」不是原子的。兩個 follow event 幾乎同時到（LINE 重送、快速封鎖再加回）都會看到 `existing_member is None`，各送 50 點。`linked_identities` 的 unique constraint 會擋掉第二個帳號，但第二個 event 的加點可能落在第一個帳號上。

**建議：** 把「建立 + 首次加點」放進同一個 transaction，或給 `transactions` 加一筆 `description='新會員註冊獎勵'` 的唯一性檢查（同 account 只允許一筆）。

---

### 🟡 中等 10：`_is_global_command` 複製三份、Registry 用 `features[0]` 偷拿 state_manager

- 同一份全局命令清單存在 `feature_registry.py:9`、`colorize_feature.py:46`、`edit_feature.py:46` 三處，改一處忘兩處就出 routing bug。
- `feature_registry.py:116-118` 透過 `self.features[0]` 拿 state——如果哪天註冊順序變了或第一個 feature 沒有 state_manager 就爆。

**建議：** Registry 建構時直接注入 `state_manager`，全局命令清單只留 Registry 一份，features 透過 registry 查詢。

---

### 🟢 建議優化 11：初始化改為 application factory

`app.py` 的 global 變數 + `_initialized` flag + `_auto_init()` 是脆弱的單例模式（webhook 裡還要防 `handler is None`）。改成 Flask 標準的 `create_app()` factory，把依賴放在 `app.extensions` 或明確的 container 物件，測試也不用再依賴 `_is_valid_user` 那個後門。

---

### 🟢 建議優化 12：webhook 繞過 SDK handler 手動 `json.loads`

`app.py:164-168` 用 `handler.parser.parse` 只做簽名驗證，然後自己重新 parse dict。簽名驗證有做（**這點是對的**），但 dict-based event 讓所有 feature 都要手寫 `event.get('source', {}).get(...)`。建議直接用 parser 回傳的 typed event 物件。

---

### 🟢 建議優化 13：Schema 稽核欄位補強

- `accounts` 沒有 `updated_at`——點數餘額被改過卻不知道最後改動時間
- `transactions` 建議加 `created_by`／`source`（'linebot' | 'admin_script' | 'web'）欄位，追 bad data 時能區分是哪個入口寫的（已有 `scripts/add_member.py` 這種 CLI 入口）
- `colorize_feature.py:18` 的 `os.environ["REPLICATE_API_TOKEN"] = os.getenv(...)` 是 no-op，可刪

---

## 三、下一步行動清單（按優先順序）

1. **堵金流洞（問題 1）**：改成「先扣點 → 處理 → 失敗退點」，加上 `webhookEventId` 冪等檢查，DB 掛掉時改為拒絕服務而非免費服務。這是唯一直接影響錢的項目。
2. **修 `route_image_message` 雙重執行（問題 2）**：加 `can_handle_image()`，把判斷與執行分開。改動小、風險移除大。
3. **降低每則訊息的外部呼叫（問題 3）**：刪 `_is_valid_user` 的 get_profile、`get_user_name` 改讀 DB、user_state 一次查詢傳遞下去；`Procfile` 加 `-w 2 --threads 8`。做完這條，1000 人規模的延遲問題大致解除。
4. **韌性基本盤（問題 4）**：所有 `requests` 加 timeout、裸 Thread 換成 `ThreadPoolExecutor(max_workers=4)`、滿載時回「請稍後再試」。
5. **可觀測性（問題 5）**：導入 logging module（帶 request_id）、接 Sentry、失敗路徑寫 `usage_logs(status='failed')`。這條做完，之後客訴才查得動。

---

## 附錄：「1000 人撐不撐得住」的直接回答

現況撐不住——瓶頸不在 Supabase 或 Railway，而在**單 sync worker、每訊息多次阻塞 API 呼叫、無界執行緒**這三件事。完成行動清單第 3、4 項後，這個架構（Flask + Railway 單機 + Supabase）支撐 1000 名註冊用戶、數十人同時在線是沒有問題的；要再往上（例如同時 100+ 張圖在處理）才需要考慮把圖片處理拆成獨立 worker + 佇列（如 Redis + RQ）。
