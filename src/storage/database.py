"""
SimpleClaw v2.0 - Database Connection
======================================
Async-ready PostgreSQL connection with schema management.
"""

from __future__ import annotations

import structlog
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import create_engine

from src.config.settings import get_settings
from src.storage.models import Base

logger = structlog.get_logger()

_async_engine = None
_async_session_factory = None
_sync_engine = None
_sync_session_factory = None


def _get_async_url(url: str) -> str:
    """Convert sync URL to async (psycopg -> psycopg async)."""
    return url.replace("postgresql+psycopg://", "postgresql+psycopg://")


async def init_database() -> None:
    """Initialize database: create schemas, tables, and indexes."""
    global _async_engine, _async_session_factory

    settings = get_settings()
    _async_engine = create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        echo=settings.debug,
    )
    _async_session_factory = async_sessionmaker(
        _async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with _async_engine.begin() as conn:
        # Create schemas
        await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {settings.system_schema}"))
        await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {settings.agent_schema}"))
        # Create all tables
        await conn.run_sync(Base.metadata.create_all)

    logger.info("database.initialized", url=settings.database_url.split("@")[-1])


def init_sync_database() -> None:
    """Initialize sync engine for tools that need sync access."""
    global _sync_engine, _sync_session_factory

    settings = get_settings()
    _sync_engine = create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        echo=settings.debug,
    )
    _sync_session_factory = sessionmaker(_sync_engine, expire_on_commit=False)


async def get_session() -> AsyncSession:
    """Get an async database session."""
    if _async_session_factory is None:
        await init_database()
    return _async_session_factory()


def get_sync_session() -> Session:
    """Get a sync database session."""
    if _sync_session_factory is None:
        init_sync_database()
    return _sync_session_factory()


async def close_database() -> None:
    """Close database connections."""
    global _async_engine
    if _async_engine:
        await _async_engine.dispose()
        _async_engine = None
    logger.info("database.closed")
