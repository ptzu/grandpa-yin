import time
import requests
from linebot.exceptions import LineBotApiError
from src.core.app_logger import get_logger

logger = get_logger("publisher")


class MessagePublisher:
    """統一的訊息發送器"""

    def __init__(self, line_bot_api):
        self.line_bot_api = line_bot_api

    def _send_with_retry(self, send_fn, description, max_attempts=3):
        """
        執行發送動作，對瞬斷（網路錯誤）與伺服器錯誤（5xx）做指數退避重試。
        4xx 屬呼叫端錯誤（無效 token、目標不存在），不重試。

        Returns:
            bool: 是否發送成功
        """
        delay = 1
        for attempt in range(1, max_attempts + 1):
            try:
                send_fn()
                return True
            except LineBotApiError as e:
                status_code = getattr(e, 'status_code', None)
                if status_code is not None and status_code < 500:
                    logger.error(f"{description} 失敗 (status={status_code})，不重試: {str(e)}")
                    return False
                logger.warning(f"{description} 失敗 (status={status_code})，第 {attempt}/{max_attempts} 次: {str(e)}")
            except requests.exceptions.RequestException as e:
                logger.warning(f"{description} 網路錯誤，第 {attempt}/{max_attempts} 次: {str(e)}")

            if attempt < max_attempts:
                time.sleep(delay)
                delay *= 2

        logger.error(f"{description} 重試 {max_attempts} 次後仍失敗")
        return False

    def _get_source_type(self, event):
        """
        檢測訊息來源類型

        Args:
            event: LINE webhook event

        Returns:
            str: 'user', 'group', 'room' 或 'unknown'
        """
        source = event.get('source', {})
        source_type = source.get('type', 'unknown')
        return source_type

    def _is_group_chat(self, event):
        """
        判斷是否為群組聊天

        Args:
            event: LINE webhook event

        Returns:
            bool: True 如果是群組聊天，False 如果是個人聊天
        """
        source_type = self._get_source_type(event)
        return source_type in ['group', 'room']

    def _get_target_id(self, event):
        """
        獲取正確的目標ID（用於推送訊息）

        Args:
            event: LINE webhook event

        Returns:
            str: 群組ID（群組聊天）或用戶ID（個人聊天）
        """
        if not event:
            return None

        source = event.get('source', {})
        source_type = source.get('type', 'user')

        if source_type == 'group':
            return source.get('groupId', '')
        elif source_type == 'room':
            return source.get('roomId', '')
        else:  # source_type == 'user'
            return source.get('userId', '')

    def process_reply_message(self, reply_token, messages, user_id=None, event=None):
        """
        回覆訊息；發送失敗時記錄錯誤，不向外拋出

        Args:
            reply_token: LINE 回覆 token
            messages: 要發送的訊息
            user_id: 用戶 ID（僅用於記錄）
            event: LINE webhook event（保留參數，維持呼叫端介面）

        Returns:
            None（統一回傳，webhook 以 200 回應 LINE）
        """
        try:
            self.line_bot_api.reply_message(reply_token, messages)
        except LineBotApiError as e:
            status_code = getattr(e, 'status_code', None)
            logger.error(f"回覆訊息失敗 (user={user_id}, status={status_code}): {str(e)}")
        return None

    def reply_text(self, reply_token, text, user_id=None, event=None):
        """
        回覆文字訊息的便利方法

        Args:
            reply_token: LINE 回覆 token
            text: 要回覆的文字內容
            user_id: 用戶 ID（僅用於記錄）
            event: LINE webhook event（保留參數，維持呼叫端介面）

        Returns:
            None（統一回傳）
        """
        from linebot.models import TextSendMessage
        return self.process_reply_message(reply_token, TextSendMessage(text=text), user_id, event)

    def process_push_message(self, user_id, messages, event=None):
        """
        推送訊息；群組聊天推送到群組，發送失敗時記錄錯誤，不向外拋出

        Args:
            user_id: 用戶 ID（個人聊天使用）
            messages: 要發送的訊息
            event: LINE webhook event（用於判斷是否為群組聊天）

        Returns:
            None（統一回傳）
        """
        if event and self._is_group_chat(event):
            target_id = self._get_target_id(event)
        else:
            target_id = user_id

        # 推送多在背景執行緒進行，可承受重試；reply token 為一次性故 reply 不重試
        self._send_with_retry(
            lambda: self.line_bot_api.push_message(target_id, messages),
            f"推送訊息 (target={target_id})"
        )
        return None
