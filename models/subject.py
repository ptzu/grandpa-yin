import uuid
from sqlalchemy import Column, Integer, Boolean, String, DateTime, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from models.database import Base


class Subject(Base):
    """Standalone identity + wallet, owned by grandpa_yin.

    Used when DEPLOY_MODE=standalone: it replaces Altide's public.accounts +
    linked_identities so the product can run without the shared platform. In
    platform mode this table exists but stays empty/unused.

    A subject collapses "who" (provider + provider_uid) and "wallet"
    (points_balance) into one grandpa_yin-owned row, mirroring the subset of
    Account/LinkedIdentity that this product actually needs.
    """
    __tablename__ = 'subjects'
    __table_args__ = (
        UniqueConstraint('provider', 'provider_uid', name='uq_subjects_provider_uid'),
        {'schema': 'grandpa_yin'},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider = Column(String(50), nullable=False)        # 'line'
    provider_uid = Column(String(255), nullable=False)   # LINE UID
    points_balance = Column(Integer, nullable=False, default=0)
    is_admin = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self):
        return f"<Subject(id={self.id}, provider_uid={self.provider_uid}, points={self.points_balance})>"
