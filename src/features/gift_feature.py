import os

from linebot.models import TextSendMessage, QuickReply, QuickReplyButton, MessageAction

from src.core.app_logger import get_logger
from src.features.base_feature import BaseFeature
from src.services import gift_card_service as gift_codes

logger = get_logger("gift")

COMMAND = "兌換"
CANCEL = "取消"

STATE_WAITING_CODE = "waiting_code"

# After this many wrong codes in a row, stop repeating the same hint and point
# them back at the person who sent the card — an elderly user who has mistyped
# three times is usually reading the wrong thing, not typing it wrong.
_MAX_FAILS = 3


class GiftFeature(BaseFeature):
    """禮物卡兌換：朋友買、長輩兌換。

    買卡在網頁上（見 app.py 的 /gift），這裡只負責把卡號換成點數。
    """

    @property
    def name(self) -> str:
        return "gift"

    def can_handle(self, message: str, user_id: str, user_state=None) -> bool:
        message = (message or "").strip()

        # 「兌換」或「兌換 ABCD-EFGH」都收
        if message == COMMAND or message.startswith(COMMAND):
            return True

        # 等卡號時，任何文字都當成卡號（全局命令在路由層已先被攔下，
        # 所以「功能」「點數」仍然隨時逃得掉）
        state = self.resolve_user_state(user_id, user_state)
        return bool(state and state.get("feature") == self.name
                    and state.get("state") == STATE_WAITING_CODE)

    def handle_text(self, event: dict) -> dict:
        user_id = self.get_user_id(event)
        reply_token = self.get_reply_token(event)
        message = self.get_message_text(event).strip()

        try:
            if message == CANCEL:
                self.clear_user_state(user_id)
                return self._reply(reply_token, user_id, event, "好的，不兌換了。")

            if message.startswith(COMMAND):
                # 「兌換 ABCD-EFGH」一次到位；只打「兌換」就問卡號
                rest = message[len(COMMAND):].strip()
                if rest:
                    return self._redeem(user_id, reply_token, event, rest)
                return self._ask_for_code(user_id, reply_token, event)

            return self._redeem(user_id, reply_token, event, message)

        except Exception:
            logger.exception(f"處理兌換失敗: {user_id}")
            self.clear_user_state(user_id)
            return self._reply(reply_token, user_id, event,
                               "現在沒辦法兌換，麻煩晚一點再試。")

    # ------------------------------------------------------------------ 推播

    def notify_gift_ready_to_send(self, buyer_uid: str, points: int,
                                  order_no: str) -> bool:
        """提醒買家：禮物已備好但還沒送出，點連結回到分享頁選朋友。

        用在「付完款、還沒選朋友就關頁」的情況——卡已開立卻卡在資料庫裡，買家
        看不到卡號也沒有重新送出的入口。這則訊息就是那個入口。
        """
        liff_id = os.getenv("LIFF_ID", "")
        if not liff_id:
            return False
        link = f"https://liff.line.me/{liff_id}?p=share&no={order_no}"
        message = TextSendMessage(
            text=(f"🎁 您買的 {points} 點禮物已經準備好了，還沒送出喔。\n\n"
                  f"點下面的連結，選一位朋友送出：\n{link}"),
        )
        return self.publisher.process_push_message(buyer_uid, message)

    def notify_gift_received(self, user_id: str, points: int, balance: int) -> bool:
        """把「收到禮物」推到對方的聊天室。

        用在「按按鈕領取」那條路上：對方是在 LIFF 頁面裡完成領取的，聊天室裡
        不會留下任何痕跡；不補這一則，他關掉頁面之後就沒有東西可以回頭看了。
        """
        user_name = self.get_user_name(user_id)
        message = TextSendMessage(
            text=(f"🎁 {user_name}，您收到一份禮物！\n\n"
                  f"朋友送您 {points} 點，已經加進您的帳戶了。\n\n"
                  f"您現在有 {balance} 點，可以拿老照片來試試看。"),
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label=label, text=value))
                for label, value in [("📸 修復老照片", "修復老照片"),
                                     ("🎬 照片動起來", "照片動起來"),
                                     ("💎 我的點數", "點數")]
            ]),
        )
        return self.publisher.process_push_message(user_id, message)

    # ------------------------------------------------------------------ steps

    def _ask_for_code(self, user_id: str, reply_token: str, event: dict):
        self.set_user_state(user_id, STATE_WAITING_CODE, {"fails": 0})
        return self._reply(
            reply_token, user_id, event,
            "請把朋友給您的禮物卡號碼傳給我。\n\n"
            f"長得像這樣：{gift_codes.format_code('ABCDEFGH')}\n"
            "大小寫都可以，中間的減號有沒有打都沒關係。",
            quick_reply=[(CANCEL, CANCEL)],
        )

    def _redeem(self, user_id: str, reply_token: str, event: dict, raw_code: str):
        user_name = self.get_user_name(user_id)

        # 兌換要有帳號可以進點；先確保會員存在（新朋友直接兌換也走得通）
        if self.member_service:
            self.member_service.get_or_create_member(user_id, user_name)

        if not self.gift_card_service:
            return self._reply(reply_token, user_id, event,
                               "現在沒辦法兌換，麻煩晚一點再試。")

        result = self.gift_card_service.redeem_for_user(user_id, raw_code)

        if result.status == gift_codes.OK:
            self.clear_user_state(user_id)
            # 這則就是長輩看到的「收到禮物」通知——朋友挑好卡片送過來，他點一下
            # 就走到這裡。寫得像收禮，不像交易確認。
            return self._reply(
                reply_token, user_id, event,
                f"🎁 {user_name}，您收到一份禮物！\n\n"
                f"朋友送您 {result.points} 點，已經加進您的帳戶了。\n\n"
                f"您現在有 {result.balance} 點，可以拿老照片來試試看。",
                quick_reply=[("📸 修復老照片", "修復老照片"),
                             ("🎬 照片動起來", "照片動起來"),
                             ("💎 我的點數", "點數")],
            )

        if result.status == gift_codes.ALREADY_USED:
            self.clear_user_state(user_id)
            return self._reply(
                reply_token, user_id, event,
                "這張卡已經用過了，沒辦法再兌換一次。\n\n"
                "如果您覺得不對，可以問問幫您買的朋友。",
            )

        return self._handle_bad_code(user_id, reply_token, event)

    def _handle_bad_code(self, user_id: str, reply_token: str, event: dict):
        """卡號不對：留在等卡號狀態讓他直接重打，連錯幾次才換個說法。"""
        state = self.get_user_state(user_id) or {}
        fails = (state.get("data") or {}).get("fails", 0) + 1
        self.set_user_state(user_id, STATE_WAITING_CODE, {"fails": fails})

        if fails >= _MAX_FAILS:
            self.clear_user_state(user_id)
            return self._reply(
                reply_token, user_id, event,
                "還是找不到這個號碼。\n\n"
                "麻煩請幫您買卡的朋友，把號碼再傳一次給您，"
                "然後輸入「兌換」重新試一次。",
            )

        return self._reply(
            reply_token, user_id, event,
            "找不到這個號碼，麻煩再確認一次。\n\n"
            f"卡號是 {gift_codes.CODE_LENGTH} 個字，長得像 "
            f"{gift_codes.format_code('ABCDEFGH')}。",
            quick_reply=[(CANCEL, CANCEL)],
        )

    # ------------------------------------------------------------------ reply

    def _reply(self, reply_token: str, user_id: str, event: dict, text: str,
               quick_reply=None):
        message = TextSendMessage(
            text=text,
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label=label, text=value))
                for label, value in quick_reply
            ]) if quick_reply else None,
        )
        return self.publisher.process_reply_message(reply_token, message, user_id, event)
