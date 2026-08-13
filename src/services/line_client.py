"""LINE Messaging API — receive side.

MessagePublisher owns the send side (reply / push with retry); this owns
everything that reads from LINE or pokes its REST API directly. Features used to
hold a raw LineBotApi and call it themselves, which put HTTP calls in the feature
layer; keeping both directions behind services/ restores `features -> services`.
"""
import os

import requests

from src.core.app_logger import get_logger

logger = get_logger("line_client")

LOADING_ANIMATION_URL = "https://api.line.me/v2/bot/chat/loading/start"


class LineClient:
    """Receive-side LINE client: message content, profiles, loading animation."""

    def __init__(self, line_bot_api, channel_access_token=None):
        self._api = line_bot_api
        self._token = channel_access_token or os.getenv("CHANNEL_ACCESS_TOKEN", "")

    def download_message_content(self, message_id: str) -> bytes:
        """Download an image (or other content) the user uploaded."""
        content = self._api.get_message_content(message_id)
        return b''.join(chunk for chunk in content.iter_content())

    def get_display_name(self, user_id: str):
        """LINE profile display name, or None when it cannot be fetched.

        Returns None rather than a placeholder so callers decide the fallback.
        """
        try:
            return self._api.get_profile(user_id).display_name
        except Exception as e:
            logger.warning(f"無法獲取用戶名稱：{str(e)}")
            return None

    def start_loading_animation(self, user_id: str, seconds: int = 30):
        """Show the typing indicator. Best-effort — never breaks the main flow."""
        try:
            response = requests.post(
                LOADING_ANIMATION_URL,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self._token}',
                },
                json={"chatId": user_id, "loadingSeconds": seconds},
                timeout=(3, 10),
            )
            if response.status_code != 200:
                logger.warning(f"載入動畫啟動失敗: {response.status_code} - {response.text}")
        except Exception as e:
            logger.warning(f"啟動載入動畫時發生錯誤: {str(e)}")
