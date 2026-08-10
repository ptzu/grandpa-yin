#!/usr/bin/env python3
"""
本地測試資料庫建表腳本（路 A：SQLAlchemy create_all）

用 src/models/*.py 的定義，在「本機 Postgres」上一鍵建出這個服務需要的表。
等價於 jotta 的 db:migrate，但直接照 SQLAlchemy model 建，不需要 altide-landing-page
的 schema.sql。

建哪些表取決於 DEPLOY_MODE：
  standalone → 只建 grandpa_yin.*，真正在零 Altide 表的環境下驗證獨立性
  platform   → 連 Altide 共用層 public.* 一起建（本地模擬整合環境）

安全鎖：只允許 DATABASE_URL 指向本機（localhost / 127.0.0.1 / socket），
避免誤把測試表建到線上 Supabase。

用法（於 repo 根目錄，DATABASE_URL 已在 .env 設好）：
    python test/setup_test_db.py
"""
import os
import sys
from urllib.parse import urlparse

# 讓 `python test/setup_test_db.py` 從 repo 根目錄跑得起來
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}


def _assert_local(database_url: str) -> None:
    """安全鎖：非本機一律拒絕，避免誤建到線上。"""
    host = urlparse(database_url).hostname or ""
    if host not in LOCAL_HOSTS:
        print(f"❌ 拒絕執行：DATABASE_URL 指向非本機主機 '{host}'。")
        print("   這支腳本只能對本機資料庫建表，避免污染線上。")
        sys.exit(1)
    print(f"🔒 安全檢查通過：目標主機為本機 ('{host or 'socket'}')")


PLATFORM_TABLES = ("accounts", "linked_identities", "transactions")


def _warn_if_stray_platform_tables(engine) -> None:
    """standalone 環境不該有 Altide 共用層的表。

    舊版腳本會一併建出 public.*，留下來的話會讓整合 migration
    （a7b8c9d0e1f2）誤判成 platform 模式，錯誤地補上跨 schema 外鍵。
    這裡只提醒不刪除——刪表是破壞性操作，交給人決定。
    """
    from sqlalchemy import text

    with engine.connect() as conn:
        stray = [
            name for name in PLATFORM_TABLES
            if conn.execute(
                text("SELECT to_regclass(:qualified)"), {"qualified": f"public.{name}"}
            ).scalar() is not None
        ]

    if not stray:
        return

    print("\n⚠️  偵測到 Altide 共用層的表殘留在這個 standalone 資料庫：")
    for name in stray:
        print(f"     - public.{name}")
    print("   （多半是舊版腳本建的）留著會讓整合 migration 把此庫誤判為 platform 模式。")
    print("   確認裡面沒有需要的資料後，可以這樣清掉：")
    print(f"     psql <db> -c 'DROP TABLE IF EXISTS {', '.join('public.' + n for n in stray)} CASCADE;'")


def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("❌ DATABASE_URL 未設定，請先在 .env 填好本機資料庫連線字串。")
        sys.exit(1)

    _assert_local(database_url)

    from sqlalchemy import text
    from src.models.database import init_database, Base, OWNED_SCHEMA
    from src import models  # noqa: F401  觸發所有 model 註冊到 Base.metadata

    mode = os.getenv("DEPLOY_MODE", "platform").strip().lower()
    print(f"🚩 DEPLOY_MODE = {mode}")

    # standalone 只建自有的 grandpa_yin.*。共用層 public.*（accounts /
    # transactions / linked_identities）屬於 Altide，standalone 根本不會用到——
    # 建了會讓本地環境「看起來像 platform」，既沒在測真正的獨立性，也會讓
    # 整合 migration（a7b8c9d0e1f2）用「public.accounts 是否存在」判斷模式時誤判。
    if mode == "standalone":
        tables = [t for t in Base.metadata.sorted_tables if t.schema == OWNED_SCHEMA]
        print(f"   → 只建立 {OWNED_SCHEMA}.*（不建 Altide 共用層）")
    else:
        tables = list(Base.metadata.sorted_tables)
        print("   → 建立全部（含 Altide 共用層 public.*）")

    engine = init_database()

    # create_all 不會自動建 schema，grandpa_yin.* 需要的 schema 先手動建
    schemas = {t.schema for t in tables if t.schema}
    with engine.begin() as conn:
        for schema in sorted(schemas):
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
            print(f"📁 schema 就緒：{schema}")

    Base.metadata.create_all(engine, tables=tables)

    table_names = sorted(
        f"{t.schema + '.' if t.schema else ''}{t.name}" for t in tables
    )
    print(f"✅ 已建立 {len(table_names)} 張表：")
    for name in table_names:
        print(f"   - {name}")

    if mode == "standalone":
        _warn_if_stray_platform_tables(engine)

    # 表由 create_all 建好後，把 grandpa_yin 的 Alembic 版本標記到 head，讓本地 DB
    # 與 migration 一致：日後改 model → 產生 migration → `alembic upgrade head`
    # 只套差異。用 stamp 而非 upgrade，是因為表已經由 create_all 建出來了。
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    from alembic.config import Config
    from alembic import command

    alembic_cfg = Config(os.path.join(repo_root, "alembic.ini"))
    command.stamp(alembic_cfg, "head")
    print("🏷️  已將 grandpa_yin 的 Alembic 版本標記到 head")

    print("\n🎉 本地測試資料庫已就緒，可以啟動服務了。")


if __name__ == "__main__":
    main()
