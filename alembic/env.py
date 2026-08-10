"""Alembic 環境設定。

範圍限定：只管理 `grandpa_yin.*` 產品表。共用層 `public.*`（accounts /
transactions / linked_identities）由 Altide（altide-landing-page/supabase）管理，
本 migration 一律不碰——見 include_object 過濾。
"""
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

# 讓 alembic 能 import 專案的 models
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from src.models.database import Base, OWNED_SCHEMA  # noqa: F401
from src import models  # noqa: F401  觸發所有 model 註冊到 Base.metadata

# OWNED_SCHEMA（= "grandpa_yin"）定義在 src/models/database.py，與
# test/setup_test_db.py 共用同一份定義。只有這個 schema 的物件會被 migration 管理。

config = context.config

# 連線字串一律從環境變數取得（本地 .env / Railway Variables），不寫進 alembic.ini
database_url = os.getenv("DATABASE_URL")
if not database_url:
    raise RuntimeError("DATABASE_URL 未設定，Alembic 無法連線")
config.set_main_option("sqlalchemy.url", database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_object(object, name, type_, reflected, compare_to):
    """只納入 grandpa_yin schema 的表；public.* 共用表一律略過。

    grandpa_yin 的表對 public.accounts 有外鍵，accounts 仍需存在於
    metadata 供外鍵解析，但因 schema 不符不會被產生 CREATE/DROP DDL。
    """
    if type_ == "table":
        return object.schema == OWNED_SCHEMA
    # 欄位/索引/約束等，交由其所屬表的 schema 連帶決定
    if type_ in ("column", "index", "unique_constraint", "foreign_key_constraint"):
        parent = getattr(object, "table", None)
        if parent is not None and parent.schema != OWNED_SCHEMA:
            return False
    return True


def _configure(connection=None, url=None):
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        include_schemas=True,               # 反射非預設 schema（grandpa_yin）
        include_object=include_object,      # 只管 grandpa_yin.*
        version_table="alembic_version",
        version_table_schema=OWNED_SCHEMA,  # 版本表放進 grandpa_yin，不污染 public
        compare_type=True,
        compare_server_default=True,
    )


def run_migrations_offline() -> None:
    _configure(url=config.get_main_option("sqlalchemy.url"))
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        # 先確保 schema 存在：version 表放在 grandpa_yin，Alembic 會在跑 migration
        # 前就嘗試建立它，若此時 schema 還沒建會失敗（雞生蛋）。
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{OWNED_SCHEMA}"'))
        connection.commit()
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
