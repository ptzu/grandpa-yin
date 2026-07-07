import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from contextlib import contextmanager

from app_logger import get_logger

logger = get_logger("database")

# 建立 Base 模型類別
Base = declarative_base()

# 全域變數
_engine = None
_SessionFactory = None


def init_database():
    """初始化資料庫連線"""
    global _engine, _SessionFactory
    
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL 環境變數未設定")
    
    logger.info("連接資料庫...")
    
    # 建立 engine，優化連線設定
    _engine = create_engine(
        database_url,
        pool_size=5,  # 常駐連線數
        max_overflow=10,  # 尖峰時的額外連線（webhook threads + 背景圖片處理共用）
        pool_pre_ping=True,  # 確保連線有效
        pool_recycle=3600,  # 連線回收時間（1小時）
        connect_args={
            "connect_timeout": 10,  # 連線超時時間
            "application_name": "linebot_member_system"  # 應用程式名稱
        },
        echo=False  # 設為 True 可以看到 SQL 語句（開發用）
    )
    
    # 建立 Session factory
    _SessionFactory = sessionmaker(bind=_engine)
    
    logger.info("資料庫連線初始化完成")
    
    return _engine


@contextmanager
def get_session():
    """取得資料庫 session（使用 context manager）"""
    if _SessionFactory is None:
        raise RuntimeError("資料庫尚未初始化，請先呼叫 init_database()")
    
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

