import os
import logging
import sys
from contextvars import ContextVar

# 每個 webhook request 的追蹤 ID；背景工作經由 task_executor 的
# copy_context() 自動繼承，客訴時可用同一個 ID 串起完整處理過程
request_id_var: ContextVar = ContextVar("request_id", default="-")


class _ContextFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id_var.get()
        return True


def get_logger(name):
    """取得帶 request_id 的 logger；重複呼叫同名 logger 不會重複加 handler"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"
        ))
        handler.addFilter(_ContextFilter())
        logger.addHandler(handler)
        logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
        logger.propagate = False
    return logger
