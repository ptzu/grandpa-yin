import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from message_publisher import MessagePublisher
from user_state_manager import UserStateManager
from features.feature_registry import FeatureRegistry
from features.menu_feature import MenuFeature
from features.colorize_feature import ColorizeFeature
from features.edit_feature import EditFeature
from features.member_feature import MemberFeature
from models.database import init_database
from services.member_service import MemberService

# 全域變數
app = Flask(__name__)
line_bot_api = None
handler = None
publisher = None
user_state_manager = None
feature_registry = None
member_service = None
_initialized = False

def init():
    """初始化所有 LINE Bot 相關組件"""
    global app, line_bot_api, handler, publisher, user_state_manager, feature_registry, member_service, _initialized
    
    # 如果已經初始化過，直接返回
    if _initialized:
        return
    
    print("🚀 正在初始化 LINE Bot...")
    
    # 1. 驗證環境變數
    print("📋 檢查環境變數...")
    if not os.getenv("CHANNEL_ACCESS_TOKEN"):
        raise ValueError("CHANNEL_ACCESS_TOKEN 環境變數未設定")
    if not os.getenv("CHANNEL_SECRET"):
        raise ValueError("CHANNEL_SECRET 環境變數未設定")
    if not os.getenv("REPLICATE_API_TOKEN"):
        raise ValueError("REPLICATE_API_TOKEN 環境變數未設定")
    print("✅ 環境變數檢查完成")
    
    # 2. Flask 應用程式已在模組層級初始化
    print("🌐 Flask 應用程式已準備就緒")
    print("✅ Flask 應用程式初始化完成")
    
    # 3. 初始化資料庫（如果有設定 DATABASE_URL）
    if os.getenv("DATABASE_URL"):
        print("🗄️  初始化資料庫連線...")
        try:
            init_database()
            # Schema is managed by Supabase migrations (altide-landing-page/supabase/migrations/)
            print("✅ 資料庫連線初始化完成")
        except Exception as e:
            print(f"⚠️  資料庫初始化失敗: {str(e)}")
            print("ℹ️  會員功能將無法使用")
    else:
        print("ℹ️  未設定 DATABASE_URL，會員功能將不可用")
    
    # 4. 初始化 LINE Bot API
    print("🤖 初始化 LINE Bot API...")
    line_bot_api = LineBotApi(os.getenv("CHANNEL_ACCESS_TOKEN"))
    handler = WebhookHandler(os.getenv("CHANNEL_SECRET"))
    print("✅ LINE Bot API 初始化完成")
    
    # 5. 創建統一的訊息發送器
    print("📤 初始化訊息發送器...")
    publisher = MessagePublisher(line_bot_api)
    print("✅ 訊息發送器初始化完成")
    
    # 6. 創建用戶狀態管理器
    print("👤 初始化用戶狀態管理器...")
    user_state_manager = UserStateManager()
    print("✅ 用戶狀態管理器初始化完成")
    
    # 7. 創建會員服務（如果資料庫可用）
    if os.getenv("DATABASE_URL"):
        print("👥 初始化會員服務...")
        try:
            member_service = MemberService()
            print("✅ 會員服務初始化完成")
        except Exception as e:
            print(f"⚠️  會員服務初始化失敗: {str(e)}")
            member_service = None
    else:
        member_service = None
    
    # 8. 創建功能註冊表
    print("📝 初始化功能註冊表...")
    feature_registry = FeatureRegistry()
    print("✅ 功能註冊表初始化完成")
    
    # 9. 註冊所有功能
    print("🔧 註冊功能模組...")
    menu_feature = MenuFeature(line_bot_api, publisher, user_state_manager, member_service)
    colorize_feature = ColorizeFeature(line_bot_api, publisher, user_state_manager, member_service)
    edit_feature = EditFeature(line_bot_api, publisher, user_state_manager, member_service)
    
    feature_registry.register(menu_feature)
    feature_registry.register(colorize_feature)
    feature_registry.register(edit_feature)
    
    # 註冊會員功能（如果會員服務可用）
    if member_service:
        member_feature = MemberFeature(line_bot_api, publisher, user_state_manager, member_service)
        feature_registry.register(member_feature)
        print("✅ 會員功能已啟用")
    
    print(f"✅ 已註冊 {len(feature_registry.get_all_features())} 個功能:")
    for feature in feature_registry.get_all_features():
        print(f"   - {feature.name}")
    
    # 標記為已初始化
    _initialized = True
    print("🎉 LINE Bot 初始化完成！")

def main():
    """主程式入口點"""
    print("=" * 50)
    print("🚀 啟動 LINE Bot 服務")
    print("=" * 50)
    
    # 初始化所有組件
    init()
    
    # 啟動 Flask 應用程式
    print("🌐 啟動 Flask 伺服器...")
    port = int(os.getenv("PORT", 5000))
    debug_mode = os.getenv("FLASK_DEBUG", "False").lower() == "true"
    
    if debug_mode:
        print("🔧 開發模式已啟用 - 程式碼變更時會自動重載")
        print("⚠️  注意：開發模式僅用於本地開發，生產環境請關閉")
    
    print(f"📍 服務運行在: http://0.0.0.0:{port}")
    print("=" * 50)
    
    app.run(host="0.0.0.0", port=port, debug=debug_mode)

@app.route("/webhook", methods=["POST"])
def webhook():
    # 如果模組載入時初始化失敗，在這裡重試一次
    if not _initialized:
        try:
            print("🔄 重試初始化...")
            init()
        except Exception as e:
            print(f"❌ 初始化失敗: {str(e)}")
            import traceback
            traceback.print_exc()
            abort(500)
    
    # 檢查關鍵組件是否已正確初始化
    if handler is None:
        print("❌ Handler 未初始化")
        abort(500)
    
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    try:
        # 驗證簽名
        handler.parser.parse(body, signature)
        
        # 解析請求內容
        import json
        events = json.loads(body).get('events', [])
        
        for event in events:
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
        print("❌ Invalid signature error")
        abort(400)
    except Exception as e:
        print(f"❌ Webhook error: {str(e)}")
        import traceback
        traceback.print_exc()
        abort(500)

def handle_text_message(event):
    """處理文字訊息，委託給 FeatureRegistry"""
    try:
        result = feature_registry.route_text_message(event)
        return result
    except Exception as e:
        print(f"❌ handle_text_message error: {str(e)}")
        import traceback
        traceback.print_exc()
        return None

def handle_image_message(event):
    """處理圖片訊息，委託給 FeatureRegistry"""
    try:
        result = feature_registry.route_image_message(event)
        return result
    except Exception as e:
        print(f"❌ handle_image_message error: {str(e)}")
        import traceback
        traceback.print_exc()
        return None

def handle_follow_event(event):
    """處理加好友事件 - 自動建立會員並發送歡迎訊息"""
    try:
        # 取得用戶 ID
        user_id = event.get('source', {}).get('userId', '')
        if not user_id:
            print("❌ 無法取得用戶 ID")
            return None
        
        print(f"🎉 新用戶加好友: {user_id}")
        
        # 檢查是否有會員服務
        if not member_service:
            print("⚠️  會員服務未啟用，跳過自動註冊")
            return None
        
        # 檢查會員是否已存在（防止重複加好友刷點數）
        existing_member = member_service.get_member_info(user_id)
        is_new_member = existing_member is None
        
        if is_new_member:
            print(f"✨ 檢測到新會員: {user_id}")
        else:
            print(f"👋 歡迎回來！會員已存在: {user_id}")
        
        # 透過 LINE API 取得用戶資料
        try:
            profile = line_bot_api.get_profile(user_id)
            display_name = profile.display_name
            print(f"👤 用戶資料: {display_name}")
        except Exception as e:
            print(f"⚠️  無法取得用戶資料: {str(e)}")
            display_name = "使用者"

        # 建立或更新會員
        member = member_service.get_or_create_member(
            user_id=user_id,
            display_name=display_name
        )
        
        if not member:
            print("❌ 建立會員失敗")
            return None
        
        if is_new_member:
            print(f"✅ 新會員已建立: {member['display_name']}")
        else:
            print(f"✅ 會員資料已更新: {member['display_name']}")
        
        # 只對新會員贈送註冊獎勵點數
        welcome_points = int(os.getenv("WELCOME_POINTS", "0"))
        if is_new_member and welcome_points > 0:
            success = member_service.add_points(
                user_id=user_id,
                points=welcome_points,
                transaction_type='admin_add',
                description='新會員註冊獎勵'
            )
            if success:
                print(f"🎁 已贈送註冊獎勵: {welcome_points} 點")
            else:
                print("❌ 贈送註冊獎勵失敗")
        
        # 發送歡迎訊息（新舊會員不同內容）
        if is_new_member:
            welcome_message = f"""🎉 歡迎加入！

👤 會員註冊成功
📝 姓名：{member['display_name']}
💎 點數：{member['points']} 點"""

            if welcome_points > 0:
                welcome_message += f"\n🎁 註冊獎勵：+{welcome_points} 點"

            welcome_message += """

📋 使用說明：
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
• 輸入「!功能」查看功能表
• 輸入「點數」查看剩餘點數
• 輸入「圖片彩色化」處理黑白照片
• 輸入「圖片編輯」編輯照片

💡 繼續使用吧！"""

        # 發送歡迎訊息
        try:
            publisher.push_text(user_id, welcome_message)
            print("✅ 歡迎訊息已發送")
        except Exception as e:
            print(f"❌ 發送歡迎訊息失敗: {str(e)}")
        
        return None
        
    except Exception as e:
        print(f"❌ handle_follow_event error: {str(e)}")
        import traceback
        traceback.print_exc()
        return None

# 模組載入時自動初始化（適用於生產環境）
def _auto_init():
    """Auto initialize on module load if environment variables are available"""
    try:
        # 檢查是否有必要的環境變數
        if (os.getenv("CHANNEL_ACCESS_TOKEN") and 
            os.getenv("CHANNEL_SECRET") and 
            os.getenv("REPLICATE_API_TOKEN")):
            print("🔄 檢測到生產環境，開始自動初始化...")
            init()
        else:
            print("ℹ️  環境變數未完整設定，跳過自動初始化(適用於開發環境)")
    except Exception as e:
        print(f"⚠️  自動初始化失敗: {str(e)}")
        print("ℹ️  將在第一次請求時重試初始化")

# 執行自動初始化
_auto_init()

if __name__ == "__main__":
    main()
