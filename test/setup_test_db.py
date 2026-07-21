#!/usr/bin/env python3
"""
本地測試資料庫建表腳本（路 A：SQLAlchemy create_all）

用 models/*.py 的定義，在「本機 Postgres」上一鍵建出這個服務需要的所有表。
等價於 jotta 的 db:migrate，但直接照 SQLAlchemy model 建，不需要 altide-landing-page
的 schema.sql。

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


def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("❌ DATABASE_URL 未設定，請先在 .env 填好本機資料庫連線字串。")
        sys.exit(1)

    _assert_local(database_url)

    from sqlalchemy import text
    from models.database import init_database, Base
    import models  # noqa: F401  觸發所有 model 註冊到 Base.metadata

    engine = init_database()

    # create_all 不會自動建 schema，grandpa_yin.* 需要的 schema 先手動建
    schemas = {
        t.schema for t in Base.metadata.tables.values() if t.schema
    }
    with engine.begin() as conn:
        for schema in sorted(schemas):
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
            print(f"📁 schema 就緒：{schema}")

    Base.metadata.create_all(engine)

    table_names = sorted(
        f"{t.schema + '.' if t.schema else ''}{t.name}"
        for t in Base.metadata.tables.values()
    )
    print(f"✅ 已建立 {len(table_names)} 張表：")
    for name in table_names:
        print(f"   - {name}")
    print("\n🎉 本地測試資料庫已就緒，可以啟動服務了。")


if __name__ == "__main__":
    main()
