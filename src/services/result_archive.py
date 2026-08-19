"""成品保存：把模型輸出轉存到自家 Storage，換成長效的 signed URL。

為什麼需要這一層：模型輸出的網址是 Replicate 那邊的暫存檔，大約一小時後就
失效。在那之前圖看得起來，之後用戶往上滑重看就是破圖——而長輩最在意的正是
「把修好的照片留著」。所以推送前先把成品抓回來存進自己的 bucket，推給 LINE
的是我們自己的 signed URL，保留期內都有效。

保存是**盡力而為**：任何一步失敗都回 None，呼叫端沿用原本的模型輸出網址照常
推送。用戶已經扣了點數，不能因為保存失敗就拿不到成品——那是把「留久一點」
的加分項變成「根本沒收到」的重大退步。

物件一律放在 `results/` 底下，讓 cleanup_storage.py 能把它跟「處理完就該刪」
的暫存圖分開，套用不同的保留期。
"""
from urllib.parse import urlparse

import requests

from src.core.app_logger import get_logger

logger = get_logger("result_archive")

# 成品專用的 prefix；cleanup_storage.py 認這個字串來套用長保留期
RESULT_PREFIX = "results"

RETENTION_DAYS = 30
SECONDS_PER_DAY = 86400

# 保存前的大小上限。正常成品是幾 MB 的圖或 5 秒影片，破表代表模型回了非預期
# 的東西，寧可不保存也不要把 bucket 灌爆。
MAX_RESULT_BYTES = 60 * 1024 * 1024

# 認得的成品型別。認不出來就不保存（沿用原網址），不要瞎猜——型別填錯存進
# Storage，LINE 那邊會變成打不開的訊息。
EXTENSION_CONTENT_TYPES = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
}
_CONTENT_TYPE_EXTENSIONS = {v: k for k, v in EXTENSION_CONTENT_TYPES.items()}
_CONTENT_TYPE_EXTENSIONS["image/jpg"] = "jpg"

_EXTENSION_ALIASES = {"jpeg": "jpg", "qt": "mov"}


class ResultArchive:
    """Copies a model's output into our own bucket and returns a long-lived URL."""

    def __init__(self, storage_service, retention_days: int = RETENTION_DAYS):
        self._storage = storage_service
        self.retention_days = retention_days
        self.url_ttl_seconds = retention_days * SECONDS_PER_DAY

    def is_available(self) -> bool:
        """沒設定 Storage 時整個保存機制靜靜地不作用，服務照常運作"""
        return bool(self._storage and self._storage.is_configured())

    def archive(self, output_url: str):
        """轉存模型輸出，回傳長效 signed URL；不可用或失敗時回傳 None"""
        if not output_url or not self.is_available():
            return None

        try:
            data, content_type = self._download(output_url)
            extension, content_type = self._resolve_type(content_type, output_url)
            return self._store(data, extension, content_type)
        except Exception:
            logger.exception(f"成品保存失敗，改用模型的暫存網址: {output_url[:120]}")
            return None

    def store_bytes(self, data: bytes, extension: str = "jpg",
                    content_type: str = "image/jpeg"):
        """保存手上已有的 bytes（影片訊息的縮圖走這條）；失敗回傳 None"""
        if not data or not self.is_available():
            return None

        try:
            return self._store(data, extension, content_type)
        except Exception:
            logger.exception("成品保存失敗（bytes）")
            return None

    # ---- 內部 ----

    def _store(self, data: bytes, extension: str, content_type: str) -> str:
        key = self._storage.upload_object(data, RESULT_PREFIX, extension, content_type)
        url = self._storage.create_signed_url(key, self.url_ttl_seconds)
        logger.info(f"成品已保存 {self.retention_days} 天: {key} ({len(data)} bytes)")
        return url

    def _download(self, url: str):
        """下載成品，回傳 (bytes, Content-Type)；超過大小上限就放棄"""
        response = requests.get(url, stream=True, timeout=(5, 60))
        response.raise_for_status()

        chunks = []
        total = 0
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_RESULT_BYTES:
                raise ValueError(f"成品超過 {MAX_RESULT_BYTES} bytes 的保存上限")
            chunks.append(chunk)

        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return b"".join(chunks), content_type

    @staticmethod
    def _resolve_type(content_type: str, url: str):
        """判定 (副檔名, Content-Type)；認不出來就拋例外，讓呼叫端沿用原網址

        以 HTTP 回應的 Content-Type 為準，它比網址可靠——Replicate 的輸出網址
        不保證帶副檔名。兩邊都問不出來時不猜。
        """
        extension = _CONTENT_TYPE_EXTENSIONS.get(content_type)
        if extension:
            return extension, content_type

        path = urlparse(url).path
        candidate = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        candidate = _EXTENSION_ALIASES.get(candidate, candidate)
        if candidate in EXTENSION_CONTENT_TYPES:
            return candidate, EXTENSION_CONTENT_TYPES[candidate]

        raise ValueError(f"認不出成品的檔案型別 (Content-Type={content_type!r}, url={url[:120]})")
