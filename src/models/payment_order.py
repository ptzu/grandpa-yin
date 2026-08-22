import uuid
from sqlalchemy import Column, Integer, String, DateTime, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from src.models.database import Base


class PaymentOrder(Base):
    """A paid order: money in, points or a gift card out.

    Owned by grandpa_yin in both deploy modes, because the order is a product
    concern — only the resulting points land in the shared ledger.

    The table exists to make settlement *idempotent and auditable*. The payment
    provider retries its callback, so `merchant_trade_no` is unique and
    `credited_at` records whether the order was already settled; both are
    checked in the database, not in memory, because the app runs multiple
    workers. `raw_callback` keeps the provider's own words for when a user
    disputes a charge.

    `kind` decides what settlement means — points straight into the buyer's
    balance, or a gift card someone else will redeem. Everything before that
    moment (placing, paying, verifying, the once-only guarantee) is identical,
    which is why both share this table rather than getting one each.
    """
    __tablename__ = 'payment_orders'
    __table_args__ = (
        UniqueConstraint('merchant_trade_no', name='uq_payment_orders_merchant_trade_no'),
        {'schema': 'grandpa_yin'},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Logical reference to the paying subject (platform: accounts.id /
    # standalone: grandpa_yin.subjects.id). No cross-schema FK — same reasoning
    # as BotSession.
    #
    # Null for gift orders: the buyer is not identified (the checkout page is
    # open web, no LINE login) and the recipient is not known until the card is
    # redeemed. The subject who ends up with the points is on the gift card.
    subject_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    # 'topup' — points go straight to subject_id.
    # 'gift'  — a gift card is issued instead; subject_id is null.
    kind = Column(String(10), nullable=False, default='topup', server_default='topup')

    # ECPay's MerchantTradeNo: max 20 alphanumeric chars, unique per merchant.
    merchant_trade_no = Column(String(20), nullable=False)

    # Points and price are copied from the package at order time, never read
    # back from config when crediting: re-pricing must not change what an
    # already-placed order is worth.
    package_id = Column(String(16), nullable=False)
    points = Column(Integer, nullable=False)
    amount_twd = Column(Integer, nullable=False)

    # pending → paid (credited) | failed | expired
    status = Column(String(20), nullable=False, default='pending')
    payment_type = Column(String(30), nullable=True)   # Credit / CVS / ATM ...

    # Paid and credited are separate moments; a gap between them means the
    # money arrived but the points did not, which is exactly what reconciliation
    # needs to be able to see.
    paid_at = Column(DateTime(timezone=True), nullable=True)
    credited_at = Column(DateTime(timezone=True), nullable=True)

    raw_callback = Column(JSONB, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)

    def __repr__(self):
        return (f"<PaymentOrder(no={self.merchant_trade_no}, status={self.status}, "
                f"points={self.points}, amount={self.amount_twd})>")
