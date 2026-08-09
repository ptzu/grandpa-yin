import uuid
from sqlalchemy import Column, Integer, String, Text, DateTime, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from models.database import Base


class UsageLog(Base):
    """銀爺爺 AI 功能使用業務帳"""
    __tablename__ = 'usage_logs'
    __table_args__ = {'schema': 'grandpa_yin'}

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Logical reference to the owning subject; no cross-schema FK (see BotSession).
    account_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    feature_type = Column(Text, nullable=False)  # 'colorize' | 'edit' | ...
    points_deducted = Column(Integer, nullable=False, default=0)
    status = Column(String(20), nullable=False)  # 'processing' | 'completed' | 'failed'
    # "metadata" 是 SQLAlchemy 保留字，Python 屬性用 log_metadata；DB 欄位仍是 metadata
    log_metadata = Column('metadata', JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self):
        return f"<UsageLog(account_id={self.account_id}, feature={self.feature_type}, status={self.status})>"
