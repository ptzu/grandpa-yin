from sqlalchemy import func

from src.core.app_logger import get_logger
from src.models.database import get_session
from src.models.grandpa_yin_profile import GrandpaYinProfile
from src.models.usage_log import UsageLog
from src.services.account_backend import get_account_backend

logger = get_logger("member_service")


SERVICE_NAME = 'silver-grandpa'
SIGNUP_BONUS_DESCRIPTION = '新會員註冊獎勵'


def _member_dict(subject, profile, line_uid):
    """組成向後相容的 member dict"""
    return {
        'user_id': line_uid,
        'display_name': (profile.display_name if profile else None) or '使用者',
        'points': subject.points_balance,
        'status': profile.status if profile else 'normal',
        'created_at': subject.created_at.isoformat() if subject.created_at else None,
        'updated_at': profile.updated_at.isoformat() if profile and profile.updated_at else None,
    }


def _transaction_to_history_dict(t):
    """把 ledger row 轉成舊 history API 相容的 dict（推斷 transaction_type）"""
    if t.amount > 0:
        tx_type = 'earn'
    elif t.amount < 0:
        tx_type = 'spend'
    else:
        tx_type = 'adjustment'
    return {
        'id': str(t.id),
        'transaction_type': tx_type,
        'points': t.amount,
        'balance_after': t.balance_after,
        'description': t.description,
        'created_at': t.created_at.isoformat() if t.created_at else None,
    }


class MemberService:
    """會員服務層 - 對外 API 以 LINE UID 為主鍵。

    身份 / 點數 / 交易帳由注入的 AccountBackend 負責（platform 走 Altide
    public.*，standalone 走 grandpa_yin.* 自有表）；本層只保留產品自有的
    GrandpaYinProfile 與 UsageLog，兩種模式共用。
    """

    def __init__(self, backend=None):
        self._backend = backend or get_account_backend()

    def get_or_create_member(self, user_id, display_name=None):
        """取得或建立會員。新 LINE UID 自動建立 shadow subject + grandpa_yin_profile。"""
        with get_session() as session:
            subject = self._backend.resolve(session, user_id)

            if subject:
                profile = session.query(GrandpaYinProfile).filter_by(account_id=subject.id).first()
                # 更新 display_name（debug 方便看是誰）
                if profile and display_name and profile.display_name != display_name:
                    profile.display_name = display_name
                    session.commit()
                    logger.info(f"會員資料已更新: {user_id}")
                return _member_dict(subject, profile, user_id)

            # 新會員：backend 建 shadow subject（+ 綁定），本層建 grandpa_yin_profile
            subject = self._backend.get_or_create(session, user_id)
            profile = GrandpaYinProfile(
                account_id=subject.id,
                display_name=display_name or '使用者',
                status='normal',
                is_tutorial_completed=False,
            )
            session.add(profile)
            session.commit()
            logger.info(f"新會員已建立: {user_id} ({display_name})")
            return _member_dict(subject, profile, user_id)

    def get_member_info(self, user_id):
        """查詢會員完整資訊"""
        with get_session() as session:
            subject = self._backend.resolve(session, user_id)
            if not subject:
                return None
            profile = session.query(GrandpaYinProfile).filter_by(account_id=subject.id).first()
            return _member_dict(subject, profile, user_id)

    def add_points(self, user_id, points, transaction_type='earn', description=None):
        """增加點數並記錄交易。transaction_type 為舊介面相容，併入 description。"""
        if points <= 0:
            logger.error(f"點數必須為正數: {points}")
            return False

        with get_session() as session:
            try:
                subject = self._backend.resolve(session, user_id, for_update=True)
                if not subject:
                    logger.error(f"會員不存在: {user_id}")
                    return False

                new_balance = self._backend.credit(
                    session, subject, points,
                    service=SERVICE_NAME,
                    description=description or f'加點（{transaction_type}）',
                )

                session.commit()
                logger.info(f"點數已增加: {user_id} (+{points}), 餘額: {new_balance}")
                return True

            except Exception:
                session.rollback()
                logger.exception(f"增加點數失敗: {user_id}")
                return False

    def grant_signup_bonus(self, user_id, points):
        """冪等發放註冊獎勵：同一 subject 只會發放一次。

        用 row lock + 交易記錄唯一性檢查取代呼叫端的「先查再加點」，
        兩個 follow event 同時到達時第二個會被鎖住、看到已有獎勵而跳過。

        Returns:
            bool: 是否實際發放（已發放過或失敗都回傳 False）
        """
        if points <= 0:
            logger.error(f"點數必須為正數: {points}")
            return False

        with get_session() as session:
            try:
                subject = self._backend.resolve(session, user_id, for_update=True)
                if not subject:
                    logger.error(f"會員不存在: {user_id}")
                    return False

                if self._backend.has_transaction(
                    session, subject,
                    service=SERVICE_NAME, description=SIGNUP_BONUS_DESCRIPTION,
                ):
                    logger.info(f"註冊獎勵已發放過，跳過: {user_id}")
                    return False

                new_balance = self._backend.credit(
                    session, subject, points,
                    service=SERVICE_NAME, description=SIGNUP_BONUS_DESCRIPTION,
                )

                session.commit()
                logger.info(f"註冊獎勵已發放: {user_id} (+{points}), 餘額: {new_balance}")
                return True

            except Exception:
                session.rollback()
                logger.exception(f"發放註冊獎勵失敗: {user_id}")
                return False

    def deduct_points(self, user_id, points, description=None, feature_type=None):
        """扣除點數：同時寫交易帳（管道）與 grandpa_yin.usage_logs（細節）"""
        if points <= 0:
            logger.error(f"點數必須為正數: {points}")
            return False

        with get_session() as session:
            try:
                subject = self._backend.resolve(session, user_id, for_update=True)
                if not subject:
                    logger.error(f"會員不存在: {user_id}")
                    return False

                if subject.points_balance < points:
                    logger.info(f"點數不足: {user_id}, 需要 {points}, 目前 {subject.points_balance}")
                    return False

                channel_desc = f'銀爺爺：{feature_type}' if feature_type else '銀爺爺功能扣點'
                new_balance = self._backend.debit(
                    session, subject, points,
                    service=SERVICE_NAME, description=channel_desc,
                )

                session.add(UsageLog(
                    account_id=subject.id,
                    feature_type=feature_type or 'unknown',
                    points_deducted=points,
                    status='completed',
                    log_metadata={'description': description} if description else {},
                ))

                session.commit()
                logger.info(f"點數已扣除: {user_id} (-{points}), 餘額: {new_balance}")
                return True

            except Exception:
                session.rollback()
                logger.exception(f"扣除點數失敗: {user_id}")
                return False

    def refund_points(self, user_id, points, feature_type=None, reason=None):
        """功能處理失敗時退還點數，並在 usage_logs 留下 failed 記錄供稽核"""
        if points <= 0:
            logger.error(f"點數必須為正數: {points}")
            return False

        with get_session() as session:
            try:
                subject = self._backend.resolve(session, user_id, for_update=True)
                if not subject:
                    logger.error(f"會員不存在: {user_id}")
                    return False

                refund_desc = f'銀爺爺：{feature_type} 失敗退點' if feature_type else '銀爺爺功能失敗退點'
                new_balance = self._backend.credit(
                    session, subject, points,
                    service=SERVICE_NAME, description=refund_desc,
                )

                session.add(UsageLog(
                    account_id=subject.id,
                    feature_type=feature_type or 'unknown',
                    points_deducted=0,
                    status='failed',
                    log_metadata={'error': (reason or '')[:500], 'refunded_points': points},
                ))

                session.commit()
                logger.info(f"點數已退還: {user_id} (+{points}), 餘額: {new_balance}")
                return True

            except Exception:
                session.rollback()
                logger.exception(f"退還點數失敗: {user_id}")
                return False

    def get_works_summary(self, user_id):
        """各功能已完成的作品數 {feature_type: count}（只算成功的 usage_logs）。"""
        with get_session() as session:
            subject = self._backend.resolve(session, user_id)
            if not subject:
                return {}
            rows = (
                session.query(UsageLog.feature_type, func.count(UsageLog.id))
                .filter(
                    UsageLog.account_id == subject.id,
                    UsageLog.status == 'completed',
                )
                .group_by(UsageLog.feature_type)
                .all()
            )
            return {feature_type: count for feature_type, count in rows}

    def get_point_history(self, user_id, limit=10):
        """查詢交易記錄"""
        with get_session() as session:
            subject = self._backend.resolve(session, user_id)
            if not subject:
                return []
            rows = self._backend.history_rows(session, subject, limit=limit)
            return [_transaction_to_history_dict(t) for t in rows]
