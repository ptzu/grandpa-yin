"""做完一件事之後的下一步。

在此之前，成品推出去、狀態就被清掉，對話到此結束。用戶想拿剛做好的照片再做
點什麼——把上好色的全家福做成影片、把修好的照片再修一下——只能從頭再傳一次
照片。長輩最不擅長的就是那個「從頭再來」。

這個功能把成品接住：推送成品時附上 Quick Reply，並記下成品的網址。用戶按了
按鈕，就把成品抓回來當成新的輸入，交棒給對應的功能（跟 photo_intent 交棒給
colorize / edit 是同一套機制）。

兩個刻意的設計：

1. **只記網址，不記 image_key**。成品放在 Storage 的 results/ 底下要保留
   30 天，而 state 裡的 `image_key` 會在狀態轉換時被自動回收
   （見 BaseFeature._discard_superseded_image）。成品若寫進那個欄位，
   用戶點下一步的瞬間就會把自己的成品刪掉。交棒時是「把成品複製一份成新的
   暫存圖」，原件不動。

2. **can_handle 只認自己的按鈕文字**。這個狀態會在對話裡留著（沒有明確的
   結束動作），若比照其他功能用「狀態屬於我就全收」，會把用戶接下來想對別的
   功能講的話一併吃掉。認不得的文字一律放行，讓路由繼續往下找。
"""
from linebot.models import TextSendMessage, QuickReply, QuickReplyButton, MessageAction

from src.core.app_logger import get_logger
from .base_feature import BaseFeature, _UNSET

logger = get_logger("followup")

STATE_OFFERED = "offered"

CMD_DONE = "不用了"

# 按鈕送出的文字 -> 接手的功能名稱。成品（圖片）會成為該功能的輸入。
CHOICES = {
    "做成影片": "animate",
    "再修一下": "edit",
}

# 按鈕在選單上的順序與圖示
CHOICE_ICONS = {
    "做成影片": "🎬",
    "再修一下": "🎨",
}


class FollowUpFeature(BaseFeature):
    """把剛做好的成品接住，讓用戶不必重傳就能繼續下一步"""

    @property
    def name(self) -> str:
        return "followup"

    # ---- 路由判斷 ----

    def can_handle(self, message: str, user_id: str, user_state=_UNSET) -> bool:
        """只接自己的按鈕文字（其餘一律放行，見模組說明第 2 點）"""
        message = message.strip()
        if message != CMD_DONE and message not in CHOICES:
            return False
        state = self.resolve_user_state(user_id, user_state)
        return bool(state and state.get("feature") == self.name)

    # ---- 由 ReplicateImageFeature 在推送成品時呼叫 ----

    def build_offer(self, user_id: str):
        """成品後面附的那則訊息；沒有可接續的功能時回傳 None。

        另開一則而不是把 Quick Reply 掛在圖片上：長輩的視線停在照片上，
        掛在媒體訊息上的按鈕很容易整排被忽略。
        """
        items = self._choice_buttons()
        if not items:
            return None

        items.append(QuickReplyButton(action=MessageAction(label="👌 不用了", text=CMD_DONE)))
        return TextSendMessage(
            text=(
                "做好了。\n\n"
                "想傳給家人的話，長按照片就可以轉傳。\n\n"
                "還要再做點什麼嗎？"
            ),
            quick_reply=QuickReply(items=items),
        )

    def remember(self, user_id: str, result_url: str):
        """記下成品的網址，讓用戶按按鈕時不必重傳照片"""
        self.set_user_state(user_id, STATE_OFFERED, {"result_url": result_url})

    # ---- 文字入口（用戶按了按鈕） ----

    def handle_text(self, event: dict) -> dict:
        user_id = self.get_user_id(event)
        reply_token = self.get_reply_token(event)
        message = self.get_message_text(event).strip()

        try:
            user_state = self.get_user_state(user_id) or {}
            if user_state.get("feature") != self.name:
                return None

            state_data = user_state.get("data") or {}

            if message == CMD_DONE:
                self.clear_user_state(user_id)
                self._reply(
                    reply_token, user_id, event,
                    "好的。\n\n想做的時候再把照片傳給我就好。"
                )
                return None

            target = self._resolve_target(message)
            if not target:
                return None

            image_bytes = self._load_result(state_data)
            if not image_bytes:
                # 成品過了保存期、或當初根本沒存成功（推的是模型端的暫存網址）
                self.clear_user_state(user_id)
                self._reply(
                    reply_token, user_id, event,
                    "不好意思，剛才那張我這邊留不住了。\n\n麻煩把照片再傳一次給我。"
                )
                return None

            stash = self.stash_image(image_bytes)

            logger.info(f"成品交棒給功能 {target.name}: {user_id}")
            if not target.accept_handoff(reply_token, user_id, event, stash):
                # guard 擋下（維護中／點數不足），目標功能已回覆原因，這裡負責收拾。
                # 刪的是剛複製出來的暫存圖，results/ 裡的成品不受影響。
                self.discard_stashed_image(stash)
                self.clear_user_state(user_id)

        except Exception:
            logger.exception("FollowUpFeature handle_text error")

        return None

    # ---- 內部工具 ----

    def _choice_buttons(self) -> list:
        """依實際註冊到的功能產生按鈕（點數從設定檔讀，才不會跟扣款對不上）"""
        items = []
        for text, feature_name in CHOICES.items():
            target = self._resolve_target(text)
            if not target:
                continue
            label = f"{CHOICE_ICONS[text]} {text}（{target.required_points} 點）"
            items.append(QuickReplyButton(action=MessageAction(label=label, text=text)))
        return items

    def _resolve_target(self, message: str):
        feature_name = CHOICES.get(message)
        if not feature_name or not self.registry:
            return None
        return self.registry.get_feature_by_name(feature_name)

    def _load_result(self, state_data: dict):
        if not self.result_archive:
            return None
        return self.result_archive.fetch((state_data or {}).get("result_url"))

    def _reply(self, reply_token: str, user_id: str, event: dict, text: str):
        self.publisher.process_reply_message(
            reply_token, TextSendMessage(text=text), user_id, event
        )
