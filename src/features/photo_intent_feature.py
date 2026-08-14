from linebot.models import TextSendMessage, QuickReply, QuickReplyButton, MessageAction

from src.core.app_logger import get_logger
from .base_feature import BaseFeature, _UNSET
from .feature_registry import is_global_command
from .replicate_feature import MAINTENANCE_MESSAGE

logger = get_logger("photo_intent")

STATE_WAITING_CHOICE = "waiting_choice"

CMD_CANCEL = "取消"

# 按鈕送出的文字 -> 接手的功能名稱
CHOICES = {
    "幫照片上色": "colorize",
    "照我說的修改": "edit",
    "讓照片動起來": "animate",
}


class PhotoIntentFeature(BaseFeature):
    """「先傳照片、再問要做什麼」的接住器。

    長輩的心智模型是「我要處理這張照片」，而不是「我要進入某個功能模式」。
    在此之前，沒有先輸入功能指令就上傳的照片會被靜默丟棄，用戶完全收不到
    任何回饋。這個功能作為圖片路由的 catch-all：暫存照片，用 Quick Reply
    問清楚意圖，再把照片交棒給真正的功能（見 accept_handoff）。

    本身不扣點；點數與維護 guard 由接手的功能負責。
    必須最後註冊，才不會搶在其他功能之前接走圖片。
    """

    @property
    def name(self) -> str:
        return "photo_intent"

    # ---- 路由判斷 ----

    def can_handle(self, message: str, user_id: str, user_state=_UNSET) -> bool:
        """只處理「照片已暫存、等用戶選功能」狀態下的文字"""
        if is_global_command(message):
            return False
        state = self.resolve_user_state(user_id, user_state)
        return bool(state and state.get("feature") == self.name)

    def can_handle_image(self, user_id: str) -> bool:
        """catch-all：其他功能都接不住的照片，由這裡接住"""
        return True

    # ---- 圖片入口 ----

    def handle_image(self, event: dict) -> dict:
        user_id = self.get_user_id(event)
        reply_token = self.get_reply_token(event)
        message_id = self.get_message_id(event)

        # 群組裡的照片不主動搭話，避免對每張貼圖照片都跳出選單
        if self.is_group_chat(event):
            logger.debug("群組聊天中的圖片，不進行意圖詢問")
            return None

        if not self.member_service:
            self._reply(reply_token, user_id, event, MAINTENANCE_MESSAGE)
            return None

        previous_state = self.get_user_state(user_id) or {}
        is_own_state = previous_state.get("feature") == self.name
        previous_data = previous_state.get("data") if is_own_state else None

        # 上一張還在跑就別覆蓋狀態，否則背景任務結束時會把這裡的選單狀態一起清掉
        if not is_own_state and previous_state.get("state") == "processing":
            self._reply(
                reply_token, user_id, event,
                "上一張照片還在處理中，好了會馬上傳給您 ⏳\n\n這張請稍等一下再傳給我 🙏"
            )
            return None

        try:
            image_bytes = self.download_image(message_id)
            stash = self.stash_image(image_bytes)

            # 連續傳好幾張時只留最後一張，舊的暫存檔即時清掉
            if previous_data:
                self.discard_stashed_image(previous_data)

            self.set_user_state(user_id, STATE_WAITING_CHOICE, stash)

            user_name = self.get_user_name(user_id)
            self._reply(
                reply_token, user_id, event,
                f"{user_name}，收到您的照片了！📷\n\n請問想要我幫您做什麼呢？\n點下面的按鈕就可以 👇",
                self._choice_quick_reply()
            )

        except Exception:
            logger.exception(f"照片意圖詢問失敗: {user_id}")
            self.clear_user_state(user_id)
            self._reply(reply_token, user_id, event, "處理圖片時發生錯誤，請稍後再試 🙏")

        return None

    # ---- 文字入口（用戶做出選擇） ----

    def handle_text(self, event: dict) -> dict:
        user_id = self.get_user_id(event)
        reply_token = self.get_reply_token(event)
        message = self.get_message_text(event)

        try:
            user_state = self.get_user_state(user_id) or {}
            if user_state.get("feature") != self.name:
                return None

            state_data = user_state.get("data") or {}

            if message == CMD_CANCEL:
                self.discard_stashed_image(state_data)
                self.clear_user_state(user_id)
                self._reply(
                    reply_token, user_id, event,
                    "好的，先不處理這張照片 👌\n\n需要的時候再傳一次給我就可以了。"
                )
                return None

            target = self._resolve_target(message)
            if not target:
                # 沒看懂：把選項再問一次，而不是讓用戶對著沒反應的畫面猜
                self._reply(
                    reply_token, user_id, event,
                    "不好意思，我不太確定您的意思 🙇\n\n請直接點下面的按鈕選一個 👇",
                    self._choice_quick_reply()
                )
                return None

            if not self.has_stashed_image(state_data):
                self.clear_user_state(user_id)
                self._reply(reply_token, user_id, event, "找不到剛才的照片，請重新傳一次給我 🙏")
                return None

            # 交棒：由目標功能跑自己的 guard 並接手狀態
            logger.info(f"照片交棒給功能 {target.name}: {user_id}")
            if not target.accept_handoff(reply_token, user_id, event, state_data):
                # guard 擋下（維護中／點數不足），目標功能已回覆原因，這裡負責收拾
                self.discard_stashed_image(state_data)
                self.clear_user_state(user_id)

        except Exception:
            logger.exception("PhotoIntentFeature handle_text error")

        return None

    # ---- 內部工具 ----

    def _resolve_target(self, message: str):
        """把用戶的選擇對應到接手的功能；也接受該功能原本的觸發指令"""
        if not self.registry:
            return None

        feature_name = CHOICES.get(message)
        if feature_name:
            return self.registry.get_feature_by_name(feature_name)

        for feature in self.registry.get_all_features():
            if getattr(feature, "trigger_command", None) == message:
                return feature

        return None

    def _choice_quick_reply(self) -> QuickReply:
        return QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="📸 幫照片上色", text="幫照片上色")),
            QuickReplyButton(action=MessageAction(label="🎬 讓照片動起來", text="讓照片動起來")),
            QuickReplyButton(action=MessageAction(label="🎨 照我說的修改", text="照我說的修改")),
            QuickReplyButton(action=MessageAction(label="❌ 取消", text=CMD_CANCEL)),
        ])

    def _reply(self, reply_token: str, user_id: str, event: dict, text: str, quick_reply=None):
        self.publisher.process_reply_message(
            reply_token,
            TextSendMessage(text=text, quick_reply=quick_reply),
            user_id,
            event
        )
