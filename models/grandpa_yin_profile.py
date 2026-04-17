from sqlalchemy import Column, String, Text, Boolean, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from models.database import Base


class GrandpaYinProfile(Base):
    """銀爺爺長輩專屬設定（一對一擴充 accounts）"""
    __tablename__ = 'user_profiles'
    __table_args__ = {'schema': 'grandpa_yin'}

    account_id = Column(UUID(as_uuid=True), ForeignKey('accounts.id'), primary_key=True)
    preferred_nickname = Column(Text, nullable=True)
    is_tutorial_completed = Column(Boolean, nullable=False, default=False)
    display_name = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default='normal')  # normal | vip | suspended | banned
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    def __repr__(self):
        return f"<GrandpaYinProfile(account_id={self.account_id}, display_name={self.display_name}, status={self.status})>"

    def to_dict(self):
        return {
            'account_id': str(self.account_id),
            'preferred_nickname': self.preferred_nickname,
            'is_tutorial_completed': self.is_tutorial_completed,
            'display_name': self.display_name,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
