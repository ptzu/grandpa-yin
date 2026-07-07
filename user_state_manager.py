from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from sqlalchemy import delete

from models.database import get_session
from models.account import Account
from models.linked_identity import LinkedIdentity
from models.bot_session import BotSession


LINE_PROVIDER = 'line'


def _resolve_account(session, line_uid):
    identity = (
        session.query(LinkedIdentity)
        .filter_by(provider=LINE_PROVIDER, provider_uid=line_uid)
        .first()
    )
    if not identity:
        return None
    return session.query(Account).filter_by(id=identity.account_id).first()


def _resolve_or_create_account(session, line_uid):
    account = _resolve_account(session, line_uid)
    if account:
        return account
    # 自動建 shadow account（follow event 若遲到也不會炸）
    account = Account(points_balance=0, is_admin=False)
    session.add(account)
    session.flush()
    session.add(LinkedIdentity(account_id=account.id, provider=LINE_PROVIDER, provider_uid=line_uid))
    return account


class UserStateManager:
    """用戶狀態管理器（使用 grandpa_yin.bot_sessions）"""

    def __init__(self):
        pass

    def set_state(self, user_id: str, state: Dict[str, Any]):
        """設定用戶狀態
        state 格式：{"feature": "colorize", "state": "waiting", "data": {...}}
        """
        feature = state.get("feature")
        state_name = state.get("state")
        data = state.get("data") or {}
        # bot_sessions.current_state 採複合字串 "feature:state"
        current_state = f"{feature}:{state_name}" if feature else state_name

        try:
            with get_session() as session:
                account = _resolve_or_create_account(session, user_id)

                existing = session.query(BotSession).filter_by(account_id=account.id).first()
                if existing:
                    existing.current_state = current_state
                    existing.state_metadata = data
                    print(f"用戶 {user_id} 狀態已更新: {state}")
                else:
                    session.add(BotSession(
                        account_id=account.id,
                        current_state=current_state,
                        state_metadata=data,
                    ))
                    print(f"用戶 {user_id} 狀態已建立: {state}")

                session.commit()
        except Exception as e:
            print(f"設定用戶狀態失敗: {str(e)}")
            raise e

    def get_state(self, user_id: str) -> Optional[Dict[str, Any]]:
        """獲取用戶狀態；無狀態回傳 None"""
        try:
            with get_session() as session:
                account = _resolve_account(session, user_id)
                if not account:
                    return None
                bot_session = session.query(BotSession).filter_by(account_id=account.id).first()
                if not bot_session:
                    return None
                return {
                    "feature": bot_session.feature,
                    "state": bot_session.state,
                    "data": bot_session.state_metadata or None,
                }
        except Exception as e:
            print(f"獲取用戶狀態失敗: {str(e)}")
            return None

    def clear_state(self, user_id: str):
        """清除用戶狀態"""
        try:
            with get_session() as session:
                account = _resolve_account(session, user_id)
                if not account:
                    print(f"用戶 {user_id} 沒有 account，略過清除")
                    return

                bot_session = session.query(BotSession).filter_by(account_id=account.id).first()
                if bot_session:
                    old_state = {
                        "feature": bot_session.feature,
                        "state": bot_session.state,
                        "data": bot_session.state_metadata,
                    }
                    session.delete(bot_session)
                    session.commit()
                    print(f"用戶 {user_id} 狀態已清除 (原狀態: {old_state})")
                else:
                    print(f"用戶 {user_id} 沒有狀態需要清除")
        except Exception as e:
            print(f"清除用戶狀態失敗: {str(e)}")
            raise e

    def get_all_states(self) -> Dict[str, Dict[str, Any]]:
        """獲取所有用戶狀態（回傳以 LINE UID 為 key 的 dict，給 debug/admin 用）"""
        try:
            with get_session() as session:
                rows = (
                    session.query(BotSession, LinkedIdentity)
                    .join(LinkedIdentity, LinkedIdentity.account_id == BotSession.account_id)
                    .filter(LinkedIdentity.provider == LINE_PROVIDER)
                    .all()
                )
                return {
                    identity.provider_uid: {
                        "feature": bs.feature,
                        "state": bs.state,
                        "data": bs.state_metadata,
                    }
                    for bs, identity in rows
                }
        except Exception as e:
            print(f"獲取所有狀態失敗: {str(e)}")
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
                print(f"已清理 {result.rowcount} 個超過 {hours} 小時的舊狀態")
                return result.rowcount
        except Exception as e:
            print(f"清理舊狀態失敗: {str(e)}")
            return 0
