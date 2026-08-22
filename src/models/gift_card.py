import uuid
from sqlalchemy import Column, Integer, String, DateTime, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from src.models.database import Base


class GiftCard(Base):
    """A prepaid points card: someone pays, someone else redeems.

    Exists because the buyer and the user are usually different people — an
    adult child pays, a parent redeems. The card is the object that survives
    between those two moments, so it is bearer-style: whoever types the code
    gets the points, and no recipient is named at purchase time.

    Cards are only ever created by a verified payment callback, never by the
    checkout request, for the same reason points are: the browser proves
    nothing. The row is written inside the callback's transaction and guarded
    by `PaymentOrder.credited_at`, so a retried callback cannot mint a second
    card for the same order — hence the unique order_id.

    Redemption is idempotent the same way crediting is: `code` is unique, the
    row is locked before redeeming, and `redeemed_at` records that it happened.
    Two workers racing on the same code means one wins and one sees it taken.

    No expiry column, deliberately. Taiwan's rules for gift certificates forbid
    a stated expiry date, and whether a points card counts is grey enough that
    the safe reading is also the kind one.
    """
    __tablename__ = 'gift_cards'
    __table_args__ = (
        UniqueConstraint('code', name='uq_gift_cards_code'),
        UniqueConstraint('order_id', name='uq_gift_cards_order_id'),
        {'schema': 'grandpa_yin'},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Normalised form: uppercase, no separators. What the buyer sees is
    # hyphenated for readability; what is stored and compared is this.
    code = Column(String(16), nullable=False)

    # The paid order that produced this card. Unique: one order, one card.
    order_id = Column(UUID(as_uuid=True), nullable=False)

    # Snapshotted from the order, for the same reason the order snapshots the
    # package: re-pricing must not change what an issued card is worth.
    points = Column(Integer, nullable=False)

    # active -> redeemed. There is no 'pending': a card only exists once paid.
    status = Column(String(20), nullable=False, default='active')

    # Logical reference to the subject who redeemed it (no cross-schema FK,
    # same reasoning as PaymentOrder.subject_id).
    redeemed_by_subject_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    redeemed_at = Column(DateTime(timezone=True), nullable=True)

    # When the card was handed to LINE's share picker. Advisory: lets the share
    # page warn on re-open, but the card can still be re-sent before redemption.
    sent_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self):
        return (f"<GiftCard(code={self.code}, status={self.status}, "
                f"points={self.points})>")
