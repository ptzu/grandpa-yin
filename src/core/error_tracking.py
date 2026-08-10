import os

from src.core.app_logger import get_logger

logger = get_logger("sentry")

_enabled = False


def init_sentry() -> bool:
    """初始化 Sentry 錯誤追蹤（設定 SENTRY_DSN 才啟用）。

    啟用後：
    - Flask 未捕捉的例外自動上報
    - logger.error / logger.exception 自動變成 Sentry issue
      （INFO 以上的 log 會成為 issue 的 breadcrumbs 上下文）
    """
    global _enabled
    dsn = os.getenv("SENTRY_DSN")
    if not dsn:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration

        sentry_sdk.init(
            dsn=dsn,
            integrations=[FlaskIntegration()],
            environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
            # Railway 部署時自動帶入 commit SHA，issue 可對回程式版本
            release=os.getenv("RAILWAY_GIT_COMMIT_SHA"),
            send_default_pii=False,  # 不自動上傳 IP 等個資
            traces_sample_rate=0.0,  # 只做錯誤追蹤，不做效能追蹤
        )
        _enabled = True
        logger.info("Sentry 錯誤追蹤已啟用")
    except ImportError:
        logger.warning("已設定 SENTRY_DSN 但未安裝 sentry-sdk，錯誤追蹤未啟用")

    return _enabled


def set_request_context(request_id=None, user_id=None):
    """把 request_id / LINE user id 附加到後續的 Sentry 事件上。

    背景執行緒經由 task_executor 的 copy_context() 繼承同一個 scope。
    未啟用 Sentry 時為 no-op。
    """
    if not _enabled:
        return
    import sentry_sdk
    if request_id:
        sentry_sdk.set_tag("request_id", request_id)
    if user_id:
        sentry_sdk.set_user({"id": user_id})
