import os
import uuid
import requests

from src.core.app_logger import get_logger

logger = get_logger("storage")

# Supabase list API 單頁上限；批次刪除單次送出的 key 數量
LIST_PAGE_SIZE = 100
DELETE_BATCH_SIZE = 100


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
        """刪除圖片；失敗只記 log，殘留物件由 cleanup_storage.py 兜底。

        刪除視為冪等：物件不存在（404）代表目標狀態已達成，不算失敗——
        狀態轉換的自動清理與各功能的顯式清理可能刪到同一個 key。
        """
        try:
            response = requests.delete(
                self._object_url(key),
                headers=self._headers(),
                timeout=(3, 10),
            )
            if response.status_code == 404:
                logger.debug(f"Storage 物件已不存在，略過: {key}")
            elif response.status_code not in (200, 204):
                logger.warning(f"刪除 Storage 物件失敗: {key} (status={response.status_code})")
        except Exception:
            logger.warning(f"刪除 Storage 物件時發生錯誤: {key}")

    # ---- 批次維運（給 scripts/cleanup_storage.py 用） ----

    def _list_page(self, prefix: str, offset: int) -> list:
        """呼叫一次 list API。回傳的 name 相對於 prefix，資料夾以 id=None 出現。"""
        response = requests.post(
            f"{self.base_url}/storage/v1/object/list/{self.bucket}",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={
                "prefix": prefix,
                "limit": LIST_PAGE_SIZE,
                "offset": offset,
                "sortBy": {"column": "name", "order": "asc"},
            },
            timeout=(3, 20),
        )
        response.raise_for_status()
        return response.json() or []

    def list_folders(self, prefix: str = "") -> list:
        """列出某層底下的資料夾名稱（暫存圖以功能名稱分資料夾）"""
        folders = []
        offset = 0
        while True:
            page = self._list_page(prefix, offset)
            folders.extend(item["name"] for item in page if item.get("id") is None)
            if len(page) < LIST_PAGE_SIZE:
                break
            offset += LIST_PAGE_SIZE
        return folders

    def list_objects(self, prefix: str = "") -> list:
        """列出某個 prefix 底下的物件（自動翻頁，不遞迴進子資料夾）

        Returns:
            list[dict]: [{"key": 完整 object key, "created_at": ISO 字串}, ...]
        """
        objects = []
        offset = 0
        while True:
            page = self._list_page(prefix, offset)
            for item in page:
                if item.get("id") is None:
                    continue  # 資料夾，不是物件
                name = item.get("name")
                objects.append({
                    "key": f"{prefix}/{name}" if prefix else name,
                    "created_at": item.get("created_at") or item.get("updated_at"),
                })
            if len(page) < LIST_PAGE_SIZE:
                break
            offset += LIST_PAGE_SIZE
        return objects

    def delete_images(self, keys) -> int:
        """批次刪除物件；回傳成功送出的數量。失敗只記 log，不中斷整批。"""
        keys = list(keys)
        deleted = 0
        for start in range(0, len(keys), DELETE_BATCH_SIZE):
            batch = keys[start:start + DELETE_BATCH_SIZE]
            try:
                response = requests.delete(
                    f"{self.base_url}/storage/v1/object/{self.bucket}",
                    headers={**self._headers(), "Content-Type": "application/json"},
                    json={"prefixes": batch},
                    timeout=(3, 30),
                )
                if response.status_code in (200, 204):
                    deleted += len(batch)
                else:
                    logger.warning(f"批次刪除失敗 (status={response.status_code}): {response.text[:200]}")
            except Exception:
                logger.warning(f"批次刪除時發生錯誤（{len(batch)} 個物件）")
        return deleted
