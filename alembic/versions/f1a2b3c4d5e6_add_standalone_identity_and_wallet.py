"""add standalone identity and wallet

Adds grandpa_yin.subjects and grandpa_yin.wallet_transactions — the self-owned
identity + point ledger used when DEPLOY_MODE=standalone (replacing Altide's
public.accounts / linked_identities / transactions). These tables are created
in every environment; in platform mode they simply stay unused.

Revision ID: f1a2b3c4d5e6
Revises: a6e5ccf71d56
Create Date: 2026-08-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'a6e5ccf71d56'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('subjects',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('provider', sa.String(length=50), nullable=False),
        sa.Column('provider_uid', sa.String(length=255), nullable=False),
        sa.Column('points_balance', sa.Integer(), nullable=False),
        sa.Column('is_admin', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider', 'provider_uid', name='uq_subjects_provider_uid'),
        schema='grandpa_yin'
    )
    op.create_table('wallet_transactions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('subject_id', sa.UUID(), nullable=False),
        sa.Column('amount', sa.Integer(), nullable=False),
        sa.Column('service', sa.String(length=50), nullable=False),
        sa.Column('balance_after', sa.Integer(), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['subject_id'], ['grandpa_yin.subjects.id'], ),
        sa.PrimaryKeyConstraint('id'),
        schema='grandpa_yin'
    )
    op.create_index(op.f('ix_grandpa_yin_wallet_transactions_subject_id'), 'wallet_transactions', ['subject_id'], unique=False, schema='grandpa_yin')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_grandpa_yin_wallet_transactions_subject_id'), table_name='wallet_transactions', schema='grandpa_yin')
    op.drop_table('wallet_transactions', schema='grandpa_yin')
    op.drop_table('subjects', schema='grandpa_yin')
