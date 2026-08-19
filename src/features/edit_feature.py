from linebot.models import TextSendMessage, QuickReply, QuickReplyButton, MessageAction

from src.core.app_logger import get_logger
from .replicate_feature import ReplicateImageFeature

logger = get_logger("edit")

# 狀態機：waiting_image -> waiting_description -> waiting_confirm -> processing
STATE_WAITING_IMAGE = "waiting_image"
STATE_WAITING_DESCRIPTION = "waiting_description"
STATE_WAITING_CONFIRM = "waiting_confirm"
STATE_PROCESSING = "processing"

# 有圖在手、可以接受新描述的狀態
STATES_WITH_IMAGE = (STATE_WAITING_DESCRIPTION, STATE_WAITING_CONFIRM)

# Quick Reply 按鈕送出的指令文字
CMD_CANCEL = "取消"
CMD_CUSTOM_DESCRIPTION = "我自己描述"
CMD_CONFIRM = "確定開始"
CMD_REDO_DESCRIPTION = "重新描述"

# 預設編輯描述：按鈕文字即描述本身，長輩不必打字就能完成整套流程
PRESET_DESCRIPTIONS = [
    ("🏖️ 背景換成海灘", "背景換成海灘"),
    ("🌅 天空變成夕陽", "天空變成夕陽"),
    ("🌸 加上花朵背景", "加上花朵背景"),
    ("👕 衣服變成紅色", "衣服變成紅色"),
    ("✨ 讓照片更清晰", "讓照片更清晰"),
]


class EditFeature(ReplicateImageFeature):
    """圖片編輯功能處理器

    流程：上傳圖片 → 選（或輸入）編輯描述 → 確認扣點 → 背景處理。
    圖片可在描述／確認階段隨時換掉，描述也可以重來，確認前都不扣點。
    """

    trigger_command = "圖片編輯"
    image_waiting_state = STATE_WAITING_IMAGE

    @property
    def name(self) -> str:
        return "edit"

    # ---- Quick Reply 建材 ----

    def _description_quick_reply(self) -> QuickReply:
        """預設描述選項 + 自行輸入 + 取消"""
        items = [
            QuickReplyButton(action=MessageAction(label=label, text=text))
            for label, text in PRESET_DESCRIPTIONS
        ]
        items.append(QuickReplyButton(action=MessageAction(label="✏️ 我自己描述", text=CMD_CUSTOM_DESCRIPTION)))
        items.append(QuickReplyButton(action=MessageAction(label="❌ 取消", text=CMD_CANCEL)))
        return QuickReply(items=items)

    def _confirm_quick_reply(self) -> QuickReply:
        return QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="✅ 確定開始", text=CMD_CONFIRM)),
            QuickReplyButton(action=MessageAction(label="✏️ 重新描述", text=CMD_REDO_DESCRIPTION)),
            QuickReplyButton(action=MessageAction(label="❌ 取消", text=CMD_CANCEL)),
        ])

    def _cancel_quick_reply(self) -> QuickReply:
        return QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="❌ 取消", text=CMD_CANCEL)),
        ])

    def _reply(self, reply_token: str, user_id: str, event: dict, text: str, quick_reply=None):
        self.publisher.process_reply_message(
            reply_token,
            TextSendMessage(text=text, quick_reply=quick_reply),
            user_id,
            event
        )

    # ---- 文字入口 ----

    def handle_text(self, event: dict) -> dict:
        """處理文字訊息"""
        user_id = self.get_user_id(event)
        reply_token = self.get_reply_token(event)
        message = self.get_message_text(event)

        try:
            if message == self.trigger_command:
                return self._handle_edit_request(reply_token, user_id, event)

            # 狀態只查一次，後續分支共用
            user_state = self.get_user_state(user_id) or {}
            if user_state.get("feature") != self.name:
                return None

            current_state = user_state.get("state")
            state_data = user_state.get("data") or {}

            if message == CMD_CANCEL and current_state != STATE_PROCESSING:
                return self._handle_cancel(reply_token, user_id, event, state_data)

            if current_state == STATE_WAITING_IMAGE:
                # 等圖片時收到文字：明確引導，而不是靜默丟棄
                self._reply(
                    reply_token, user_id, event,
                    "還沒收到照片喔。\n\n請按輸入框旁邊的「＋」，選「照片」傳給我。",
                    self._cancel_quick_reply()
                )
                return None

            if current_state == STATE_WAITING_DESCRIPTION:
                if message == CMD_CUSTOM_DESCRIPTION:
                    self._reply(
                        reply_token, user_id, event,
                        "好，請打字告訴我想怎麼修改。\n例如：「把背景換成公園」、「加上一頂帽子」。",
                        self._cancel_quick_reply()
                    )
                    return None
                return self._handle_description_input(reply_token, user_id, event, state_data, message)

            if current_state == STATE_WAITING_CONFIRM:
                if message == CMD_CONFIRM:
                    return self._handle_confirm(reply_token, user_id, event, state_data)
                if message == CMD_REDO_DESCRIPTION:
                    return self._handle_redo_description(reply_token, user_id, event, state_data)
                # 沒點按鈕而是直接又打了一段描述：當成改描述，重新確認
                return self._handle_description_input(reply_token, user_id, event, state_data, message)

        except Exception:
            logger.exception("EditFeature handle_text error")

        return None

    # ---- 圖片入口 ----

    def can_handle_image(self, user_id: str) -> bool:
        """等圖片時收圖；已經有圖時收圖代表「換一張」"""
        user_state = self.get_user_state(user_id) or {}
        if user_state.get("feature") != self.name:
            return False
        return user_state.get("state") in (STATE_WAITING_IMAGE,) + STATES_WITH_IMAGE

    def handle_image(self, event: dict) -> dict:
        """處理圖片訊息：暫存圖片，引導用戶選擇編輯描述"""
        user_id = self.get_user_id(event)
        reply_token = self.get_reply_token(event)
        message_id = self.get_message_id(event)

        logger.info(f"收到圖片訊息，用戶 ID：{user_id}")

        user_state = self.get_user_state(user_id) or {}
        if user_state.get("feature") != self.name:
            return None

        current_state = user_state.get("state")
        if current_state not in (STATE_WAITING_IMAGE,) + STATES_WITH_IMAGE:
            return None

        if self.reply_maintenance_if_unavailable(reply_token, user_id, event, clear_state=True):
            return None

        previous_data = user_state.get("data") or {}
        is_replacement = current_state in STATES_WITH_IMAGE

        try:
            image_bytes = self.download_image(message_id)
            stash = self.stash_image(image_bytes)

            # 換圖時舊的暫存檔已無用，避免留下孤兒物件
            if is_replacement:
                self.discard_stashed_image(previous_data)

            self._ask_for_description(reply_token, user_id, event, stash, is_replacement=is_replacement)

        except Exception:
            logger.exception(f"圖片編輯 handle_image 失敗: {user_id}")
            self.discard_stashed_image(previous_data)
            self.clear_user_state(user_id)
            self._reply(reply_token, user_id, event, "處理圖片時發生錯誤，請稍後再試 🙏")

        return None

    def begin_from_stash(self, reply_token, user_id, event, stash: dict):
        """交棒入口：用戶先傳圖、再選「依描述修改」"""
        self._ask_for_description(reply_token, user_id, event, stash)

    # ---- 各階段 ----

    def _handle_edit_request(self, reply_token: str, user_id: str, event: dict) -> dict:
        """處理圖片編輯請求：guard → 設定等待狀態 → 引導上傳"""
        if self.reply_maintenance_if_unavailable(reply_token, user_id, event):
            return None

        user_name = self.get_user_name(user_id)
        if self.reply_if_insufficient_points(reply_token, user_name, user_id, event):
            return None

        self.set_user_state(user_id, STATE_WAITING_IMAGE)

        self._reply(
            reply_token, user_id, event,
            f"{user_name}，請先傳一張照片給我，\n"
            f"接著我會給您幾個修改的選項。\n\n"
            f"確定要改之後才會扣 {self.required_points} 點，在那之前都可以取消。",
            self._cancel_quick_reply()
        )
        return None

    def _ask_for_description(self, reply_token: str, user_id: str, event: dict, stash: dict, is_replacement: bool = False):
        """圖片已暫存，請用戶選擇或輸入編輯描述"""
        self.set_user_state(user_id, STATE_WAITING_DESCRIPTION, stash)

        user_name = self.get_user_name(user_id)
        headline = (
            "好，換成這張新的了。"
            if is_replacement
            else f"{user_name}，照片收到了。"
        )

        self._reply(
            reply_token, user_id, event,
            f"{headline}\n\n想怎麼修改呢？下面點一個就好，也可以自己打字告訴我。",
            self._description_quick_reply()
        )

    def _handle_description_input(self, reply_token: str, user_id: str, event: dict, state_data: dict, description: str) -> dict:
        """收到編輯描述 → 進入確認階段（此時仍未扣點）"""
        if not self.has_stashed_image(state_data):
            self.clear_user_state(user_id)
            self._reply(reply_token, user_id, event, "找不到剛才那張照片，麻煩重新開始一次。")
            return None

        # 保留圖片位置，附加描述
        next_data = dict(state_data)
        next_data["description"] = description
        self.set_user_state(user_id, STATE_WAITING_CONFIRM, next_data)

        self._reply(
            reply_token, user_id, event,
            f"我會這樣改：\n\n「{description}」\n\n"
            f"開始之後會扣 {self.required_points} 點，確定嗎？",
            self._confirm_quick_reply()
        )
        return None

    def _handle_redo_description(self, reply_token: str, user_id: str, event: dict, state_data: dict) -> dict:
        """退回描述階段，圖片保留不動"""
        next_data = {k: v for k, v in state_data.items() if k != "description"}
        self.set_user_state(user_id, STATE_WAITING_DESCRIPTION, next_data)

        self._reply(
            reply_token, user_id, event,
            "好，照片我留著。\n\n請重新告訴我想怎麼修改。",
            self._description_quick_reply()
        )
        return None

    def _handle_cancel(self, reply_token: str, user_id: str, event: dict, state_data: dict) -> dict:
        """取消流程：丟棄暫存圖片與狀態，未扣任何點數"""
        self.discard_stashed_image(state_data)
        self.clear_user_state(user_id)
        self._reply(
            reply_token, user_id, event,
            "好，取消了，沒有扣點。\n\n想用的時候再輸入「功能」就可以。"
        )
        return None

    def _handle_confirm(self, reply_token: str, user_id: str, event: dict, state_data: dict) -> dict:
        """用戶確認 → 取出暫存圖片 → 背景計費處理"""
        user_name = self.get_user_name(user_id)
        description = state_data.get("description")

        try:
            if not description or not self.has_stashed_image(state_data):
                self.clear_user_state(user_id)
                self._reply(reply_token, user_id, event, "找不到剛才那張照片，麻煩重新開始一次。")
                return None

            # 先取回圖片再回覆，取不回來時外層 except 還能用 reply_token 告知用戶
            try:
                image_bytes = self.load_stashed_image(state_data)
            finally:
                # 用過即刪；下載失敗時物件也已無用，一併清掉
                self.discard_stashed_image(state_data)

            # 設定狀態為正在處理（圖片已在記憶體中，不再重複存入 DB）
            self.set_user_state(user_id, STATE_PROCESSING, {"description": description})

            self._reply(
                reply_token, user_id, event,
                f"好，開始處理了。\n\n要改的是：「{description}」\n\n請稍等一下。"
            )
            self.start_loading_animation(user_id)

            self.submit_billed_processing(
                user_id,
                event,
                deduct_description=f"圖片編輯：{description[:20]}",
                run=lambda: self.run_model(image_bytes, prompt=description),
            )

        except Exception:
            logger.exception(f"圖片編輯確認處理失敗: {user_id}")
            self.clear_user_state(user_id)
            self._reply(reply_token, user_id, event, "處理的時候出了點問題，麻煩晚一點再試。")

        return None
