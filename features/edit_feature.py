import os
import base64
from linebot.models import TextSendMessage

from app_logger import get_logger
from .replicate_feature import ReplicateImageFeature

logger = get_logger("edit")


class EditFeature(ReplicateImageFeature):
    """圖片編輯功能處理器（兩步驟：先上傳圖片、再輸入編輯描述）"""

    trigger_command = "圖片編輯"
    replicate_model = "google/nano-banana"
    image_waiting_state = "waiting_image"
    loading_seconds = 45  # 圖片編輯可能需要更長時間

    def __init__(self, line_bot_api, publisher, state_manager, member_service=None, storage_service=None):
        super().__init__(line_bot_api, publisher, state_manager, member_service)
        self.storage_service = storage_service
        self.required_points = int(os.getenv("EDIT_COST", "5"))

    @property
    def name(self) -> str:
        return "edit"

    def handle_text(self, event: dict) -> dict:
        """處理文字訊息"""
        user_id = self.get_user_id(event)
        reply_token = self.get_reply_token(event)
        message = self.get_message_text(event)

        try:
            if message == self.trigger_command:
                return self._handle_edit_request(reply_token, user_id, event)

            # 檢查用戶是否在等待編輯描述狀態
            if self.is_user_in_state(user_id, "waiting_description"):
                return self._handle_description_input(reply_token, user_id, message, event)

        except Exception:
            logger.exception("EditFeature handle_text error")

        return None

    def handle_image(self, event: dict) -> dict:
        """處理圖片訊息：暫存圖片，引導用戶輸入編輯描述"""
        user_id = self.get_user_id(event)
        reply_token = self.get_reply_token(event)
        message_id = self.get_message_id(event)

        logger.info(f"收到圖片訊息，用戶 ID：{user_id}")

        # 檢查用戶是否在等待圖片狀態
        if not self.is_user_in_state(user_id, self.image_waiting_state):
            # 用戶沒有確認圖片編輯，靜默處理，不發送任何回覆
            logger.debug(f"用戶 {user_id} 上傳圖片但未確認圖片編輯功能，靜默處理")
            return None

        if self.reply_maintenance_if_unavailable(reply_token, user_id, event, clear_state=True):
            return None

        user_name = self.get_user_name(user_id)

        try:
            image_bytes = self.download_image(message_id)

            # 圖片暫存至 Supabase Storage，state 只存 object key，
            # 避免整張圖 base64 塞進 JSONB 造成 row 膨脹、每次查 state 都搬整張圖
            if self.storage_service and self.storage_service.is_configured():
                image_key = self.storage_service.upload_image(image_bytes, prefix=self.name)
                state_data = {"image_key": image_key}
            else:
                logger.warning("Supabase Storage 未設定，退回以 base64 暫存於 state（建議設定 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY）")
                state_data = {"image_data": base64.b64encode(image_bytes).decode('utf-8')}

            # 設定狀態為等待編輯描述
            self.set_user_state(user_id, "waiting_description", state_data)

            self.publisher.process_reply_message(
                reply_token,
                TextSendMessage(text=f"{user_name}，我已經收到您的圖片了！📷✨\n\n請告訴我您希望如何編輯這張圖片？例如：\n• 將背景改成海灘\n• 把天空變成夕陽\n• 添加彩虹效果\n• 讓人物穿上紅色衣服\n\n請輸入您的編輯描述："),
                user_id,
                event
            )

        except Exception:
            logger.exception(f"圖片編輯 handle_image 失敗: {user_id}")
            self.clear_user_state(user_id)
            self.publisher.process_reply_message(
                reply_token,
                TextSendMessage(text="處理圖片時發生錯誤，請稍後再試 🙏"),
                user_id,
                event
            )

        return None

    def _handle_edit_request(self, reply_token: str, user_id: str, event: dict) -> dict:
        """處理圖片編輯請求：guard → 設定等待狀態 → 引導上傳"""
        if self.reply_maintenance_if_unavailable(reply_token, user_id, event):
            return None

        user_name = self.get_user_name(user_id)
        if self.reply_if_insufficient_points(reply_token, user_name, user_id, event):
            return None

        # 設定用戶狀態為等待圖片
        self.set_user_state(user_id, self.image_waiting_state)

        self.publisher.process_reply_message(
            reply_token,
            TextSendMessage(
                text=f"{user_name} 你好！✨\n🎨 圖片編輯功能\n\n💎 此功能會消耗 {self.required_points} 點點數，讓您的圖片煥然一新！\n\n請先上傳一張您想要編輯的圖片，然後我會請您描述想要的編輯效果 🖼️"
            ),
            user_id,
            event
        )
        return None

    def _handle_description_input(self, reply_token: str, user_id: str, description: str, event: dict) -> dict:
        """處理編輯描述輸入：取出暫存圖片 → 背景計費處理"""
        user_name = self.get_user_name(user_id)
        try:
            # 獲取暫存的圖片（Storage key 或退回路徑的 base64）
            user_state = self.get_user_state(user_id)
            state_data = (user_state or {}).get("data") or {}
            image_key = state_data.get("image_key")
            image_data = state_data.get("image_data")

            if not image_key and not image_data:
                self.clear_user_state(user_id)
                self.publisher.process_reply_message(
                    reply_token,
                    TextSendMessage(text="找不到您上傳的圖片，請重新開始圖片編輯流程。"),
                    user_id,
                    event
                )
                return None

            # 先取回圖片再回覆，取不回來時外層 except 還能用 reply_token 告知用戶
            if image_key:
                try:
                    image_bytes = self.storage_service.download_image(image_key)
                finally:
                    # 用過即刪；下載失敗時物件也已無用，一併清掉
                    self.storage_service.delete_image(image_key)
            else:
                image_bytes = base64.b64decode(image_data)

            # 設定狀態為正在處理（圖片已在記憶體中，不再重複存入 DB）
            self.set_user_state(user_id, "processing", {"description": description})

            self.publisher.process_reply_message(
                reply_token,
                TextSendMessage(text=f"{user_name}，我已經收到您的編輯需求！🎨\n\n編輯描述：「{description}」\n\n正在為您精心處理中，請稍候片刻 ✨"),
                user_id,
                event
            )
            self.start_loading_animation(user_id)

            self.submit_billed_processing(
                user_id,
                event,
                deduct_description=f"圖片編輯：{description[:20]}",
                run=lambda: self.run_replicate({
                    "prompt": description,
                    "image_input": [self.image_to_data_url(image_bytes)],
                    "output_format": "jpg",
                }),
            )

        except Exception:
            logger.exception(f"圖片編輯描述處理失敗: {user_id}")
            self.clear_user_state(user_id)
            self.publisher.process_reply_message(
                reply_token,
                TextSendMessage(text="處理過程發生錯誤，請稍後再試 🙏"),
                user_id,
                event
            )

        return None
