"""add payment orders

Adds grandpa_yin.payment_orders — top-up orders placed against the payment
provider (ECPay). Product-owned in both deploy modes; only the resulting points
touch the shared ledger.

The unique constraint on merchant_trade_no is load-bearing: the provider retries
its callback, and crediting must happen exactly once across all workers.

Revision ID: b1c2d3e4f5a6
Revises: a7b8c9d0e1f2
Create Date: 2026-08-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, Sequence[str], None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('payment_orders',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('subject_id', sa.UUID(), nullable=False),
        sa.Column('merchant_trade_no', sa.String(length=20), nullable=False),
        sa.Column('package_id', sa.String(length=16), nullable=False),
        sa.Column('points', sa.Integer(), nullable=False),
        sa.Column('amount_twd', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('payment_type', sa.String(length=30), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('credited_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('raw_callback', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('merchant_trade_no', name='uq_payment_orders_merchant_trade_no'),
        schema='grandpa_yin'
    )
    op.create_index(op.f('ix_grandpa_yin_payment_orders_subject_id'), 'payment_orders',
                    ['subject_id'], unique=False, schema='grandpa_yin')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_grandpa_yin_payment_orders_subject_id'),
                  table_name='payment_orders', schema='grandpa_yin')
    op.drop_table('payment_orders', schema='grandpa_yin')
