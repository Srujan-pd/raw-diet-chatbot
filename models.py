"""
models.py — SQLAlchemy models for Raw Diet chatbot persistence.
"""
from datetime import datetime
from sqlalchemy import Column, String, Text, DateTime, Integer
from database import Base


class Chat(Base):
    __tablename__ = "chats"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    session_id   = Column(String(128), nullable=False, index=True)
    firebase_uid = Column(String(128), nullable=True, index=True)  # always the Firebase UID
    question     = Column(Text, nullable=False)
    answer       = Column(Text, nullable=False)
    created_at   = Column(DateTime, default=datetime.utcnow)
