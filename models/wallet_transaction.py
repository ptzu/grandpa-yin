import uuid
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from models.database import Base


class WalletTransaction(Base):
    """Standalone point ledger, owned by grandpa_yin.

    Mirror of public.transactions for DEPLOY_MODE=standalone. Field names match
    Transaction (amount / service / balance_after / description / created_at) so
    the history serialization in member_service can be shared across modes.

    The FK points at grandpa_yin.subjects (same schema, self-owned) — no
    cross-schema dependency on Altide.
    """
    __tablename__ = 'wallet_transactions'
    __table_args__ = {'schema': 'grandpa_yin'}

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subject_id = Column(
        UUID(as_uuid=True),
        ForeignKey('grandpa_yin.subjects.id'),
        nullable=False,
        index=True,
    )
    amount = Column(Integer, nullable=False)  # positive = credit / negative = debit
    service = Column(String(50), nullable=False)
    balance_after = Column(Integer, nullable=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self):
        return f"<WalletTransaction(subject_id={self.subject_id}, amount={self.amount}, service={self.service})>"
