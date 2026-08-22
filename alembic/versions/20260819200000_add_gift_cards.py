"""add gift cards

Adds grandpa_yin.gift_cards and the payment_orders columns that let one order
table carry both kinds of purchase:

  * payment_orders.kind — 'topup' (points to the buyer) or 'gift' (a card).
  * payment_orders.subject_id becomes nullable — a gift order has no subject
    at payment time; the recipient is whoever redeems the card.

Existing rows are all top-ups, so kind gets a server default of 'topup' and
backfills without a data migration.

The unique constraints on gift_cards are load-bearing: `code` makes redeeming
exactly-once across workers, and `order_id` stops a retried payment callback
from minting a second card for the same order.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-08-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c2d3e4f5a6b7'
down_revision: Union[str, Sequence[str], None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('payment_orders',
                  sa.Column('kind', sa.String(length=10), nullable=False,
                            server_default='topup'),
                  schema='grandpa_yin')
    op.alter_column('payment_orders', 'subject_id',
                    existing_type=sa.UUID(), nullable=True,
                    schema='grandpa_yin')

    op.create_table('gift_cards',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('code', sa.String(length=16), nullable=False),
        sa.Column('order_id', sa.UUID(), nullable=False),
        sa.Column('points', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('redeemed_by_subject_id', sa.UUID(), nullable=True),
        sa.Column('redeemed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code', name='uq_gift_cards_code'),
        sa.UniqueConstraint('order_id', name='uq_gift_cards_order_id'),
        schema='grandpa_yin'
    )
    op.create_index(op.f('ix_grandpa_yin_gift_cards_redeemed_by_subject_id'),
                    'gift_cards', ['redeemed_by_subject_id'], unique=False,
                    schema='grandpa_yin')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_grandpa_yin_gift_cards_redeemed_by_subject_id'),
                  table_name='gift_cards', schema='grandpa_yin')
    op.drop_table('gift_cards', schema='grandpa_yin')

    # Gift orders have no subject_id, so they must go before the column can be
    # NOT NULL again. They are unredeemable without the cards anyway.
    op.execute("DELETE FROM grandpa_yin.payment_orders WHERE kind = 'gift'")
    op.alter_column('payment_orders', 'subject_id',
                    existing_type=sa.UUID(), nullable=False,
                    schema='grandpa_yin')
    op.drop_column('payment_orders', 'kind', schema='grandpa_yin')
