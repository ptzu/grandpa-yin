from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from sqlalchemy import delete

from app_logger import get_logger
from models.database import get_session
from models.bot_session import BotSession
from services.account_backend import get_account_backend

logger = get_logger("user_state")


class UserStateManager:
    """用戶狀態管理器（使用 grandpa_yin.bot_sessions）"""

    def __init__(self, backend=None):
        self._backend = backend or get_account_backend()

    def set_state(self, user_id: str, state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """設定用戶狀態
        state 格式：{"feature": "colorize", "state": "waiting", "data": {...}}

        Returns:
            被覆蓋掉的舊 state data（沒有舊狀態時為 None）。呼叫端據此清理
            不再被引用的外部資源，例如暫存在 Storage 的圖片。
        """
        feature = state.get("feature")
        state_name = state.get("state")
        data = state.get("data") or {}
        # bot_sessions.current_state 採複合字串 "feature:state"
        current_state = f"{feature}:{state_name}" if feature else state_name

        try:
            with get_session() as session:
                subject = self._backend.get_or_create(session, user_id)

                existing = session.query(BotSession).filter_by(account_id=subject.id).first()
                if existing:
                    # 覆蓋前先留一份，讓呼叫端能清理舊狀態引用的資源
                    replaced = dict(existing.state_metadata or {})
                    existing.current_state = current_state
                    existing.state_metadata = data
                    logger.debug(f"用戶 {user_id} 狀態已更新: {state}")
                else:
                    replaced = None
                    session.add(BotSession(
                        account_id=subject.id,
                        current_state=current_state,
                        state_metadata=data,
                    ))
                    logger.debug(f"用戶 {user_id} 狀態已建立: {state}")

                session.commit()
                return replaced
        except Exception as e:
            logger.exception(f"設定用戶 {user_id} 狀態失敗")
            raise e

    def get_state(self, user_id: str) -> Optional[Dict[str, Any]]:
        """獲取用戶狀態；無狀態回傳 None"""
        try:
            with get_session() as session:
                subject = self._backend.resolve(session, user_id)
                if not subject:
                    return None
                bot_session = session.query(BotSession).filter_by(account_id=subject.id).first()
                if not bot_session:
                    return None
                return {
                    "feature": bot_session.feature,
                    "state": bot_session.state,
                    "data": bot_session.state_metadata or None,
                }
        except Exception as e:
            logger.exception(f"獲取用戶 {user_id} 狀態失敗")
            return None

    def clear_state(self, user_id: str) -> Optional[Dict[str, Any]]:
        """清除用戶狀態

        Returns:
            被清掉的 state data（原本就沒有狀態時為 None），語意同 set_state。
        """
        try:
            with get_session() as session:
                subject = self._backend.resolve(session, user_id)
                if not subject:
                    logger.debug(f"用戶 {user_id} 沒有 account，略過清除")
                    return None

                bot_session = session.query(BotSession).filter_by(account_id=subject.id).first()
                if bot_session:
                    old_state = {
                        "feature": bot_session.feature,
                        "state": bot_session.state,
                        "data": bot_session.state_metadata,
                    }
                    removed = dict(bot_session.state_metadata or {})
                    session.delete(bot_session)
                    session.commit()
                    logger.debug(f"用戶 {user_id} 狀態已清除 (原狀態: {old_state})")
                    return removed

                logger.debug(f"用戶 {user_id} 沒有狀態需要清除")
                return None
        except Exception as e:
            logger.exception(f"清除用戶 {user_id} 狀態失敗")
            raise e

    def get_all_states(self) -> Dict[str, Dict[str, Any]]:
        """獲取所有用戶狀態（回傳以 LINE UID 為 key 的 dict，給 debug/admin 用）"""
        try:
            with get_session() as session:
                sessions = session.query(BotSession).all()
                # subject_id -> LINE UID reverse map, resolved by the active backend
                uid_map = self._backend.provider_uid_map(
                    session, [bs.account_id for bs in sessions]
                )
                return {
                    uid_map[bs.account_id]: {
                        "feature": bs.feature,
                        "state": bs.state,
                        "data": bs.state_metadata,
                    }
                    for bs in sessions
                    if bs.account_id in uid_map
                }
        except Exception as e:
            logger.exception("獲取所有狀態失敗")
            return {}

    def cleanup_old_states(self, hours: int = 24):
        """清理超過指定小時的舊狀態"""
        try:
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)

            with get_session() as session:
                result = session.execute(
                    delete(BotSession).where(BotSession.updated_at < cutoff_time)
                )
                session.commit()
                logger.info(f"已清理 {result.rowcount} 個超過 {hours} 小時的舊狀態")
                return result.rowcount
        except Exception as e:
            logger.exception("清理舊狀態失敗")
            return 0
