#!/usr/bin/env python3
"""
加點腳本：指定「加誰的點數」

用顯示名稱或 LINE userId 指定對象，加點前會先印出對象與餘額讓你確認。
加完的紀錄用戶端輸入「歷史」就看得到。

使用方式:
    python scripts/add_points.py <顯示名稱或 LINE userId> <點數> [--reason 說明] [--yes]

範例:
    python scripts/add_points.py 王小明 50                    # 用名字找人
    python scripts/add_points.py U1a2b3c4d5e6f... 100         # 用 userId 精準指定
    python scripts/add_points.py 王小明 50 --reason 朋友介紹   # 自訂紀錄說明
    python scripts/add_points.py U1a2b3c4d5e6f... 50 --yes    # 跳過確認（給自動化用）

名字有多人相符時會列出清單讓你挑；--yes 模式下則直接中止並要求改用 userId，
避免在無人看著的情況下把點數加給錯的人。
"""

import os
import sys
import argparse
import unicodedata

# 將專案根目錄加入 Python 路徑
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 確保在 Windows 終端也能輸出中文/emoji
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

from dotenv import load_dotenv

from src.models.database import init_database, get_session
from src.services.member_directory import MemberDirectory, display_name_of
from src.services.member_service import MemberService

DEFAULT_REASON = '管理員手動增加'
CONFIRM_ANSWERS = {'y', 'yes', '是'}


def _fit(text, width):
    """裁切／補滿到指定的終端欄寬（中文字算兩欄，欄位才不會歪掉）"""
    def cols(c):
        return 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1

    used = 0
    kept = []
    for char in text:
        if used + cols(char) > width:
            break
        kept.append(char)
        used += cols(char)
    return ''.join(kept) + ' ' * (width - used)


def select_match(matches, identifier, *, assume_yes, prompt=input, out=print):
    """從查詢結果挑出唯一對象；挑不出來回傳 None。

    同名的人不只一個時，把清單攤開讓操作者挑編號——這裡猜錯就是把點數
    加到別人頭上，所以寧可多問一次。
    """
    if not matches:
        out(f"❌ 找不到會員：{identifier}")
        return None

    if len(matches) == 1:
        return matches[0]

    out(f"\n⚠️  找到 {len(matches)} 位名稱相符的會員：\n")
    out(f"{_fit('  #', 5)}{_fit('顯示名稱', 20)}{_fit('LINE UID', 35)}{'餘額':>6}")
    out("-" * 66)
    for i, match in enumerate(matches, start=1):
        out(f"{_fit(f'{i:>3}.', 5)}{_fit(display_name_of(match), 20)}"
            f"{_fit(match.line_uid, 35)}{match.subject.points_balance:>6}")

    if assume_yes:
        out("\n❌ --yes 模式不會自動選人，請改用上面的 LINE UID 精準指定。")
        return None

    answer = prompt("\n要加給哪一位？輸入編號（直接按 Enter 取消）: ").strip()
    if not answer:
        out("已取消，沒有任何變更。")
        return None
    if not answer.isdigit() or not (1 <= int(answer) <= len(matches)):
        out(f"❌ 請輸入 1～{len(matches)} 之間的編號。")
        return None
    return matches[int(answer) - 1]


def confirm(target, points, reason, *, prompt=input, out=print):
    """加點前的最後確認畫面"""
    out("\n" + "=" * 60)
    out("✋ 請確認")
    out("=" * 60)
    out(f"對象     : {target['display_name']}")
    out(f"LINE UID : {target['line_uid']}")
    out(f"目前餘額 : {target['points']} 點")
    out(f"本次加點 : +{points} 點")
    out(f"加完餘額 : {target['points'] + points} 點")
    out(f"說明     : {reason}")
    out("=" * 60)

    answer = prompt("\n確定要加點嗎？(y/n): ").strip().lower()
    return answer in CONFIRM_ANSWERS


def add_points(identifier, points, reason, assume_yes):
    load_dotenv()

    if not os.getenv("DATABASE_URL"):
        print("❌ 未設定 DATABASE_URL 環境變數，請在 .env 中設定")
        return False

    init_database()

    # 先在唯讀 session 內把需要的值抓成純 Python 值：MemberService.add_points
    # 會另開自己的 session，ORM 物件跨 session 用不得。
    with get_session() as session:
        match = select_match(
            MemberDirectory().search(session, identifier),
            identifier,
            assume_yes=assume_yes,
        )
        if not match:
            return False
        target = {
            'display_name': display_name_of(match),
            'line_uid': match.line_uid,
            'points': match.subject.points_balance,
        }

    if not assume_yes and not confirm(target, points, reason):
        print("已取消，沒有任何變更。")
        return False

    member_service = MemberService()
    ok = member_service.add_points(
        user_id=target['line_uid'],
        points=points,
        transaction_type='admin_add',
        description=reason,
    )
    if not ok:
        print("❌ 加點失敗，請看上面的錯誤訊息或 log。")
        return False

    # 重新查一次餘額，印出資料庫實際的結果而不是我們自己算的預期值
    member = member_service.get_member_info(target['line_uid'])
    balance = member['points'] if member else target['points'] + points
    print(f"\n✅ 已為 {target['display_name']} 加 {points} 點，目前餘額 {balance} 點。")
    return True


def main():
    parser = argparse.ArgumentParser(
        description='為指定會員加點數',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='範例：python scripts/add_points.py 王小明 50 --reason 朋友介紹',
    )
    parser.add_argument('identifier', help='LINE 顯示名稱（模糊查）或 LINE userId（精準查）')
    parser.add_argument('points', type=int, help='要增加的點數（正整數）')
    parser.add_argument('--reason', default=DEFAULT_REASON,
                        help=f'交易紀錄上的說明（預設「{DEFAULT_REASON}」，用戶輸入「歷史」看得到）')
    parser.add_argument('--yes', action='store_true',
                        help='跳過確認直接加點；此模式下名字有多人相符會中止')
    args = parser.parse_args()

    if args.points <= 0:
        parser.error('點數必須是正整數（這支腳本只加點，不扣點）')

    try:
        success = add_points(args.identifier, args.points, args.reason, args.yes)
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ 加點時發生錯誤: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
