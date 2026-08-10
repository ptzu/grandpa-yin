import uuid
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from src.models.database import Base


class Transaction(Base):
    """點數交易財務帳（跨產品共用）"""
    __tablename__ = 'transactions'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey('accounts.id'), nullable=False, index=True)
    amount = Column(Integer, nullable=False)  # 正為儲值 / 負為扣款
    service = Column(String(50), nullable=False)  # 'silver-grandpa' | 'altide-web'
    balance_after = Column(Integer, nullable=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self):
        return f"<Transaction(account_id={self.account_id}, amount={self.amount}, service={self.service})>"
