from sqlalchemy import Column, Text, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from models.database import Base


class BotSession(Base):
    """LINE Bot 對話狀態機（一對一對應 accounts）"""
    __tablename__ = 'bot_sessions'
    __table_args__ = {'schema': 'grandpa_yin'}

    account_id = Column(UUID(as_uuid=True), ForeignKey('accounts.id'), primary_key=True)
    # 複合字串格式 "feature:state"（例：'colorize:waiting'）
    current_state = Column(Text, nullable=False)
    state_metadata = Column(JSONB, nullable=False, default=dict)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"<BotSession(account_id={self.account_id}, current_state={self.current_state})>"

    @property
    def feature(self):
        """從 current_state 拆出 feature 部分"""
        if ':' in self.current_state:
            return self.current_state.split(':', 1)[0]
        return None

    @property
    def state(self):
        """從 current_state 拆出 state 部分"""
        if ':' in self.current_state:
            return self.current_state.split(':', 1)[1]
        return self.current_state
