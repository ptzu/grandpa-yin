import uuid
from sqlalchemy import Column, Integer, Boolean, DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from src.models.database import Base


class Account(Base):
    """核心帳號表（公共層，共用點數錢包）"""
    __tablename__ = 'accounts'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    auth_user_id = Column(UUID(as_uuid=True), nullable=True, unique=True)
    points_balance = Column(Integer, nullable=False, default=0)
    is_admin = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self):
        return f"<Account(id={self.id}, auth_user_id={self.auth_user_id}, points={self.points_balance})>"
