#!/usr/bin/env python3
"""Storage 暫存圖清理腳本（孤兒物件掃除）

圖片編輯／照片意圖詢問會把用戶上傳的照片暫存進 Supabase Storage，正常走完
流程或取消都會即時刪除。但以下情況會留下孤兒物件：

  - 用戶傳了照片就不再回覆，狀態被 cleanup_user_states.py 清掉，圖沒人管
  - 部署／重啟打斷流程
  - 上傳成功但寫狀態失敗

Supabase Storage 沒有 S3 那種 lifecycle policy 可以設定，只能自己掃。

判定條件（兩個都要成立才刪）：
  1. 物件建立時間超過 --hours（預設 24）
  2. 沒有任何存活的 bot_session 引用它

第 2 點讓這支腳本與 cleanup_user_states.py 的執行順序無關，也不會誤刪正在
流程中的用戶的照片。

使用方式:
    python scripts/cleanup_storage.py                # 試跑，只列出不刪
    python scripts/cleanup_storage.py --apply        # 真的刪除
    python scripts/cleanup_storage.py --hours 72 --apply
"""

import os
import sys
import argparse
from datetime import datetime, timedelta, timezone

# 將專案根目錄加入 Python 路徑
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from models.database import init_database, get_session
from models.bot_session import BotSession
from services.storage_service import StorageService


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
    """列出 bucket 內所有暫存圖（根層物件 + 每個功能資料夾底下的物件）"""
    objects = list(storage.list_objects(""))
    for folder in storage.list_folders(""):
        objects.extend(storage.list_objects(folder))
    return objects


def cleanup_storage(hours=24, apply_changes=False):
    print("=" * 60)
    print("🧹 Storage 暫存圖清理")
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
    print(f"⏳ 清理超過 {hours} 小時的物件")
    print(f"🔧 模式：{'實際刪除' if apply_changes else '試跑（不會刪除任何東西）'}")

    try:
        init_database()

        print("\n🔍 讀取仍在使用中的圖片...")
        referenced = collect_referenced_keys()
        print(f"   {len(referenced)} 張圖正被存活的對話狀態引用（一律保留）")

        print("🔍 列出 bucket 內的物件...")
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

        print("\n📊 盤點結果")
        print(f"   保留（使用中）    ：{kept_referenced}")
        print(f"   保留（還很新）    ：{kept_fresh}")
        print(f"   保留（時間不明）  ：{kept_unknown}")
        print(f"   可刪除的孤兒物件  ：{len(orphans)}")

        if not orphans:
            print("\n✅ 沒有需要清理的孤兒物件")
            return True

        preview = orphans[:20]
        print("\n將刪除：")
        for key, created_at in preview:
            age_hours = (datetime.now(timezone.utc) - created_at).total_seconds() / 3600
            print(f"   - {key}  ({age_hours:.1f} 小時前)")
        if len(orphans) > len(preview):
            print(f"   … 另外還有 {len(orphans) - len(preview)} 個")

        if not apply_changes:
            print("\n💡 這是試跑。確認無誤後加上 --apply 才會真的刪除。")
            return True

        print(f"\n🗑️  刪除 {len(orphans)} 個物件...")
        deleted = storage.delete_images(key for key, _ in orphans)

        print("\n" + "=" * 60)
        print(f"✅ 完成：已刪除 {deleted} / {len(orphans)} 個孤兒物件")
        print("=" * 60)
        return deleted == len(orphans)

    except Exception as e:
        print(f"\n❌ 清理失敗: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def main():
    parser = argparse.ArgumentParser(description='清理 Storage 中的孤兒暫存圖')
    parser.add_argument('--hours', type=int, default=24,
                        help='刪除超過指定小時數的物件 (預設: 24)')
    parser.add_argument('--apply', action='store_true',
                        help='真的執行刪除；未加此旗標時只試跑列出')

    args = parser.parse_args()

    if args.hours < 1:
        print("❌ 錯誤：--hours 至少要 1，避免刪到正在處理中的圖片")
        sys.exit(1)

    sys.exit(0 if cleanup_storage(args.hours, args.apply) else 1)


if __name__ == "__main__":
    main()
