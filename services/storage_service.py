import os
import uuid
import requests

from app_logger import get_logger

logger = get_logger("storage")


class StorageService:
    """Supabase Storage 客戶端（走 Storage REST API，不引入 supabase-py 依賴）

    用途：暫存用戶上傳的圖片（例如圖片編輯的兩步驟流程），
    取代把整張圖 base64 塞進 bot_sessions.state_metadata（JSONB）。

    需要的環境變數：
      - SUPABASE_URL：專案 URL（https://xxx.supabase.co）
      - SUPABASE_SERVICE_ROLE_KEY：service role key（繞過 RLS，僅供後端使用）
      - SUPABASE_STORAGE_BUCKET：bucket 名稱（預設 linebot-temp-images）
    """

    def __init__(self):
        self.base_url = os.getenv("SUPABASE_URL", "").rstrip("/")
        self.api_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        self.bucket = os.getenv("SUPABASE_STORAGE_BUCKET", "linebot-temp-images")

    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _object_url(self, key: str) -> str:
        return f"{self.base_url}/storage/v1/object/{self.bucket}/{key}"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "apikey": self.api_key,
        }

    def upload_image(self, image_bytes: bytes, prefix: str = "tmp") -> str:
        """上傳圖片並回傳 object key；失敗時拋出例外由呼叫端處理"""
        key = f"{prefix}/{uuid.uuid4().hex}.jpg"
        response = requests.post(
            self._object_url(key),
            headers={
                **self._headers(),
                "Content-Type": "image/jpeg",
                "x-upsert": "true",
            },
            data=image_bytes,
            timeout=(3, 15),
        )
        response.raise_for_status()
        logger.debug(f"圖片已上傳至 Storage: {key} ({len(image_bytes)} bytes)")
        return key

    def download_image(self, key: str) -> bytes:
        """下載圖片；失敗時拋出例外由呼叫端處理"""
        response = requests.get(
            self._object_url(key),
            headers=self._headers(),
            timeout=(3, 15),
        )
        response.raise_for_status()
        return response.content

    def delete_image(self, key: str):
        """刪除圖片；失敗只記 log，殘留物件可由 bucket 的清理策略處理"""
        try:
            response = requests.delete(
                self._object_url(key),
                headers=self._headers(),
                timeout=(3, 10),
            )
            if response.status_code not in (200, 204):
                logger.warning(f"刪除 Storage 物件失敗: {key} (status={response.status_code})")
        except Exception:
            logger.warning(f"刪除 Storage 物件時發生錯誤: {key}")
