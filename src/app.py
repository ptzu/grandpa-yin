import os
import time
import uuid
import threading
from urllib.parse import parse_qs, quote, unquote, urlparse
from dotenv import load_dotenv

# Load local .env so `python app.py` works for local dev; on Railway there is no
# .env file, so this is a harmless no-op and platform Variables are used as-is.
load_dotenv()

from flask import Flask, request, abort, Response, render_template, redirect
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from src.core.app_logger import get_logger, request_id_var
from src.core.error_tracking import init_sentry, set_request_context
from src.core.settings import get_member_settings
from src.services.billing import BillingService
from src.services.message_publisher import MessagePublisher
from src.services.line_client import LineClient
from src.services.display_name import resolve_display_name
from src.services.replicate_client import ReplicateClient
from src.services.preview_store import LocalPreviewStore, set_public_base_url
from src.services.user_state_manager import UserStateManager
from src.features.context import FeatureContext
from src.features.feature_registry import FeatureRegistry
from src.features.menu_feature import MenuFeature
from src.features.colorize_feature import ColorizeFeature
from src.features.animate_feature import AnimateFeature
from src.features.edit_feature import EditFeature
from src.features.member_feature import MemberFeature
from src.features.gift_feature import GiftFeature
from src.features.followup_feature import FollowUpFeature
from src.features.photo_intent_feature import PhotoIntentFeature
from src.models.database import init_database, get_session
from src.models.payment_order import PaymentOrder
from src.services.member_service import MemberService
from src.services.gift_card_service import GiftCardService
from src.services.storage_service import StorageService
from src.services.result_archive import ResultArchive
from src.services.ecpay_client import ECPayClient
from src.services.line_client import verify_id_token, verify_id_token_claims
from src.services.payment_service import (
    ECPAY_ACK, ECPAY_REJECT, KIND_GIFT, PaymentError, PaymentService,
)
from src.services.gift_card_service import format_code as format_gift_code
from src.services import gift_card_service as gift_redeem

logger = get_logger("app")

# 全域變數
app = Flask(__name__)
line_bot_api = None
handler = None
publisher = None
user_state_manager = None
feature_registry = None
member_service = None
preview_store = None
payment_service = None
gift_card_service = None
_initialized = False

# 已處理事件的記憶體快取，防止 LINE webhook 重送造成重複處理／重複扣點。
# 注意：僅在單一 process 內去重；多 worker 部署時各 process 各自維護。
_processed_events = {}
_processed_events_lock = threading.Lock()
_PROCESSED_EVENT_TTL = 600  # 秒


def _is_duplicate_event(event_id):
    """檢查 webhookEventId 是否已處理過；未處理過則記錄並回傳 False"""
    now = time.time()
    with _processed_events_lock:
        expired = [eid for eid, ts in _processed_events.items()
                   if now - ts > _PROCESSED_EVENT_TTL]
        for eid in expired:
            del _processed_events[eid]

        if event_id in _processed_events:
            return True
        _processed_events[event_id] = now
        return False

def init():
    """初始化所有 LINE Bot 相關組件"""
    global app, line_bot_api, handler, publisher, user_state_manager, feature_registry, member_service, preview_store, payment_service, gift_card_service, _initialized
    
    # 如果已經初始化過，直接返回
    if _initialized:
        return
    
    logger.info("正在初始化 LINE Bot...")

    # 1. 驗證環境變數
    if not os.getenv("CHANNEL_ACCESS_TOKEN"):
        raise ValueError("CHANNEL_ACCESS_TOKEN 環境變數未設定")
    if not os.getenv("CHANNEL_SECRET"):
        raise ValueError("CHANNEL_SECRET 環境變數未設定")
    if not os.getenv("REPLICATE_API_TOKEN"):
        raise ValueError("REPLICATE_API_TOKEN 環境變數未設定")

    # 2. 錯誤追蹤（設定 SENTRY_DSN 才啟用）
    init_sentry()

    # 3. 初始化資料庫（如果有設定 DATABASE_URL）
    if os.getenv("DATABASE_URL"):
        try:
            init_database()
            # Schema is managed by Supabase migrations (altide-landing-page/supabase/migrations/)
            logger.info("資料庫連線初始化完成")
        except Exception:
            logger.exception("資料庫初始化失敗，會員功能將無法使用")
    else:
        logger.warning("未設定 DATABASE_URL，會員功能將不可用")

    # 4. 初始化 LINE Bot API 與各組件
    channel_access_token = os.getenv("CHANNEL_ACCESS_TOKEN")
    line_bot_api = LineBotApi(channel_access_token)
    handler = WebhookHandler(os.getenv("CHANNEL_SECRET"))
    publisher = MessagePublisher(line_bot_api)
    line_client = LineClient(line_bot_api, channel_access_token)
    user_state_manager = UserStateManager()

    # 5. 創建會員服務（如果資料庫可用）
    if os.getenv("DATABASE_URL"):
        try:
            member_service = MemberService()
        except Exception:
            logger.exception("會員服務初始化失敗")
            member_service = None
    else:
        member_service = None

    # 所有回覆／推播統一補「Hi 名字 😊」開頭：publisher 比 member_service 早建立，
    # 這裡才接上名字解析（會員資料優先，退回 LINE profile）。
    publisher.set_name_resolver(
        lambda uid: resolve_display_name(uid, member_service, line_client)
    )

    # 6. 圖片暫存服務（Supabase Storage，未設定時 EditFeature 會退回 base64 存 state）
    storage_service = StorageService()
    if storage_service.is_configured():
        logger.info(f"Supabase Storage 已設定 (bucket: {storage_service.bucket})")
    else:
        logger.warning("Supabase Storage 未設定，P圖大神將以 base64 暫存於資料庫 state")

    # 7. 影片縮圖的本地降級（Storage 未設定時由本服務自己供圖）
    preview_store = LocalPreviewStore()

    # 8. 儲值與禮物卡（沒設定 ECPAY_* 或設定檔沒有 payments 區段時自動停用）
    #
    # 兌換卡只需要資料庫，不需要金流：金流關掉之後，已經賣出去的卡還是要
    # 兌換得了，否則等於沒收使用者已經付過的錢。
    ecpay = ECPayClient.from_env()
    gift_card_service = GiftCardService() if member_service else None
    payment_service = (PaymentService(ecpay=ecpay, gift_cards=gift_card_service)
                       if member_service else None)
    if payment_service and payment_service.enabled:
        logger.info(f"儲值已啟用：{len(payment_service.packages())} 種點數包")
        logger.info("禮物卡購買頁已啟用" if payment_service.gift_link()
                    else "禮物卡購買頁未啟用（未設定 PUBLIC_BASE_URL）")
    else:
        logger.info("儲值未啟用")

    # 9. 組裝功能的依賴（新增依賴只要加在 FeatureContext，不必動每個 feature 的建構子）
    ctx = FeatureContext(
        line=line_client,
        publisher=publisher,
        state_manager=user_state_manager,
        billing=BillingService(member_service, publisher),
        replicate=ReplicateClient(),
        member_service=member_service,
        storage_service=storage_service,
        preview_store=preview_store,
        result_archive=ResultArchive(storage_service),
        payment_service=payment_service,
        gift_card_service=gift_card_service,
    )

    # 10. 註冊所有功能
    feature_registry = FeatureRegistry(user_state_manager)
    feature_registry.register(MenuFeature(ctx))
    feature_registry.register(ColorizeFeature(ctx))
    feature_registry.register(EditFeature(ctx))
    feature_registry.register(AnimateFeature(ctx))

    # 成品推出去之後的「還要再做點什麼嗎」。本身不搶路由（只認自己的按鈕
    # 文字），但要在 photo_intent 之前註冊。
    feature_registry.register(FollowUpFeature(ctx))

    # 註冊會員功能（如果會員服務可用）
    if member_service:
        feature_registry.register(MemberFeature(ctx))

    # 禮物卡兌換：跟著資料庫走，與金流是否開著無關（見上面第 8 步）
    if gift_card_service:
        feature_registry.register(GiftFeature(ctx))

    # 圖片路由的 catch-all：沒先選功能就上傳的照片由它接住並詢問意圖。
    # 必須最後註冊，否則會搶在 colorize / edit 之前接走圖片。
    feature_registry.register(PhotoIntentFeature(ctx))

    feature_names = [f.name for f in feature_registry.get_all_features()]
    logger.info(f"已註冊 {len(feature_names)} 個功能: {', '.join(feature_names)}")

    # 標記為已初始化
    _initialized = True
    logger.info("LINE Bot 初始化完成")

def main():
    """主程式入口點"""
    logger.info("啟動 LINE Bot 服務")

    # 初始化所有組件
    init()

    # 啟動 Flask 應用程式
    port = int(os.getenv("PORT", 5000))
    debug_mode = os.getenv("FLASK_DEBUG", "False").lower() == "true"

    if debug_mode:
        logger.warning("開發模式已啟用，僅供本地開發使用")

    logger.info(f"服務運行在: http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)

def _public_base_url():
    """本服務對外的 origin（scheme + host）。

    ngrok 與 Railway 都在前面終止 TLS，轉進來的是普通 http，所以
    `request.url_root` 的 scheme 會是 http——而 LINE 明確拒收非 HTTPS 的
    圖片／影片網址（"Must be a valid HTTPS URL"）。scheme 必須取自
    X-Forwarded-Proto 才會正確。

    PUBLIC_BASE_URL 可覆寫，供代理層沒有送這些 header 的環境使用。
    """
    explicit = os.getenv("PUBLIC_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{scheme}://{host}"


def _liff_params():
    """這次請求真正的參數，含 LIFF 包在 `liff.state` 裡的那些。

    從 LIFF URL 開啟時，LINE **不會**把附加的路徑與 query 直接接在 Endpoint URL
    後面，而是整包 URL-encode 進一個 `liff.state` 參數：

        liff.line.me/<id>?p=pay   →   https://<endpoint>/?liff.state=%3Fp%3Dpay

    所以只讀 request.args 會看不到 p——頁面會落在根目錄的預設內容上，而使用者
    只覺得「按了沒反應」。這裡把兩種來源合起來，直接開網址與從 LINE 進來都通。
    """
    params = dict(request.args)
    state = request.args.get("liff.state")
    if state:
        # state 長得像 "?p=share&no=GY..." 或 "/pay?no=..."；只取得出 query 的部分
        decoded = unquote(state)
        parsed = urlparse(decoded)
        query = parsed.query or (decoded.lstrip("?") if not parsed.path else "")
        for key, values in parse_qs(query).items():
            params.setdefault(key, values[0] if values else "")

        # 早期版本的連結把頁面放在路徑上（.../pay、.../gift/share）。那種訊息
        # 還留在使用者的聊天室裡，翻上去點到不該掉進預設頁，所以一併認得。
        if "p" not in params and parsed.path:
            path = parsed.path.strip("/")
            if path == "pay":
                params["p"] = "pay"
            elif path == "gift/share":
                params["p"] = "share"
    # request.args 的值是字串，dict() 之後也保持字串
    return {k: (v[0] if isinstance(v, list) else v) for k, v in params.items()}


@app.route("/", methods=["GET"])
def index():
    """LIFF 的唯一進入點，用 query 參數決定要開哪一頁。

    一個 LIFF app 只能設一個 Endpoint URL，而這裡有兩頁要在 LINE 裡開。理論上
    可以用 `liff.line.me/<id>/<path>` 把路徑接在後面，但實測那條路會落回
    Endpoint URL 本身，而且 LINE 會把結果當成「外部網站」而非 LIFF app（於是
    shareTargetPicker 之類的 API 全部不可用）。query 參數則是穩定傳得進來的，
    所以路由改由 `?p=` 決定：

        https://liff.line.me/<LIFF_ID>?p=pay
        https://liff.line.me/<LIFF_ID>?p=share&no=<訂單編號>

    這裡**直接算繪結果、不做轉址**：LIFF 進入點若回 302，LINE 會判定離開了
    LIFF app，同樣會失去那些 API。

    參數要經過 `_liff_params()` 取得，不能直接讀 request.args——從 LINE 進來時
    它們被包在 `liff.state` 裡。
    """
    params = _liff_params()
    page = params.get("p")
    if page == "start":
        return start_page()
    if page == "pay":
        return pay_page()
    if page == "share":
        return gift_share(order_no=params.get("no", ""))
    if page == "claim":
        return gift_claim(code=params.get("code", ""))
    if page == "done":
        # liff.login from the gift-done page returns here (LINE wraps the
        # target in liff.state and drops it on the endpoint root). Route it
        # back to the done page, now with a login session.
        return gift_done(order_no=params.get("no", ""))

    # 一般訪客（或監控探針）：給一行字就好，不假裝有東西可賣。
    return Response("銀爺爺服務運作中。\n", mimetype="text/plain; charset=utf-8")


@app.route("/webhook", methods=["POST"])
def webhook():
    # 每個 request 一個追蹤 ID，log、Sentry 事件與背景工作都會帶上
    request_id = uuid.uuid4().hex[:8]
    request_id_var.set(request_id)
    set_request_context(request_id=request_id)
    # 讓需要對外連結的功能（影片縮圖）知道本服務的公開網址
    set_public_base_url(_public_base_url())

    # 如果模組載入時初始化失敗，在這裡重試一次
    if not _initialized:
        try:
            logger.info("重試初始化...")
            init()
        except Exception:
            logger.exception("初始化失敗")
            abort(500)

    # 檢查關鍵組件是否已正確初始化
    if handler is None:
        logger.error("Handler 未初始化")
        abort(500)

    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    # 沒帶簽章的一定不是 LINE（掃描器、監控、探測），直接擋掉。
    # 否則 None 簽章會讓 SDK 拋 AttributeError → 走到 500 並灌入 Sentry。
    if not signature:
        logger.warning("缺少 X-Line-Signature，拒絕非 LINE 請求")
        abort(400)

    try:
        # 驗證簽名
        handler.parser.parse(body, signature)

        # 解析請求內容
        import json
        events = json.loads(body).get('events', [])

        for event in events:
            # 去重：LINE 在 webhook 回應逾時／非 200 時會重送同一個 event
            event_id = event.get('webhookEventId')
            if event_id and _is_duplicate_event(event_id):
                logger.info(f"跳過重複的 webhook event: {event_id}")
                continue

            # Sentry 事件附上發生問題的用戶，客訴時可直接比對
            event_user_id = event.get('source', {}).get('userId')
            if event_user_id:
                set_request_context(user_id=event_user_id)

            if event.get('type') == 'follow':
                # 處理加好友事件
                result = handle_follow_event(event)
                if result:  # 如果有 JSON 回應，直接回傳
                    return result
            elif event.get('type') == 'message' and event.get('message', {}).get('type') == 'text':
                # 處理文字訊息
                result = handle_text_message(event)
                if result:  # 如果有 JSON 回應，直接回傳
                    return result
            elif event.get('type') == 'message' and event.get('message', {}).get('type') == 'image':
                # 處理圖片訊息
                result = handle_image_message(event)
                if result:  # 如果有 JSON 回應，直接回傳
                    return result
        
        return "OK"
    except InvalidSignatureError:
        logger.error("Invalid signature error")
        abort(400)
    except Exception:
        logger.exception("Webhook error")
        abort(500)

@app.route("/preview/<token>", methods=["GET"])
def preview(token):
    """供 LINE 抓取影片訊息的縮圖（Storage 未設定時的降級路徑）。

    只在 Storage 未設定時會被用到；正式環境的縮圖走 Supabase signed URL，
    這條路由不會有流量。token 是 32 位隨機 hex，猜不到也列舉不了。
    """
    if preview_store is None:
        abort(503)

    image_bytes = preview_store.load(token)
    if not image_bytes:
        abort(404)

    return Response(
        image_bytes,
        mimetype="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )

# ─────────────────────────────────────────────────────────────────────────────
# 儲值：建單 → 綠界 → 回調入帳
#
# 發點只發生在 /pay/ecpay/callback（綠界的伺服器對伺服器通知，且驗過簽章）。
# /pay/done 是使用者的瀏覽器導回頁，任何人都能開，因此只顯示文字、不碰點數。
# ─────────────────────────────────────────────────────────────────────────────


def _payments_ready():
    """儲值是否可服務；順便補做延遲初始化"""
    if not _initialized:
        try:
            init()
        except Exception:
            logger.exception("初始化失敗")
            return False
    return payment_service is not None and payment_service.enabled


@app.route("/start", methods=["GET"])
def start_page():
    """買點數的單一入口：一頁裡「幫誰買 + 選方案」一次選完。

    bot 只給這一個連結（?p=start），而不是自用一條、送禮一條——長輩看到兩個
    連結會分不清該點哪個。這頁同時列出兩種用途的方案，點方案直接送去綠界，
    不必再進第二頁選方案。

    自用要 LINE 登入（才知道點數進誰的帳戶），送禮不記名不用；頁面在 LINE 內
    開啟（?p=start 走 LIFF），所以登入拿得到。checkout 仍分別打 /pay/checkout
    與 /gift/checkout，這頁只是把兩者的入口合在一起。
    """
    if not _payments_ready():
        abort(503)

    return render_template("start.html",
                           packages=payment_service.packages(),
                           liff_id=os.getenv("LIFF_ID", ""))


@app.route("/pay", methods=["GET"])
def pay_page():
    """LIFF 付款頁：選點數包 → 送去綠界。

    需要 LIFF_ID 才能驗證開頁的人是誰，沒設就等於還沒開通，回 503 而不是
    給一個按了會壞掉的頁面。
    """
    if not _payments_ready():
        abort(503)

    liff_id = os.getenv("LIFF_ID")
    if not liff_id:
        logger.warning("LIFF_ID 未設定，付款頁無法使用")
        abort(503)

    return render_template("pay.html",
                           packages=payment_service.packages(),
                           liff_id=liff_id)


@app.route("/pay/checkout", methods=["POST"])
def pay_checkout():
    """LIFF 付款頁呼叫：建立訂單，回傳要 POST 給綠界的表單欄位。

    身分一律以 LINE 驗證過的 ID token 為準——付款頁在瀏覽器裡，
    自稱的 userId 等於讓任何人替別人建單。
    """
    if not _payments_ready():
        abort(503)

    data = request.get_json(silent=True) or {}
    user_id = verify_id_token(data.get("id_token"), os.getenv("LINE_LOGIN_CHANNEL_ID"))
    if not user_id:
        abort(401)

    base = _public_base_url()
    try:
        with get_session() as session:
            result = payment_service.create_order(
                session,
                user_id,
                data.get("package_id"),
                return_url=f"{base}/pay/ecpay/callback",
                order_result_url=f"{base}/pay/done",
            )
            session.commit()
            return {"action": result["action"], "params": result["params"]}
    except PaymentError as e:
        return {"error": str(e)}, 400
    except Exception:
        logger.exception("建立儲值訂單失敗")
        abort(500)


@app.route("/pay/ecpay/callback", methods=["POST"])
def pay_ecpay_callback():
    """綠界的付款結果通知（ReturnURL）——唯一會發點數的地方。

    綠界收不到 "1|OK" 就會持續重送，所以「已經處理過」也要回 1|OK，
    否則同一筆會被無限重試。真正的重複發點防護在 PaymentService。
    """
    if not _payments_ready():
        abort(503)

    payload = request.form.to_dict()
    try:
        with get_session() as session:
            result = payment_service.handle_callback(session, payload)
    except Exception:
        logger.exception("處理綠界回調失敗")
        # 不回 1|OK，讓綠界重送——這種失敗多半是暫時性的（DB 斷線）
        return ECPAY_REJECT

    # 剛結清、且知道要通知誰 → 推一張完成卡片。重送的回調 credited=False，
    # 不會重複推。gift 提醒買家送出；topup 告訴買家購買完成 + 餘額。
    if result.credited and result.buyer_uid:
        if result.card is not None:
            _nudge_gift_buyer(result.buyer_uid, result.card,
                              payload.get("MerchantTradeNo"))
        else:
            _notify_topup_done(result.buyer_uid, result.points, result.balance)

    return ECPAY_ACK if result.ok else ECPAY_REJECT


def _notify_topup_done(buyer_uid, points, balance):
    """Push the buyer a 'purchase complete' card after a self top-up settles."""
    feature = feature_registry.get_feature_by_name("member") if feature_registry else None
    if feature is None:
        return
    try:
        feature.notify_topup_done(buyer_uid, points, balance)
    except Exception:
        logger.exception("推播儲值完成卡片失敗")


def _nudge_gift_buyer(buyer_uid, card, order_no):
    """Push the buyer a 'your gift is ready to send' message with a link back
    into the share flow — the safety net for closing the page mid-send.

    Takes order_no as a plain string, never the ORM order: the callback's
    session is already closed by here, so touching a detached order would raise
    DetachedInstanceError. `card` is a snapshot (IssuedCard), so card.points is
    safe. Same lesson as the gift-done page.
    """
    feature = feature_registry.get_feature_by_name("gift") if feature_registry else None
    if feature is None:
        return
    try:
        feature.notify_gift_ready_to_send(buyer_uid, card.points, order_no)
    except Exception:
        logger.exception("提醒買家送出禮物失敗")


# ─────────────────────────────────────────────────────────────────────────────
# 禮物卡：朋友在網頁上買 → 拿到卡號 → 長輩在 LINE 裡輸入「兌換」
#
# 這幾條路由刻意「不」走 LIFF：買的人通常是子女，可能坐在電腦前、甚至不是
# LINE 用戶。不記名的卡不需要知道買家是誰，多一道 LINE 登入只會多一個放棄點。
# 身分驗證的缺席不影響安全：建單不會發出任何東西，卡只在驗過簽章的回調裡開立。
# ─────────────────────────────────────────────────────────────────────────────


@app.route("/gift", methods=["GET"])
def gift_page():
    """送禮頁：選點數包 → 送去綠界。"""
    if not _payments_ready():
        abort(503)

    return render_template("gift.html", packages=payment_service.packages())


@app.route("/gift/checkout", methods=["POST"])
def gift_checkout():
    """建立禮物卡訂單，回傳要 POST 給綠界的表單欄位。

    不需要身分驗證：金額與點數由伺服器端設定決定，付款成功前不會產生任何有
    價值的東西。但若付款頁在 LINE 裡（有 id_token），就順手記下「買家是誰」
    ——這樣萬一買家付完款、還沒選朋友就關頁，bot 能提醒他把禮物送出去。
    收禮人仍然完全不記名，是誰領誰得。
    """
    if not _payments_ready():
        abort(503)

    data = request.get_json(silent=True) or {}
    # id_token 是選填的：從 LINE 內開啟會有，一般網頁買家沒有（維持不記名）
    buyer_uid = verify_id_token(data.get("id_token"),
                                os.getenv("LINE_LOGIN_CHANNEL_ID"))
    base = _public_base_url()
    try:
        with get_session() as session:
            result = payment_service.create_order(
                session,
                buyer_uid,                 # 有就記買家，沒有就不記名
                data.get("package_id"),
                kind=KIND_GIFT,
                return_url=f"{base}/pay/ecpay/callback",
                order_result_url=f"{base}/gift/done",
            )
            trade_no = result["order"].merchant_trade_no
            session.commit()
            return {"action": result["action"], "params": result["params"],
                    "order_no": trade_no}
    except PaymentError as e:
        return {"error": str(e)}, 400
    except Exception:
        logger.exception("建立禮物卡訂單失敗")
        abort(500)


@app.route("/gift/done", methods=["GET", "POST"])
def gift_done(order_no=None):
    """付款後導回：在這一頁選朋友把禮物送出。

    使用者的瀏覽器常比綠界的伺服器通知早到，所以頁面會自己輪詢 /gift/card
    等卡開好，再顯示「選朋友送出」。從 liff.login 回來時會以 order_no 直接
    帶入（見根路由 p=done），不必再靠 form/args。
    """
    if order_no is None:
        order_no = (request.form.get("MerchantTradeNo")
                    or request.args.get("no", "")).strip()

    return render_template("gift_done.html",
                           order_no=order_no,
                           liff_id=os.getenv("LIFF_ID", ""))


@app.route("/gift/mark-sent", methods=["POST"])
def gift_mark_sent():
    """分享頁送出卡片後回報，讓卡片標記為「已送出」。

    只是提示用途：下次再開分享頁能提醒買家「已經送過了」，避免同一張一次性
    的卡不小心又送給第二個人。卡在被領取前仍可再送（送錯人時補救），所以這裡
    不阻擋任何事，只留一個記號。
    """
    if gift_card_service is None:
        abort(503)
    order_no = (request.get_json(silent=True) or {}).get("no", "").strip()
    if not order_no:
        return {"ok": False}, 400
    try:
        with get_session() as session:
            gift_card_service.mark_sent(session, order_no)
    except Exception:
        logger.exception("標記禮物卡已送出失敗")
        # 標記失敗不影響已送出的事實，回 ok 讓前端不糾結
    return {"ok": True}


@app.route("/gift/share", methods=["GET"])
def gift_share(order_no=None):
    """用 LINE 原生的好友選擇器把禮物卡片送給朋友。

    這一頁必須從 LIFF 連結開啟（liff.line.me/<LIFF_ID>?no=...），不能是付款完成
    頁的一部分：綠界的付款流程會把瀏覽器帶離本站再帶回來，LIFF 的執行環境不保證
    撐得過那一趟。做成獨立的一頁等於重新啟動一次 LIFF，也讓買家在電腦上付完款、
    改用手機開這個連結分享。

    LINE 沒有讀取好友清單的 API——挑中的是誰，我們自始至終不會知道，訊息也是由
    買家自己的帳號送出的。這裡能做的就是把卡片組好交給 LINE。
    """
    if not _payments_ready():
        abort(503)

    liff_id = os.getenv("LIFF_ID")
    if not liff_id:
        logger.warning("LIFF_ID 未設定，禮物卡分享頁無法使用")
        abort(503)

    if order_no is None:
        order_no = _liff_params().get("no", "")
    return render_template("gift_share.html",
                           order_no=order_no.strip(),
                           liff_id=liff_id,
                           bot_basic_id=os.getenv("LINE_BASIC_ID", ""))


@app.route("/gift/claim", methods=["GET"])
def gift_claim(code=None):
    """收禮的人按下卡片上的「領取」之後看到的頁面。

    存在的理由是把卡號變成純內部識別：收禮的長輩不必看到它、不必打字，按一下
    就入帳。LINE 不允許推播給沒跟 bot 互動過的人，所以「按那一下」省不掉——
    它就是那個互動；但除了那一下之外，其餘步驟都能拿掉。

    身分一律以 LINE 驗證過的 ID token 為準（見 /gift/claim/redeem），這一頁只
    負責把 token 拿到手。
    """
    if gift_card_service is None:
        abort(503)

    liff_id = os.getenv("LIFF_ID")
    if not liff_id:
        logger.warning("LIFF_ID 未設定，禮物卡領取頁無法使用")
        abort(503)

    if code is None:
        code = _liff_params().get("code", "")

    return render_template("gift_claim.html", code=code.strip(), liff_id=liff_id,
                           bot_basic_id=os.getenv("LINE_BASIC_ID", ""))


@app.route("/gift/claim/redeem", methods=["POST"])
def gift_claim_redeem():
    """領取頁呼叫：驗明身分後把卡兌換給他。

    點數的去向由 LINE 驗過的 ID token 決定，不是由頁面自稱的 userId——否則
    任何人都能把別人的禮物領到自己帳上。兌換本身仍然只會成功一次（卡號唯一鍵
    + 列鎖 + redeemed_at），重複按不會重複加點。
    """
    if gift_card_service is None:
        abort(503)

    data = request.get_json(silent=True) or {}
    claims = verify_id_token_claims(data.get("id_token"),
                                    os.getenv("LINE_LOGIN_CHANNEL_ID"))
    if not claims or not claims.get("sub"):
        abort(401)

    user_id = claims["sub"]
    try:
        # 領取的人不一定已經是會員（可能還沒加 bot 好友），先確保帳戶存在，
        # 否則點數沒有地方可去
        if member_service:
            member_service.get_or_create_member(user_id, claims.get("name"))

        result = gift_card_service.redeem_for_user(user_id, data.get("code"))
    except Exception:
        logger.exception("領取禮物卡失敗")
        abort(500)

    if result.status != gift_redeem.OK:
        return {"status": result.status, "points": result.points}

    # 頁面關掉之後聊天室裡也要留得下痕跡
    feature = feature_registry.get_feature_by_name("gift") if feature_registry else None
    if feature:
        try:
            feature.notify_gift_received(user_id, result.points, result.balance)
        except Exception:
            # 推播失敗不影響已經入帳的事實，頁面上仍然會顯示成功
            logger.exception("推播收禮通知失敗")

    return {"status": result.status, "points": result.points,
            "balance": result.balance}


@app.route("/gift/card", methods=["GET"])
def gift_card_status():
    """給 /gift/done 輪詢用：這筆訂單的卡開出來了嗎？

    「還沒」是正常答案而不是錯誤——導回頁跟綠界的回調是兩條路，誰先到不一定。
    """
    if not _payments_ready() or gift_card_service is None:
        abort(503)

    order_no = (request.args.get("no") or "").strip()
    if not order_no:
        return {"error": "缺少訂單編號"}, 400

    try:
        with get_session() as session:
            card = gift_card_service.card_for_order_no(session, order_no)
    except Exception:
        logger.exception("查詢禮物卡失敗")
        abort(500)

    if card is None:
        return {"ready": False}

    return {
        "ready": True,
        "points": card.points,
        "redeemed": card.redeemed,
        "sent": card.sent,
        # 已經兌換過就不再回傳卡號：沒有用處，也少一個外流的地方
        "code": None if card.redeemed else format_gift_code(card.code),
    }


@app.route("/pay/done", methods=["GET", "POST"])
def pay_done():
    """付款後使用者被導回的頁面。

    顯示「購買了幾點」但不顯示餘額：購買點數是下單時就定死的（訂單快照），
    講它安全；餘額由回調決定、可能差幾秒，這裡若搶說「已加值」而回調還沒到，
    長輩會以為錢丟了——所以只說「會加進帳戶」，不報餘額。
    """
    order_no = (request.form.get("MerchantTradeNo")
                or request.args.get("no", "")).strip()
    points = None
    if order_no:
        try:
            with get_session() as session:
                order = (session.query(PaymentOrder)
                         .filter_by(merchant_trade_no=order_no).first())
                if order is not None:
                    points = order.points
        except Exception:
            logger.exception("查詢付款訂單點數失敗")
    return render_template("pay_done.html", points=points)


def handle_text_message(event):
    """處理文字訊息，委託給 FeatureRegistry"""
    try:
        result = feature_registry.route_text_message(event)
        return result
    except Exception:
        logger.exception("handle_text_message error")
        return None

def handle_image_message(event):
    """處理圖片訊息，委託給 FeatureRegistry"""
    try:
        result = feature_registry.route_image_message(event)
        return result
    except Exception:
        logger.exception("handle_image_message error")
        return None

def handle_follow_event(event):
    """處理加好友事件 - 自動建立會員並發送歡迎訊息"""
    try:
        # 取得用戶 ID
        user_id = event.get('source', {}).get('userId', '')
        if not user_id:
            logger.error("follow event 無法取得用戶 ID")
            return None

        logger.info(f"用戶加好友: {user_id}")

        # 檢查是否有會員服務
        if not member_service:
            logger.warning("會員服務未啟用，跳過自動註冊")
            return None

        # 檢查會員是否已存在（防止重複加好友刷點數）
        existing_member = member_service.get_member_info(user_id)
        is_new_member = existing_member is None

        # 透過 LINE API 取得用戶資料
        try:
            profile = line_bot_api.get_profile(user_id)
            display_name = profile.display_name
        except Exception as e:
            logger.warning(f"無法取得用戶資料: {str(e)}")
            display_name = "使用者"

        # 建立或更新會員
        member = member_service.get_or_create_member(
            user_id=user_id,
            display_name=display_name
        )

        if not member:
            logger.error(f"建立會員失敗: {user_id}")
            return None

        logger.info(f"{'新會員已建立' if is_new_member else '會員資料已更新'}: {user_id}")

        # 註冊獎勵：grant_signup_bonus 內部以 row lock + 交易記錄檢查保證
        # 同一帳號只發放一次（防止 LINE 重送或快速解除封鎖再加回刷點數）
        welcome_points = get_member_settings().welcome_points
        bonus_granted = False
        if welcome_points > 0:
            bonus_granted = member_service.grant_signup_bonus(user_id, welcome_points)
            if bonus_granted:
                # 重新查詢，讓歡迎訊息顯示含獎勵的正確餘額
                member = member_service.get_member_info(user_id) or member


        # 發送歡迎訊息（新舊會員不同內容）
        if is_new_member:
            welcome_message = f"""{member['display_name']}，歡迎！

您現在有 {member['points']} 點。

最簡單的用法：直接把照片傳給我，我會問您想做什麼。

想看我會做哪些事，輸入「功能」就可以了。"""
        else:
            # 舊會員重新加入
            welcome_message = f"""{member['display_name']}，歡迎回來！

您還有 {member['points']} 點。

直接把照片傳給我就可以開始了。"""

        # 發送歡迎訊息
        try:
            from linebot.models import TextSendMessage
            publisher.process_push_message(user_id, TextSendMessage(text=welcome_message))
        except Exception:
            logger.exception(f"發送歡迎訊息失敗: {user_id}")

        return None

    except Exception:
        logger.exception("handle_follow_event error")
        return None

# 模組載入時自動初始化（適用於生產環境）
def _auto_init():
    """Auto initialize on module load if environment variables are available"""
    try:
        # 檢查是否有必要的環境變數
        if (os.getenv("CHANNEL_ACCESS_TOKEN") and
            os.getenv("CHANNEL_SECRET") and
            os.getenv("REPLICATE_API_TOKEN")):
            logger.info("檢測到生產環境，開始自動初始化...")
            init()
        else:
            logger.info("環境變數未完整設定，跳過自動初始化（適用於開發環境）")
    except Exception:
        logger.exception("自動初始化失敗，將在第一次請求時重試")

# 執行自動初始化
_auto_init()

if __name__ == "__main__":
    main()
