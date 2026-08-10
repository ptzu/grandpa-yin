"""Charge-run-refund orchestration for background work.

This used to live on ReplicateImageFeature, which meant the money flow — deduct
first, run, refund on failure, degrade when the pool is full — was only reachable
by subclassing a *Replicate* feature. Any other paid capability had to copy it.
Here it is a service any feature can be handed via FeatureContext.

Points are deducted *before* the work runs: doing it after would let a crash or a
restart hand out free processing.
"""
from linebot.models import TextSendMessage

from src.core.app_logger import get_logger
from src.core.task_executor import submit_image_task

logger = get_logger("billing")

INSUFFICIENT_MESSAGE = "❌ 點數不足或扣點失敗，本次未進行處理。\n請輸入「點數」查看剩餘點數。"
FAILURE_MESSAGE = "處理時發生錯誤，點數已退還，請稍後再試 🙏"
BUSY_MESSAGE = "目前使用人數較多，請稍後再試 🙏"


class BillingService:
    """Runs a paid task in the background: deduct → run → refund on failure."""

    def __init__(self, member_service, publisher):
        self._member_service = member_service
        self._publisher = publisher

    def submit(self, *, user_id, event, points, feature_type, description,
               run, on_success, on_finish=None, failure_message=FAILURE_MESSAGE) -> bool:
        """Queue a billed task on the shared bounded pool.

        Args:
            run: zero-arg callable producing the result (runs on a worker thread)
            on_success: called with run()'s result to deliver it to the user
            on_finish: always called once the task settles, and also when the
                pool rejects it — the place to reset user state
            failure_message: user-facing text when run() raises; points are
                refunded either way

        Returns:
            bool: False when the pool is at capacity (the caller's user has
            already been told); True when the task was queued.
        """
        def task():
            try:
                # 先扣點，扣不到就不處理（避免先服務後扣點被免費使用）
                if not self._member_service.deduct_points(
                    user_id, points, description, feature_type=feature_type,
                ):
                    self._publisher.process_push_message(
                        user_id, TextSendMessage(text=INSUFFICIENT_MESSAGE), event
                    )
                    return

                try:
                    result = run()
                except Exception as e:
                    # 處理失敗 → 退點並留下 failed 稽核記錄
                    logger.exception(f"{feature_type} 處理失敗，退還點數: {user_id}")
                    self._member_service.refund_points(
                        user_id, points, feature_type=feature_type, reason=str(e)
                    )
                    self._publisher.process_push_message(
                        user_id, TextSendMessage(text=failure_message), event
                    )
                    return

                on_success(result)
            finally:
                if on_finish:
                    on_finish()
                logger.info(f"用戶 {user_id} {feature_type} 處理完成，狀態已重置")

        if not submit_image_task(task):
            # 執行緒池容量滿：優雅降級
            if on_finish:
                on_finish()
            self._publisher.process_push_message(
                user_id, TextSendMessage(text=BUSY_MESSAGE), event
            )
            return False
        return True
