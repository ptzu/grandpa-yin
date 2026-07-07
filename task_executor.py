import os
import threading
from concurrent.futures import ThreadPoolExecutor

# 圖片處理共用的有界執行緒池：外部 API 變慢時最多堆積有限的工作，
# 不會無限開執行緒耗盡記憶體與 DB 連線（連鎖失效防護）
_MAX_WORKERS = int(os.getenv("IMAGE_WORKERS", "4"))
_MAX_PENDING = int(os.getenv("IMAGE_QUEUE_LIMIT", "8"))

_executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="image-worker")
# 限制「執行中 + 排隊中」的工作總量
_capacity = threading.Semaphore(_MAX_WORKERS + _MAX_PENDING)


def submit_image_task(fn) -> bool:
    """
    提交背景圖片處理工作。

    Returns:
        bool: True 表示已排入執行；False 表示容量已滿，呼叫端應回覆繁忙訊息
    """
    if not _capacity.acquire(blocking=False):
        return False

    def _wrapped():
        try:
            fn()
        finally:
            _capacity.release()

    _executor.submit(_wrapped)
    return True
