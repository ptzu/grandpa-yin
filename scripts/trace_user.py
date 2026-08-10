#!/usr/bin/env python3
"""
用戶點數軌跡追蹤腳本
一次印出某用戶的帳號、餘額、交易流水、功能使用記錄、對話狀態與對帳摘要，
用於客訴時快速追查扣點問題。唯讀，不會修改任何資料。

使用方式:
    python scripts/trace_user.py <顯示名稱或 LINE userId> [--limit N]

範例:
    python scripts/trace_user.py U1a2b3c4d5e6f...       # 用 userId 精準查
    python scripts/trace_user.py 王小明 --limit 30       # 用名字模糊查
"""

import os
import sys
import argparse

# 將專案根目錄加入 Python 路徑
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 確保在 Windows 終端也能輸出中文/emoji
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

from dotenv import load_dotenv
from sqlalchemy import func

from src.models.database import init_database, get_session
from src.models.account import Account
from src.models.linked_identity import LinkedIdentity
from src.models.grandpa_yin_profile import GrandpaYinProfile
from src.models.transaction import Transaction
from src.models.usage_log import UsageLog
from src.models.bot_session import BotSession

LINE_PROVIDER = 'line'

STATUS_MAP = {'normal': '正常', 'vip': 'VIP', 'suspended': '停用', 'banned': '黑名單'}


def looks_like_line_uid(s: str) -> bool:
    """LINE userId 格式：U 開頭 + 32 個十六進位字元"""
    if len(s) != 33 or not s.startswith('U'):
        return False
    try:
        int(s[1:], 16)
        return True
    except ValueError:
        return False


def fmt_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "—"


def resolve_account_by_uid(session, line_uid):
    """LINE UID → Account；未綁定回傳 None"""
    identity = (
        session.query(LinkedIdentity)
        .filter_by(provider=LINE_PROVIDER, provider_uid=line_uid)
        .first()
    )
    if not identity:
        return None
    return session.query(Account).filter_by(id=identity.account_id).first()


def find_candidates_by_name(session, name):
    """用顯示名稱模糊查，回傳 [(account, profile, line_uid), ...]"""
    rows = (
        session.query(GrandpaYinProfile, LinkedIdentity)
        .join(LinkedIdentity, LinkedIdentity.account_id == GrandpaYinProfile.account_id)
        .filter(LinkedIdentity.provider == LINE_PROVIDER)
        .filter(GrandpaYinProfile.display_name.ilike(f"%{name}%"))
        .all()
    )
    results = []
    for profile, identity in rows:
        account = session.query(Account).filter_by(id=profile.account_id).first()
        results.append((account, profile, identity.provider_uid))
    return results


def print_section(title):
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


def print_basic_info(session, account, line_uid):
    profile = session.query(GrandpaYinProfile).filter_by(account_id=account.id).first()
    display_name = profile.display_name if profile else None
    status = profile.status if profile else 'normal'

    print_section("👤 帳號基本資料")
    print(f"顯示名稱   : {display_name or '（無）'}")
    print(f"LINE UID   : {line_uid}")
    print(f"account_id : {account.id}")
    print(f"會員狀態   : {STATUS_MAP.get(status, status)}")
    print(f"目前餘額   : {account.points_balance} 點")
    print(f"管理員     : {'是' if account.is_admin else '否'}")
    print(f"註冊時間   : {fmt_dt(account.created_at)}")


def print_transactions(session, account, limit):
    txs = (
        session.query(Transaction)
        .filter_by(account_id=account.id)
        .order_by(Transaction.created_at.desc())
        .limit(limit)
        .all()
    )
    print_section(f"💳 交易流水（最近 {limit} 筆）")
    if not txs:
        print("（無交易記錄）")
        return
    print(f"{'時間':<21} {'金額':>7} {'餘額':>7}  說明")
    print("-" * 60)
    for t in txs:
        amount_str = f"+{t.amount}" if t.amount > 0 else str(t.amount)
        balance = t.balance_after if t.balance_after is not None else "—"
        print(f"{fmt_dt(t.created_at):<21} {amount_str:>7} {str(balance):>7}  {t.description or ''}")


def print_usage_logs(session, account, limit):
    logs = (
        session.query(UsageLog)
        .filter_by(account_id=account.id)
        .order_by(UsageLog.created_at.desc())
        .limit(limit)
        .all()
    )
    print_section(f"🛠️  功能使用記錄（最近 {limit} 筆）")
    if not logs:
        print("（無使用記錄）")
        return
    for lg in logs:
        flag = "❌" if lg.status == 'failed' else "✅"
        line = f"{flag} {fmt_dt(lg.created_at)}  {lg.feature_type}  扣 {lg.points_deducted} 點  [{lg.status}]"
        print(line)
        # 失敗時把 metadata 裡的 error 印出來（成功的記錄通常沒有 error）
        meta = lg.log_metadata or {}
        error = meta.get('error')
        if error:
            print(f"     ↳ error: {error}")


def print_bot_session(session, account):
    bs = session.query(BotSession).filter_by(account_id=account.id).first()
    print_section("💬 目前對話狀態")
    if not bs:
        print("（無進行中的對話狀態）")
        return
    print(f"current_state : {bs.current_state}  (feature={bs.feature}, state={bs.state})")
    print(f"最後更新       : {fmt_dt(bs.updated_at)}")
    # state_metadata 可能含 base64 圖片，只印 key 與大小，避免洗版
    meta = bs.state_metadata or {}
    if meta:
        summary = ", ".join(f"{k}({len(str(v))} chars)" for k, v in meta.items())
        print(f"state_metadata : {summary}")


def print_reconciliation(session, account):
    total_in = (
        session.query(func.coalesce(func.sum(Transaction.amount), 0))
        .filter(Transaction.account_id == account.id, Transaction.amount > 0)
        .scalar()
    )
    total_out = (
        session.query(func.coalesce(func.sum(Transaction.amount), 0))
        .filter(Transaction.account_id == account.id, Transaction.amount < 0)
        .scalar()
    )
    tx_count = (
        session.query(func.count(Transaction.id))
        .filter(Transaction.account_id == account.id)
        .scalar()
    )
    failed_count = (
        session.query(func.count(UsageLog.id))
        .filter(UsageLog.account_id == account.id, UsageLog.status == 'failed')
        .scalar()
    )

    print_section("📊 對帳摘要")
    print(f"交易筆數     : {tx_count}")
    print(f"總收入       : +{total_in} 點")
    print(f"總支出       : {total_out} 點")
    print(f"交易淨額     : {total_in + total_out} 點")
    print(f"處理失敗次數 : {failed_count} 次")

    # 一致性檢查：最新一筆交易的餘額應等於帳號現有餘額
    latest = (
        session.query(Transaction)
        .filter_by(account_id=account.id)
        .order_by(Transaction.created_at.desc())
        .first()
    )
    print()
    if latest and latest.balance_after is not None:
        if latest.balance_after == account.points_balance:
            print(f"✅ 餘額一致：帳號餘額 {account.points_balance} = 最新交易餘額 {latest.balance_after}")
        else:
            print(f"⚠️  餘額不一致！帳號餘額 {account.points_balance} ≠ 最新交易餘額 "
                  f"{latest.balance_after}（可能有繞過交易記錄的直接改動，需人工追查）")
    else:
        print("ℹ️  無交易記錄可供餘額比對")


def trace(identifier, limit):
    load_dotenv()

    if not os.getenv("DATABASE_URL"):
        print("❌ 未設定 DATABASE_URL 環境變數，請在 .env 中設定")
        return False

    init_database()

    with get_session() as session:
        # 1. 找到目標帳號
        if looks_like_line_uid(identifier):
            line_uid = identifier
            account = resolve_account_by_uid(session, line_uid)
            if not account:
                print(f"❌ 找不到此 LINE UID 的會員：{line_uid}")
                return False
        else:
            candidates = find_candidates_by_name(session, identifier)
            if not candidates:
                print(f"❌ 找不到顯示名稱包含「{identifier}」的會員")
                return False
            if len(candidates) > 1:
                print(f"⚠️  找到 {len(candidates)} 位名稱相符的會員，請用 userId 重新查詢：\n")
                print(f"{'顯示名稱':<16} {'LINE UID':<35} {'餘額':>6}  註冊時間")
                print("-" * 80)
                for account, profile, uid in candidates:
                    name = (profile.display_name or '')[:14]
                    print(f"{name:<16} {uid:<35} {account.points_balance:>6}  {fmt_dt(account.created_at)}")
                return True
            account, _, line_uid = candidates[0]

        # 2. 印出完整軌跡
        print_basic_info(session, account, line_uid)
        print_transactions(session, account, limit)
        print_usage_logs(session, account, limit)
        print_bot_session(session, account)
        print_reconciliation(session, account)
        print()

    return True


def main():
    parser = argparse.ArgumentParser(description='追蹤某用戶的點數軌跡（唯讀）')
    parser.add_argument('identifier', help='LINE 顯示名稱（模糊查）或 LINE userId（精準查）')
    parser.add_argument('--limit', type=int, default=20,
                        help='交易與使用記錄的顯示筆數（預設 20）')
    args = parser.parse_args()

    try:
        success = trace(args.identifier, args.limit)
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ 查詢時發生錯誤: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
