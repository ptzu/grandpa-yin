"""platform integration: add account_id FK to public.accounts

Re-adds the cross-schema FK grandpa_yin.{bot_sessions,usage_logs,user_profiles}
.account_id -> public.accounts.id that the baseline deliberately omits (so the
product can build standalone).

This runs in the single linear history but is *mode-aware and idempotent*:

  * standalone  -> public.accounts does not exist -> skip (no-op).
  * platform, fresh -> add the FKs.
  * platform, existing (FK already created by Altide's schema.sql) -> detect an
    existing FK on account_id and skip that table.

So `alembic upgrade head` is safe in every environment.

Revision ID: a7b8c9d0e1f2
Revises: f1a2b3c4d5e6
Create Date: 2026-08-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ('bot_sessions', 'usage_logs', 'user_profiles')
_OWNED_SCHEMA = 'grandpa_yin'


def _fk_name(table: str) -> str:
    return f'fk_{table}_account_id_accounts'


def _account_fk_exists(conn, table: str) -> bool:
    """True if any FK on <table>.account_id already exists (regardless of name,
    so FKs created by Altide's schema.sql are also detected)."""
    return conn.execute(sa.text(
        """
        SELECT 1
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = :schema
          AND tc.table_name = :table
          AND kcu.column_name = 'account_id'
        LIMIT 1
        """
    ), {"schema": _OWNED_SCHEMA, "table": table}).scalar() is not None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()

    # Standalone: no shared accounts table to reference -> nothing to do.
    if conn.execute(sa.text("SELECT to_regclass('public.accounts')")).scalar() is None:
        return

    for table in _TABLES:
        if _account_fk_exists(conn, table):
            continue  # already linked (fresh run earlier, or by schema.sql)
        op.create_foreign_key(
            _fk_name(table), table, 'accounts',
            ['account_id'], ['id'],
            source_schema=_OWNED_SCHEMA, referent_schema='public',
        )


def downgrade() -> None:
    """Downgrade schema.

    Only drops the FKs this migration created (by our deterministic name); FKs
    that predate it (e.g. from schema.sql) are left untouched.
    """
    conn = op.get_bind()
    if conn.execute(sa.text("SELECT to_regclass('public.accounts')")).scalar() is None:
        return
    for table in _TABLES:
        exists = conn.execute(sa.text(
            """
            SELECT 1 FROM information_schema.table_constraints
            WHERE constraint_type = 'FOREIGN KEY'
              AND table_schema = :schema AND table_name = :table
              AND constraint_name = :name
            LIMIT 1
            """
        ), {"schema": _OWNED_SCHEMA, "table": table, "name": _fk_name(table)}).scalar()
        if exists:
            op.drop_constraint(_fk_name(table), table, schema=_OWNED_SCHEMA, type_='foreignkey')
