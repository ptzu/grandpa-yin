#!/usr/bin/env python3
"""Storage 清理腳本（暫存圖孤兒掃除 + 成品保留期到期回收）

Supabase Storage 沒有 S3 那種 lifecycle policy 可以設定，只能自己掃。

bucket 裡有兩種東西，壽命差了一個數量級，所以分成兩區處理：

── 暫存圖（`<功能名>/…`）── 預設 24 小時
用戶上傳的原圖。正常走完流程或取消都會即時刪除，但以下情況會留下孤兒：

  - 用戶傳了照片就不再回覆，狀態被 cleanup_user_states.py 清掉，圖沒人管
  - 部署／重啟打斷流程
  - 上傳成功但寫狀態失敗

判定條件（兩個都要成立才刪）：
  1. 物件建立時間超過 --hours（預設 24）
  2. 沒有任何存活的 bot_session 引用它

第 2 點讓這支腳本與 cleanup_user_states.py 的執行順序無關，也不會誤刪正在
流程中的用戶的照片。

── 成品（`results/…`）── 預設 31 天
回傳給用戶的圖片／影片，刻意留著讓對話紀錄 30 天內都不破圖（見
src/services/result_archive.py）。它們**不會**被任何 bot_session 引用，所以
一定要跟暫存圖分開判定——套用 24 小時那條規則的話，用戶隔天回頭看就全破了。
預設比 signed URL 的 30 天多留一天，避免剛好在到期邊界上把還看得到的東西刪掉。

使用方式:
    python scripts/cleanup_storage.py                # 試跑，只列出不刪
    python scripts/cleanup_storage.py --apply        # 真的刪除
    python scripts/cleanup_storage.py --hours 72 --apply
    python scripts/cleanup_storage.py --result-days 90 --apply
"""

import os
import sys
import argparse
from datetime import datetime, timedelta, timezone

# 將專案根目錄加入 Python 路徑
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from src.models.database import init_database, get_session
from src.models.bot_session import BotSession
from src.services.storage_service import StorageService
from src.services.result_archive import RESULT_PREFIX, RETENTION_DAYS

# 比 signed URL 的有效天數多留一天，不要在到期邊界上跟用戶搶
DEFAULT_RESULT_DAYS = RETENTION_DAYS + 1


def parse_timestamp(value):
    """解析 Supabase 回傳的 ISO 8601 時間；解析不了回傳 None（保守不刪）"""
    if not value:
        return None

    text = value.replace("Z", "+00:00")
    # fromisoformat 在 3.9 只吃到微秒，多餘位數要截掉
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        for char in tail:
            if char.isdigit():
                digits += char
            else:
                break
        offset = tail[len(digits):]
        text = f"{head}.{digits[:6]}{offset}"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def collect_referenced_keys():
    """收集所有存活 bot_session 仍在引用的 object key"""
    referenced = set()
    with get_session() as session:
        for (metadata,) in session.query(BotSession.state_metadata).all():
            if isinstance(metadata, dict) and metadata.get("image_key"):
                referenced.add(metadata["image_key"])
    return referenced


def collect_objects(storage):
    """列出 bucket 內所有暫存圖（根層物件 + 每個功能資料夾底下的物件）

    刻意跳過 results/：成品有自己的保留期，混進來會被 24 小時的規則刪掉。
    """
    objects = list(storage.list_objects(""))
    for folder in storage.list_folders(""):
        if folder == RESULT_PREFIX:
            continue
        objects.extend(storage.list_objects(folder))
    return objects


def collect_expired_results(storage, days):
    """列出 results/ 底下超過保留期的成品

    不看 bot_session 引用——成品的壽命只由時間決定，流程早就結束了。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    expired = []
    unknown = 0

    for item in storage.list_objects(RESULT_PREFIX):
        created_at = parse_timestamp(item.get("created_at"))
        if created_at is None:
            # 時間讀不到就不動它，寧可多留也不要刪掉用戶還看得到的東西
            unknown += 1
            continue
        if created_at <= cutoff:
            expired.append((item["key"], created_at))

    return expired, unknown


def delete_batch(storage, label, items, apply_changes):
    """列出並（在 --apply 時）刪除一組物件；回傳是否成功

    items: [(key, created_at), ...]
    """
    if not items:
        print(f"\n✅ {label}：沒有需要清理的物件")
        return True

    preview = items[:20]
    print(f"\n{label} 將刪除 {len(items)} 個：")
    for key, created_at in preview:
        age_hours = (datetime.now(timezone.utc) - created_at).total_seconds() / 3600
        if age_hours >= 48:
            age = f"{age_hours / 24:.1f} 天前"
        else:
            age = f"{age_hours:.1f} 小時前"
        print(f"   - {key}  ({age})")
    if len(items) > len(preview):
        print(f"   … 另外還有 {len(items) - len(preview)} 個")

    if not apply_changes:
        return True

    print(f"\n🗑️  刪除 {len(items)} 個物件...")
    deleted = storage.delete_images(key for key, _ in items)
    print(f"   已刪除 {deleted} / {len(items)}")
    return deleted == len(items)


def cleanup_storage(hours=24, apply_changes=False, result_days=DEFAULT_RESULT_DAYS):
    print("=" * 60)
    print("🧹 Storage 清理")
    print("=" * 60)

    load_dotenv()

    storage = StorageService()
    if not storage.is_configured():
        print("❌ 錯誤：未設定 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
        return False

    if not os.getenv("DATABASE_URL"):
        print("❌ 錯誤：未設定 DATABASE_URL（需要它才能確認哪些圖仍在使用中）")
        return False

    print(f"📦 Bucket：{storage.bucket}")
    print(f"⏳ 暫存圖：清理超過 {hours} 小時且無人引用的")
    print(f"⏳ 成品（{RESULT_PREFIX}/）：清理超過 {result_days} 天的")
    print(f"🔧 模式：{'實際刪除' if apply_changes else '試跑（不會刪除任何東西）'}")

    try:
        init_database()

        # ---- 第一區：暫存圖的孤兒物件 ----
        print("\n🔍 讀取仍在使用中的圖片...")
        referenced = collect_referenced_keys()
        print(f"   {len(referenced)} 張圖正被存活的對話狀態引用（一律保留）")

        print("🔍 列出 bucket 內的暫存圖...")
        objects = collect_objects(storage)
        print(f"   共 {len(objects)} 個物件")

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        orphans = []
        kept_referenced = 0
        kept_fresh = 0
        kept_unknown = 0

        for item in objects:
            key = item["key"]
            if key in referenced:
                kept_referenced += 1
                continue

            created_at = parse_timestamp(item.get("created_at"))
            if created_at is None:
                # 時間讀不到就不動它，寧可留著也不要誤刪
                kept_unknown += 1
                continue

            if created_at > cutoff:
                kept_fresh += 1
                continue

            orphans.append((key, created_at))

        print("\n📊 暫存圖盤點")
        print(f"   保留（使用中）    ：{kept_referenced}")
        print(f"   保留（還很新）    ：{kept_fresh}")
        print(f"   保留（時間不明）  ：{kept_unknown}")
        print(f"   可刪除的孤兒物件  ：{len(orphans)}")

        # ---- 第二區：保留期到期的成品 ----
        print(f"\n🔍 列出 {RESULT_PREFIX}/ 底下的成品...")
        expired, results_unknown = collect_expired_results(storage, result_days)
        print(f"   保留期已到    ：{len(expired)}")
        if results_unknown:
            print(f"   保留（時間不明）：{results_unknown}")

        ok = delete_batch(storage, "暫存圖", orphans, apply_changes)
        ok = delete_batch(storage, "成品", expired, apply_changes) and ok

        if not apply_changes and (orphans or expired):
            print("\n💡 這是試跑。確認無誤後加上 --apply 才會真的刪除。")
            return True

        print("\n" + "=" * 60)
        print("✅ 完成" if ok else "⚠️  完成，但有部分物件刪除失敗")
        print("=" * 60)
        return ok

    except Exception as e:
        print(f"\n❌ 清理失敗: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def main():
    parser = argparse.ArgumentParser(description='清理 Storage 的孤兒暫存圖與到期成品')
    parser.add_argument('--hours', type=int, default=24,
                        help='暫存圖：刪除超過指定小時數的物件 (預設: 24)')
    parser.add_argument('--result-days', type=int, default=DEFAULT_RESULT_DAYS,
                        help=f'成品：刪除超過指定天數的物件 (預設: {DEFAULT_RESULT_DAYS})')
    parser.add_argument('--apply', action='store_true',
                        help='真的執行刪除；未加此旗標時只試跑列出')

    args = parser.parse_args()

    if args.hours < 1:
        print("❌ 錯誤：--hours 至少要 1，避免刪到正在處理中的圖片")
        sys.exit(1)

    if args.result_days < RETENTION_DAYS:
        # 比 signed URL 的有效期短 = 用戶點得到網址卻抓不到檔案，比直接不留更糟
        print(f"❌ 錯誤：--result-days 不得小於成品的保留承諾 {RETENTION_DAYS} 天")
        sys.exit(1)

    sys.exit(0 if cleanup_storage(args.hours, args.apply, args.result_days) else 1)


if __name__ == "__main__":
    main()
