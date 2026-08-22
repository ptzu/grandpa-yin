from src.core.app_logger import get_logger
from src.core.settings import get_model_config
from .base_feature import BaseFeature
from linebot.models import TextSendMessage, QuickReply, QuickReplyButton, MessageAction

logger = get_logger("menu")


class MenuFeature(BaseFeature):
    """功能選單處理器"""
    
    @property
    def name(self) -> str:
        return "menu"
    
    def can_handle(self, message: str, user_id: str, user_state=None) -> bool:
        """處理功能選單相關的訊息"""
        return message.strip() in ("功能", "使用說明")
    
    def handle_text(self, event: dict) -> dict:
        """處理文字訊息"""
        user_id = self.get_user_id(event)
        reply_token = self.get_reply_token(event)
        message = self.get_message_text(event)
        user_name = self.get_user_name(user_id)
        
        try:
            if message == "功能":
                return self._handle_main_menu(reply_token, user_name, user_id, event)
            elif message == "使用說明":
                return self._handle_help(reply_token, user_name, user_id, event)
                
        except Exception:
            logger.exception("MenuFeature handle_text error")
        
        return None
    
    def _handle_main_menu(self, reply_token: str, user_name: str, user_id: str, event: dict) -> dict:
        """處理主功能選單"""
        # 只留三個圖片功能與會員中心；點數／儲值／使用說明都收進會員中心裡
        quick_reply_buttons = [
            QuickReplyButton(action=MessageAction(label="📸 修復老照片", text="修復老照片")),
            QuickReplyButton(action=MessageAction(label="🎬 照片動起來", text="照片動起來")),
            QuickReplyButton(action=MessageAction(label="🎨 P圖大神", text="P圖大神")),
            QuickReplyButton(action=MessageAction(label="👤 會員中心", text="會員中心")),
        ]

        quick_reply = QuickReply(items=quick_reply_buttons)
        
        result = self.publisher.process_reply_message(
            reply_token,
            TextSendMessage(
                text="想做什麼呢？",
                quick_reply=quick_reply
            ),
            user_id,
            event  # 傳遞 event 以支援群組聊天
        )
        return result
    
    def _handle_help(self, reply_token: str, user_name: str, user_id: str, event: dict) -> dict:
        """處理使用說明"""
        # 讀同一份設定，說明文案才不會跟實際扣款金額對不上
        colorize_cost = get_model_config("colorize").cost
        edit_cost = get_model_config("edit").cost
        animate_cost = get_model_config("animate").cost
        gift_help = ("\n\n朋友送您點數的話，點一下他傳來的卡片就會自動加進來。"
                     if self.gift_card_service else "")
        help_message = f"""我會做這三件事：

📸 修復老照片（{colorize_cost} 點）
把泛黃或黑白的老照片變成彩色的。

🎬 照片動起來（{animate_cost} 點）
讓照片裡的人動起來，做成一段大約 5 秒的影片。

🎨 P圖大神（{edit_cost} 點）
照您說的修改照片，例如換背景、換衣服顏色。
可以從選單點，也可以自己打字說。

最簡單的用法是直接把照片傳給我，我會問您想做什麼。

想知道還有多少點，輸入「會員中心」就可以了。{gift_help}"""
        
        result = self.publisher.process_reply_message(
            reply_token,
            TextSendMessage(text=help_message),
            user_id,
            event  # 傳遞 event 以支援群組聊天
        )
        return result
    
    
