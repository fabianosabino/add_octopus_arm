"""
SimpleClaw v2.0 - Database Models
==================================
Multi-tenant models for users, tasks, conversations, vault, and cost tracking.
Uses SQLAlchemy 2.0 declarative style.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ─── USERS ──────────────────────────────────────────────────

class User(Base):
    """Telegram users (multi-tenant)."""
    __tablename__ = "users"
    __table_args__ = {"schema": "system"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(255))
    display_name: Mapped[Optional[str]] = mapped_column(String(255))
    language: Mapped[str] = mapped_column(String(10), default="pt-BR")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    preferences: Mapped[Optional[dict]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    # Relationships
    conversations: Mapped[list["Conversation"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    tasks: Mapped[list["Task"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    schedules: Mapped[list["Schedule"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    cost_logs: Mapped[list["CostLog"]] = relationship(back_populates="user", cascade="all, delete-orphan")


# ─── CONVERSATIONS ──────────────────────────────────────────

class Conversation(Base):
    """Chat messages - full history with compression support."""
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_user_created", "user_id", "created_at"),
        {"schema": "system"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("system.users.id", ondelete="CASCADE"), nullable=False)
    session_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user, assistant, system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(20), default="text")  # text, image, audio, file
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, default=dict)
    token_count: Mapped[Optional[int]] = mapped_column(Integer)
    is_compressed: Mapped[bool] = mapped_column(Boolean, default=False)
    compressed_content: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Relationships
    user: Mapped["User"] = relationship(back_populates="conversations")


# ─── TASKS ──────────────────────────────────────────────────

class TaskStatus(str):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class Task(Base):
    """Task queue with full lifecycle tracking."""
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_status_priority", "status", "priority"),
        {"schema": "system"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("system.users.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    specification: Mapped[Optional[dict]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default=TaskStatus.PENDING, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=5)  # 1=highest, 10=lowest
    assigned_agents: Mapped[Optional[list]] = mapped_column(JSONB, default=list)
    result: Mapped[Optional[dict]] = mapped_column(JSONB)
    error_log: Mapped[Optional[str]] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    git_commit_hash: Mapped[Optional[str]] = mapped_column(String(40))
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    # Relationships
    user: Mapped["User"] = relationship(back_populates="tasks")
    heartbeats: Mapped[list["Heartbeat"]] = relationship(back_populates="task", cascade="all, delete-orphan")


# ─── HEARTBEAT ──────────────────────────────────────────────

class Heartbeat(Base):
    """Task health monitoring."""
    __tablename__ = "heartbeats"
    __table_args__ = {"schema": "system"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("system.tasks.id", ondelete="CASCADE"), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="alive")
    step_description: Mapped[Optional[str]] = mapped_column(Text)
    ram_usage_mb: Mapped[Optional[float]] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Relationships
    task: Mapped["Task"] = relationship(back_populates="heartbeats")


# ─── SCHEDULES ──────────────────────────────────────────────

class Schedule(Base):
    """User-defined and system-defined scheduled tasks."""
    __tablename__ = "schedules"
    __table_args__ = {"schema": "system"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("system.users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    cron_expression: Mapped[str] = mapped_column(String(100), nullable=False)
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)  # message, task, query, report
    action_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)  # system vs user-created
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Relationships
    user: Mapped["User"] = relationship(back_populates="schedules")


# ─── VAULT ──────────────────────────────────────────────────

class VaultEntry(Base):
    """Encrypted credential storage with rotation tracking."""
    __tablename__ = "vault"
    __table_args__ = (
        UniqueConstraint("user_id", "key_name", name="uq_vault_user_key"),
        {"schema": "system"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("system.users.id", ondelete="CASCADE"))
    key_name: Mapped[str] = mapped_column(String(255), nullable=False)
    encrypted_value: Mapped[str] = mapped_column(Text, nullable=False)  # Fernet-encrypted
    provider: Mapped[Optional[str]] = mapped_column(String(100))
    rotated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ─── COST TRACKING ─────────────────────────────────────────

class CostLog(Base):
    """Token usage and cost tracking per request."""
    __tablename__ = "cost_logs"
    __table_args__ = (
        Index("ix_cost_logs_user_created", "user_id", "created_at"),
        {"schema": "system"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("system.users.id", ondelete="CASCADE"), nullable=False)
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("system.tasks.id", ondelete="SET NULL"))
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model_id: Mapped[str] = mapped_column(String(100), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    request_type: Mapped[str] = mapped_column(String(50), default="chat")  # chat, task, schedule
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Relationships
    user: Mapped["User"] = relationship(back_populates="cost_logs")


# ─── HISTORY DIGEST ─────────────────────────────────────────

class HistoryDigest(Base):
    """Compressed conversation digests for long-term storage."""
    __tablename__ = "history_digests"
    __table_args__ = {"schema": "system"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("system.users.id", ondelete="CASCADE"), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    key_topics: Mapped[list] = mapped_column(JSONB, default=list)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    user_approved: Mapped[Optional[bool]] = mapped_column(Boolean)  # None=pending, True=keep, False=deleted
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
