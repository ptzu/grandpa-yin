import threading
import contextvars
from concurrent.futures import ThreadPoolExecutor

from src.core.settings import get_worker_pool_settings

# 圖片處理共用的有界執行緒池：外部 API 變慢時最多堆積有限的工作，
# 不會無限開執行緒耗盡記憶體與 DB 連線（連鎖失效防護）。
# 大小來自 config/settings.yml 的 worker_pool（IMAGE_WORKERS / IMAGE_QUEUE_LIMIT 可臨時覆寫）。
_pool = get_worker_pool_settings()
_MAX_WORKERS = _pool.max_workers
_MAX_PENDING = _pool.queue_limit

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

    # 複製當前 context，讓背景工作繼承 request_id 等 ContextVar
    ctx = contextvars.copy_context()

    def _wrapped():
        try:
            ctx.run(fn)
        finally:
            _capacity.release()

    _executor.submit(_wrapped)
    return True
