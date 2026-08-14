"""讓老照片動起來：傳照片 → 產生 5 秒微動影片。

與其他功能的三個不同之處，都是刻意的：

1. **不讓用戶描述動作**。動作幅度越大，臉越容易崩；長輩看的是過世親人的
   照片，五官跑掉會從感動變成不安。所以固定送出一段極度克制的指令
   （在 config/settings.yml 的 animate.input.default_prompt），不給選項。
   對長輩產品而言，穩定遠比花俏重要，而且少一個要思考的步驟。

2. **一定要確認才扣點**。這是最貴的功能（預設 25 點），不能讓人手滑。

3. **輸出是影片**，所以推的是 VideoSendMessage。LINE 規定影片訊息一定要附
   縮圖網址，且由 LINE 自己在推送後去抓——所以縮圖用「用戶原本那張照片」
   的 signed URL，並且處理完不立刻刪除（見 _finish）。
"""
from linebot.models import (
    TextSendMessage, VideoSendMessage, QuickReply, QuickReplyButton, MessageAction,
)

from src.core.app_logger import get_logger
from .replicate_feature import ReplicateImageFeature

logger = get_logger("animate")

# 狀態機：waiting_image -> waiting_confirm -> processing
STATE_WAITING_IMAGE = "waiting_image"
STATE_WAITING_CONFIRM = "waiting_confirm"
STATE_PROCESSING = "processing"

CMD_CANCEL = "取消"
CMD_CONFIRM = "確定開始"

# 縮圖要撐到 LINE 來抓；一天綽綽有餘，之後由 cleanup_storage.py 回收
PREVIEW_URL_TTL_SECONDS = 86400


class AnimateFeature(ReplicateImageFeature):
    """照片動起來（輸出 5 秒影片）"""

    trigger_command = "照片動起來"
    image_waiting_state = STATE_WAITING_IMAGE

    @property
    def name(self) -> str:
        return "animate"

    # ---- Quick Reply ----

    def _confirm_quick_reply(self) -> QuickReply:
        return QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="✅ 確定開始", text=CMD_CONFIRM)),
            QuickReplyButton(action=MessageAction(label="❌ 取消", text=CMD_CANCEL)),
        ])

    def _cancel_quick_reply(self) -> QuickReply:
        return QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="❌ 取消", text=CMD_CANCEL)),
        ])

    def _reply(self, reply_token, user_id, event, text, quick_reply=None):
        self.publisher.process_reply_message(
            reply_token, TextSendMessage(text=text, quick_reply=quick_reply), user_id, event
        )

    # ---- 文字入口 ----

    def handle_text(self, event: dict) -> dict:
        user_id = self.get_user_id(event)
        reply_token = self.get_reply_token(event)
        message = self.get_message_text(event)

        try:
            if message == self.trigger_command:
                return self._handle_request(reply_token, user_id, event)

            user_state = self.get_user_state(user_id) or {}
            if user_state.get("feature") != self.name:
                return None

            current_state = user_state.get("state")
            state_data = user_state.get("data") or {}

            if message == CMD_CANCEL and current_state != STATE_PROCESSING:
                self.discard_stashed_image(state_data)
                self.clear_user_state(user_id)
                self._reply(
                    reply_token, user_id, event,
                    "好的，已經取消了，沒有扣您的點數 👌\n\n想再試試看的時候，輸入「!功能」就可以了。"
                )
                return None

            if current_state == STATE_WAITING_IMAGE:
                self._reply(
                    reply_token, user_id, event,
                    "還沒收到照片喔 📷\n\n請按輸入框旁的「＋」，選「照片」把要處理的照片傳給我。",
                    self._cancel_quick_reply()
                )
                return None

            if current_state == STATE_WAITING_CONFIRM and message == CMD_CONFIRM:
                return self._handle_confirm(reply_token, user_id, event, state_data)

        except Exception:
            logger.exception("AnimateFeature handle_text error")

        return None

    # ---- 圖片入口 ----

    def can_handle_image(self, user_id: str) -> bool:
        """等圖片時收圖；已經有圖時收圖代表「換一張」"""
        user_state = self.get_user_state(user_id) or {}
        if user_state.get("feature") != self.name:
            return False
        return user_state.get("state") in (STATE_WAITING_IMAGE, STATE_WAITING_CONFIRM)

    def handle_image(self, event: dict) -> dict:
        user_id = self.get_user_id(event)
        reply_token = self.get_reply_token(event)
        message_id = self.get_message_id(event)

        user_state = self.get_user_state(user_id) or {}
        if user_state.get("feature") != self.name:
            return None
        current_state = user_state.get("state")
        if current_state not in (STATE_WAITING_IMAGE, STATE_WAITING_CONFIRM):
            return None

        if self.reply_maintenance_if_unavailable(reply_token, user_id, event, clear_state=True):
            return None

        previous_data = user_state.get("data") or {}
        is_replacement = current_state == STATE_WAITING_CONFIRM

        try:
            image_bytes = self.download_image(message_id)
            stash = self.stash_image(image_bytes)
            if is_replacement:
                self.discard_stashed_image(previous_data)
            self._ask_to_confirm(reply_token, user_id, event, stash, is_replacement=is_replacement)
        except Exception:
            logger.exception(f"照片動起來 handle_image 失敗: {user_id}")
            self.discard_stashed_image(previous_data)
            self.clear_user_state(user_id)
            self._reply(reply_token, user_id, event, "處理圖片時發生錯誤，請稍後再試 🙏")

        return None

    def begin_from_stash(self, reply_token, user_id, event, stash: dict):
        """交棒入口：用戶先傳圖、再選「讓照片動起來」"""
        self._ask_to_confirm(reply_token, user_id, event, stash)

    # ---- 各階段 ----

    def _handle_request(self, reply_token, user_id, event) -> dict:
        if self.reply_maintenance_if_unavailable(reply_token, user_id, event):
            return None
        user_name = self.get_user_name(user_id)
        if self.reply_if_insufficient_points(reply_token, user_name, user_id, event):
            return None

        self.set_user_state(user_id, STATE_WAITING_IMAGE)
        self._reply(
            reply_token, user_id, event,
            f"{user_name} 你好！✨\n🎬 照片動起來\n\n"
            f"請傳一張照片給我，我會讓照片裡的人輕輕動起來，做成一小段影片。\n\n"
            f"💎 確認開始後才會扣 {self.required_points} 點，在那之前都可以隨時取消。",
            self._cancel_quick_reply()
        )
        return None

    def _ask_to_confirm(self, reply_token, user_id, event, stash: dict, is_replacement=False):
        self.set_user_state(user_id, STATE_WAITING_CONFIRM, stash)
        user_name = self.get_user_name(user_id)
        headline = (
            "已經換成這張新照片了！📷"
            if is_replacement
            else f"{user_name}，我已經收到您的照片了！📷✨"
        )
        self._reply(
            reply_token, user_id, event,
            f"{headline}\n\n我會讓照片裡的人輕輕動起來，做成一段約 5 秒的影片。\n\n"
            f"💎 開始後會扣 {self.required_points} 點。確定要開始嗎？",
            self._confirm_quick_reply()
        )

    def _handle_confirm(self, reply_token, user_id, event, state_data: dict) -> dict:
        user_name = self.get_user_name(user_id)
        try:
            if not self.has_stashed_image(state_data):
                self.clear_user_state(user_id)
                self._reply(reply_token, user_id, event, "找不到您上傳的照片，請重新傳一次給我 🙏")
                return None

            image_bytes = self.load_stashed_image(state_data)
            if not image_bytes:
                self.clear_user_state(user_id)
                self._reply(reply_token, user_id, event, "找不到您上傳的照片，請重新傳一次給我 🙏")
                return None

            # 縮圖沿用用戶原本那張照片。取不到網址就不做（沒有縮圖 LINE 不接受
            # 影片訊息），寧可明確告知也不要推出破圖的訊息。
            preview_url = self._preview_url(state_data, image_bytes)
            if not preview_url:
                self.discard_stashed_image(state_data)
                self.clear_user_state(user_id)
                self._reply(reply_token, user_id, event, "處理過程發生錯誤，請稍後再試 🙏")
                return None

            # 保留 image_key：它同時是影片訊息的縮圖，不能在這裡被自動清掉
            self.set_user_state(user_id, STATE_PROCESSING, dict(state_data))

            self._reply(
                reply_token, user_id, event,
                f"{user_name}，開始為您製作影片了！🎬\n\n這個比較花時間，請稍候片刻 ✨"
            )
            self.start_loading_animation(user_id)

            self.submit_billed_processing(
                user_id,
                event,
                deduct_description="照片動起來",
                run=lambda: self.run_model(image_bytes),
                build_message=lambda url: VideoSendMessage(
                    original_content_url=url,
                    preview_image_url=preview_url,
                ),
                # 縮圖要留給 LINE 事後去抓，這裡不刪；由 cleanup_storage.py 回收
                on_finish=lambda: self.clear_user_state(user_id, discard_images=False),
            )

        except Exception:
            logger.exception(f"照片動起來確認處理失敗: {user_id}")
            self.clear_user_state(user_id)
            self._reply(reply_token, user_id, event, "處理過程發生錯誤，請稍後再試 🙏")

        return None

    def _preview_url(self, state_data: dict, image_bytes: bytes):
        """影片訊息的縮圖網址；取不到回傳 None（呼叫端負責告知用戶）。

        兩條路，優先用 Storage：
          1. Storage 有設定 → signed URL（正式環境走這條）
          2. 沒設定 → 由本服務自己在 /preview/<token> 供圖（本地開發用 ngrok
             的公開網址，不必依賴任何雲端服務）
        """
        image_key = (state_data or {}).get("image_key")
        if image_key and self.storage_service:
            try:
                return self.storage_service.create_signed_url(image_key, PREVIEW_URL_TTL_SECONDS)
            except Exception:
                logger.exception("產生影片縮圖 signed URL 失敗，改用本地縮圖")

        if self.preview_store and self.preview_store.is_available():
            try:
                url = self.preview_store.save(image_bytes)
                if url and not url.startswith("https://"):
                    # LINE 明確拒收非 HTTPS 的媒體網址。與其送出去被退（用戶
                    # 白扣點），不如當場放棄——本地沒有 HTTPS 入口時就是這樣。
                    logger.warning(f"本服務的公開網址不是 HTTPS，LINE 無法使用: {url}")
                    return None
                if url:
                    logger.info("使用本地縮圖（Storage 未設定）")
                    return url
            except Exception:
                logger.exception("產生本地縮圖失敗")

        logger.warning("無法產生影片縮圖網址（Storage 未設定且取不到 HTTPS 公開網址）")
        return None
