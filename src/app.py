import os
import time
import uuid
import threading
from dotenv import load_dotenv

# Load local .env so `python app.py` works for local dev; on Railway there is no
# .env file, so this is a harmless no-op and platform Variables are used as-is.
load_dotenv()

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from src.core.app_logger import get_logger, request_id_var
from src.core.error_tracking import init_sentry, set_request_context
from src.services.message_publisher import MessagePublisher
from src.services.line_client import LineClient
from src.services.user_state_manager import UserStateManager
from src.features.context import FeatureContext
from src.features.feature_registry import FeatureRegistry
from src.features.menu_feature import MenuFeature
from src.features.colorize_feature import ColorizeFeature
from src.features.edit_feature import EditFeature
from src.features.member_feature import MemberFeature
from src.features.photo_intent_feature import PhotoIntentFeature
from src.models.database import init_database
from src.services.member_service import MemberService
from src.services.storage_service import StorageService

logger = get_logger("app")

# 全域變數
app = Flask(__name__)
line_bot_api = None
handler = None
publisher = None
user_state_manager = None
feature_registry = None
member_service = None
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
    global app, line_bot_api, handler, publisher, user_state_manager, feature_registry, member_service, _initialized
    
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

    # 6. 圖片暫存服務（Supabase Storage，未設定時 EditFeature 會退回 base64 存 state）
    storage_service = StorageService()
    if storage_service.is_configured():
        logger.info(f"Supabase Storage 已設定 (bucket: {storage_service.bucket})")
    else:
        logger.warning("Supabase Storage 未設定，圖片編輯將以 base64 暫存於資料庫 state")

    # 7. 組裝功能的依賴（新增依賴只要加在 FeatureContext，不必動每個 feature 的建構子）
    ctx = FeatureContext(
        line=line_client,
        publisher=publisher,
        state_manager=user_state_manager,
        member_service=member_service,
        storage_service=storage_service,
    )

    # 8. 註冊所有功能
    feature_registry = FeatureRegistry(user_state_manager)
    feature_registry.register(MenuFeature(ctx))
    feature_registry.register(ColorizeFeature(ctx))
    feature_registry.register(EditFeature(ctx))

    # 註冊會員功能（如果會員服務可用）
    if member_service:
        feature_registry.register(MemberFeature(ctx))

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

@app.route("/webhook", methods=["POST"])
def webhook():
    # 每個 request 一個追蹤 ID，log、Sentry 事件與背景工作都會帶上
    request_id = uuid.uuid4().hex[:8]
    request_id_var.set(request_id)
    set_request_context(request_id=request_id)

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
        welcome_points = int(os.getenv("WELCOME_POINTS", "0"))
        bonus_granted = False
        if welcome_points > 0:
            bonus_granted = member_service.grant_signup_bonus(user_id, welcome_points)
            if bonus_granted:
                # 重新查詢，讓歡迎訊息顯示含獎勵的正確餘額
                member = member_service.get_member_info(user_id) or member


        # 發送歡迎訊息（新舊會員不同內容）
        if is_new_member:
            welcome_message = f"""🎉 歡迎加入！

👤 會員註冊成功
📝 姓名：{member['display_name']}
💎 點數：{member['points']} 點"""

            if bonus_granted:
                welcome_message += f"\n🎁 註冊獎勵：+{welcome_points} 點"

            welcome_message += """

📋 使用說明：
• 最簡單：直接傳一張照片給我，我會問您想做什麼 📷
• 輸入「!功能」查看功能表
• 輸入「點數」查看剩餘點數
• 輸入「圖片彩色化」處理黑白照片
• 輸入「圖片編輯」編輯照片

💡 開始使用吧！"""
        else:
            # 舊會員重新加入
            welcome_message = f"""👋 歡迎回來！

📝 姓名：{member['display_name']}
💎 剩餘點數：{member['points']} 點

📋 使用說明：
• 最簡單：直接傳一張照片給我，我會問您想做什麼 📷
• 輸入「!功能」查看功能表
• 輸入「點數」查看剩餘點數
• 輸入「圖片彩色化」處理黑白照片
• 輸入「圖片編輯」編輯照片

💡 繼續使用吧！"""

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
