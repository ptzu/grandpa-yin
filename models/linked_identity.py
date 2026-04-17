import uuid
from sqlalchemy import Column, String, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from models.database import Base


class LinkedIdentity(Base):
    """第三方身分綁定（provider='line' 對應 LINE UID）"""
    __tablename__ = 'linked_identities'
    __table_args__ = (
        UniqueConstraint('provider', 'provider_uid', name='linked_identities_provider_provider_uid_key'),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey('accounts.id'), nullable=False, index=True)
    provider = Column(String(50), nullable=False)
    provider_uid = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self):
        return f"<LinkedIdentity(account_id={self.account_id}, provider={self.provider}, uid={self.provider_uid})>"
