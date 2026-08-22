"""add gift_cards.sent_at

Records when a card was handed to LINE's share picker, so re-opening the share
page for an already-sent card can warn the buyer instead of silently letting
them send the same one-time card to a second person.

`sent_at` is advisory only — the card can still be re-sent (to fix a wrong
recipient before anyone redeems it), so redemption's exactly-once guarantee is
untouched.

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-08-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd3e4f5a6b7c8'
down_revision: Union[str, Sequence[str], None] = 'c2d3e4f5a6b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('gift_cards',
                  sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
                  schema='grandpa_yin')


def downgrade() -> None:
    op.drop_column('gift_cards', 'sent_at', schema='grandpa_yin')
