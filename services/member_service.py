from models.database import get_session
from models.account import Account
from models.linked_identity import LinkedIdentity
from models.grandpa_yin_profile import GrandpaYinProfile
from models.transaction import Transaction
from models.usage_log import UsageLog


LINE_PROVIDER = 'line'
SERVICE_NAME = 'silver-grandpa'


def _resolve_account(session, line_uid, for_update=False):
    """LINE UID → Account；未綁定則回傳 None"""
    identity = (
        session.query(LinkedIdentity)
        .filter_by(provider=LINE_PROVIDER, provider_uid=line_uid)
        .first()
    )
    if not identity:
        return None
    q = session.query(Account).filter_by(id=identity.account_id)
    if for_update:
        q = q.with_for_update()
    return q.first()


def _member_dict(account, profile, line_uid):
    """組成向後相容的 member dict"""
    return {
        'user_id': line_uid,
        'display_name': (profile.display_name if profile else None) or '使用者',
        'picture_url': None,  # 不再儲存，顯示時即時查 LINE API
        'email': None,        # shadow account 無 email；有 auth_user 時從 auth.users 取
        'points': account.points_balance,
        'status': profile.status if profile else 'normal',
        'created_at': account.created_at.isoformat() if account.created_at else None,
        'updated_at': profile.updated_at.isoformat() if profile and profile.updated_at else None,
    }


def _transaction_to_history_dict(t):
    """把 Transaction 轉成舊 history API 相容的 dict（推斷 transaction_type）"""
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
    """會員服務層 - 對外 API 以 LINE UID 為主鍵，內部解析至 account_id"""

    def get_or_create_member(self, user_id, display_name=None, picture_url=None, email=None):
        """取得或建立會員。新 LINE UID 自動建立 shadow account + linked_identity + grandpa_yin_profile。"""
        with get_session() as session:
            account = _resolve_account(session, user_id)

            if account:
                profile = session.query(GrandpaYinProfile).filter_by(account_id=account.id).first()
                # 更新 display_name（debug 方便看是誰）
                if profile and display_name and profile.display_name != display_name:
                    profile.display_name = display_name
                    session.commit()
                    print(f"✅ 會員資料已更新: {user_id}")
                return _member_dict(account, profile, user_id)

            # 新會員：建 shadow account + linked_identity + grandpa_yin_profile
            account = Account(points_balance=0, is_admin=False)
            session.add(account)
            session.flush()  # 取得 account.id

            session.add(LinkedIdentity(
                account_id=account.id,
                provider=LINE_PROVIDER,
                provider_uid=user_id,
            ))
            profile = GrandpaYinProfile(
                account_id=account.id,
                display_name=display_name or '使用者',
                status='normal',
                is_tutorial_completed=False,
            )
            session.add(profile)
            session.commit()
            print(f"✅ 新會員已建立: {user_id} ({display_name})")
            return _member_dict(account, profile, user_id)

    def get_member_info(self, user_id):
        """查詢會員完整資訊"""
        with get_session() as session:
            account = _resolve_account(session, user_id)
            if not account:
                return None
            profile = session.query(GrandpaYinProfile).filter_by(account_id=account.id).first()
            return _member_dict(account, profile, user_id)

    def get_member_points(self, user_id):
        """查詢會員點數"""
        with get_session() as session:
            account = _resolve_account(session, user_id)
            return account.points_balance if account else None

    def add_points(self, user_id, points, transaction_type='earn', description=None):
        """增加點數並記錄交易。transaction_type 為舊介面相容，併入 description。"""
        if points <= 0:
            print(f"❌ 點數必須為正數: {points}")
            return False

        with get_session() as session:
            try:
                account = _resolve_account(session, user_id, for_update=True)
                if not account:
                    print(f"❌ 會員不存在: {user_id}")
                    return False

                account.points_balance += points
                new_balance = account.points_balance

                session.add(Transaction(
                    account_id=account.id,
                    amount=points,
                    service=SERVICE_NAME,
                    balance_after=new_balance,
                    description=description or f'加點（{transaction_type}）',
                ))

                session.commit()
                print(f"✅ 點數已增加: {user_id} (+{points}), 餘額: {new_balance}")
                return True

            except Exception as e:
                session.rollback()
                print(f"❌ 增加點數失敗: {str(e)}")
                return False

    def deduct_points(self, user_id, points, description=None, feature_type=None):
        """扣除點數：同時寫 public.transactions（管道）與 grandpa_yin.usage_logs（細節）"""
        if points <= 0:
            print(f"❌ 點數必須為正數: {points}")
            return False

        with get_session() as session:
            try:
                account = _resolve_account(session, user_id, for_update=True)
                if not account:
                    print(f"❌ 會員不存在: {user_id}")
                    return False

                if account.points_balance < points:
                    print(f"❌ 點數不足: {user_id}, 需要 {points}, 目前 {account.points_balance}")
                    return False

                account.points_balance -= points
                new_balance = account.points_balance

                channel_desc = f'銀爺爺：{feature_type}' if feature_type else '銀爺爺功能扣點'
                session.add(Transaction(
                    account_id=account.id,
                    amount=-points,
                    service=SERVICE_NAME,
                    balance_after=new_balance,
                    description=channel_desc,
                ))

                session.add(UsageLog(
                    account_id=account.id,
                    feature_type=feature_type or 'unknown',
                    points_deducted=points,
                    status='completed',
                    log_metadata={'description': description} if description else {},
                ))

                session.commit()
                print(f"✅ 點數已扣除: {user_id} (-{points}), 餘額: {new_balance}")
                return True

            except Exception as e:
                session.rollback()
                print(f"❌ 扣除點數失敗: {str(e)}")
                return False

    def get_point_history(self, user_id, limit=10):
        """查詢交易記錄"""
        with get_session() as session:
            account = _resolve_account(session, user_id)
            if not account:
                return []
            transactions = (
                session.query(Transaction)
                .filter_by(account_id=account.id)
                .order_by(Transaction.created_at.desc())
                .limit(limit)
                .all()
            )
            return [_transaction_to_history_dict(t) for t in transactions]

    def update_member_status(self, user_id, status):
        """更新 grandpa_yin 端的會員狀態"""
        valid_statuses = ['normal', 'vip', 'suspended', 'banned']
        if status not in valid_statuses:
            print(f"❌ 無效的狀態: {status}")
            return False

        with get_session() as session:
            try:
                account = _resolve_account(session, user_id)
                if not account:
                    print(f"❌ 會員不存在: {user_id}")
                    return False

                profile = session.query(GrandpaYinProfile).filter_by(account_id=account.id).first()
                if not profile:
                    print(f"❌ 長輩資料不存在: {user_id}")
                    return False

                old_status = profile.status
                profile.status = status
                session.commit()
                print(f"✅ 會員狀態已更新: {user_id} ({old_status} → {status})")
                return True

            except Exception as e:
                session.rollback()
                print(f"❌ 更新狀態失敗: {str(e)}")
                return False
