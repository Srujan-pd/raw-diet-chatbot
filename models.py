import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, JSON, String, Text, func
from sqlalchemy.orm import relationship

from database import Base


# ── Enum: matches Prisma's MessageRole ────────────────────────────────────────

class MessageRole(str, enum.Enum):
    USER      = "USER"
    ASSISTANT = "ASSISTANT"
    SYSTEM    = "SYSTEM"


# ── ChatSession ───────────────────────────────────────────────────────────────

class ChatSession(Base):
    """
    Mirrors Prisma model ChatSession exactly.
    userId references User.id (Prisma UUID) — NOT the Firebase UID.
    """
    __tablename__ = "ChatSession"

    id        = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    userId    = Column(String, nullable=False, index=True)   # → User.id (Prisma UUID)
    title     = Column(String(255), nullable=True)
    isActive  = Column(Boolean, nullable=False, default=True)
    createdAt = Column(DateTime(timezone=True), server_default=func.now(),
                       nullable=False, default=datetime.now)
    updatedAt = Column(DateTime(timezone=True), server_default=func.now(),
                       onupdate=func.now(), nullable=False, default=datetime.now)

    messages  = relationship("ChatMessage", back_populates="session",
                             cascade="all, delete-orphan",
                             order_by="ChatMessage.createdAt")


# ── ChatMessage ───────────────────────────────────────────────────────────────

class ChatMessage(Base):
    """
    Mirrors Prisma model ChatMessage exactly.
    role = USER      → the human's question
    role = ASSISTANT → the AI's reply
    role = SYSTEM    → system injections (optional)
    Note: 'metadata' is reserved by SQLAlchemy, so Python attr is 'meta'
          but the DB column is still named 'metadata' to match Prisma.
    """
    __tablename__ = "ChatMessage"

    id        = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    sessionId = Column(String,
                       ForeignKey("ChatSession.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    role      = Column(Enum(MessageRole, name="MessageRole"), nullable=False)
    content   = Column(Text, nullable=False)
    meta      = Column("metadata", JSON, nullable=True)  # DB col = "metadata"
    createdAt = Column(DateTime(timezone=True), server_default=func.now(),
                       nullable=False, default=datetime.now)

    session   = relationship("ChatSession", back_populates="messages")

