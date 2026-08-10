import os
from linebot.models import TextSendMessage

from app_logger import get_logger
from .replicate_feature import ReplicateImageFeature

logger = get_logger("colorize")


class ColorizeFeature(ReplicateImageFeature):
    """圖片彩色化功能處理器"""

    trigger_command = "圖片彩色化"
    replicate_model = "flux-kontext-apps/restore-image"
    image_waiting_state = "waiting"
    loading_seconds = 30

    def __init__(self, line_bot_api, publisher, state_manager, member_service=None, storage_service=None):
        super().__init__(line_bot_api, publisher, state_manager, member_service, storage_service)
        self.required_points = int(os.getenv("COLORIZE_COST", "10"))

    @property
    def name(self) -> str:
        return "colorize"

    def handle_text(self, event: dict) -> dict:
        """處理文字訊息"""
        user_id = self.get_user_id(event)
        reply_token = self.get_reply_token(event)
        message = self.get_message_text(event)

        try:
            if message == self.trigger_command:
                return self._handle_colorize_request(reply_token, user_id, event)
        except Exception:
            logger.exception("ColorizeFeature handle_text error")

        return None

    def handle_image(self, event: dict) -> dict:
        """處理圖片訊息：下載 → 回覆已收到 → 背景計費處理"""
        user_id = self.get_user_id(event)
        reply_token = self.get_reply_token(event)
        message_id = self.get_message_id(event)

        logger.info(f"收到圖片訊息，用戶 ID：{user_id}")

        # 檢查用戶是否在等待彩色化狀態
        if not self.is_user_in_state(user_id, self.image_waiting_state):
            # 用戶沒有確認彩色化，靜默處理，不發送任何回覆
            logger.debug(f"用戶 {user_id} 上傳圖片但未確認彩色化功能，靜默處理")
            return None

        if self.reply_maintenance_if_unavailable(reply_token, user_id, event, clear_state=True):
            return None

        try:
            image_bytes = self.download_image(message_id)
        except Exception:
            logger.exception(f"彩色化下載圖片失敗: {user_id}")
            self.clear_user_state(user_id)
            self.publisher.process_reply_message(
                reply_token,
                TextSendMessage(text="處理圖片時發生錯誤，請稍後再試 🙏"),
                user_id,
                event
            )
            return None

        self._start_processing(reply_token, user_id, event, image_bytes)
        return None

    def begin_from_stash(self, reply_token, user_id, event, stash: dict):
        """交棒入口：用戶先傳圖、再選「幫我上色」"""
        try:
            image_bytes = self.load_stashed_image(stash)
        finally:
            # 用過即刪；下載失敗時物件也已無用，一併清掉
            self.discard_stashed_image(stash)

        if not image_bytes:
            raise ValueError("stash 中沒有可用的圖片")

        self._start_processing(reply_token, user_id, event, image_bytes)

    def _start_processing(self, reply_token: str, user_id: str, event: dict, image_bytes: bytes):
        """回覆已收到 → 背景計費處理（兩種入口共用的尾段）"""
        user_name = self.get_user_name(user_id)
        try:
            # 設定狀態為正在彩色化
            self.set_user_state(user_id, "processing")

            self.publisher.process_reply_message(
                reply_token,
                TextSendMessage(text=f"{user_name}，我已經收到您的珍貴照片了！✨ 正在為您精心處理中，請稍候片刻 🌟"),
                user_id,
                event
            )
            self.start_loading_animation(user_id)

            self.submit_billed_processing(
                user_id,
                event,
                deduct_description="彩色化圖片",
                run=lambda: self.run_replicate({
                    "input_image": self.image_to_data_url(image_bytes),
                }),
            )

        except Exception:
            logger.exception(f"彩色化處理失敗: {user_id}")
            self.clear_user_state(user_id)
            self.publisher.process_reply_message(
                reply_token,
                TextSendMessage(text="處理圖片時發生錯誤，請稍後再試 🙏"),
                user_id,
                event
            )

    def _handle_colorize_request(self, reply_token: str, user_id: str, event: dict) -> dict:
        """處理彩色化請求：guard → 設定等待狀態 → 引導上傳"""
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
                text=f"{user_name} 你好！✨\n🎨 圖片彩色化功能\n\n💎 此功能會消耗 {self.required_points} 點點數，讓您的珍貴回憶重現色彩！\n\n請上傳一張黑白照片，我將為您進行彩色化處理，讓回憶重新綻放光彩 🌈"
            ),
            user_id,
            event
        )
        return None
