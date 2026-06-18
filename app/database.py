"""
Async database connections for nexus-search.
  - PostgreSQL via SQLAlchemy 2.0 async (projects table)
  - Redis via redis-py async (cache-aside)

MongoDB is out of scope for nexus-search (PostgreSQL only per task spec).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import Settings

# ── ORM Base ─────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ── Module-level singletons (set by configure_engine) ────────────────────────

_engine = None
_session_factory: async_sessionmaker | None = None
_redis_client: aioredis.Redis | None = None


def configure_engine(settings: Settings) -> None:
    """Called once at startup with the injected Settings."""
    global _engine, _session_factory

    # SQLite (tests) doesn't support pool_size / max_overflow
    is_sqlite = settings.database_url.startswith("sqlite")
    kwargs = {} if is_sqlite else {"pool_size": 10, "max_overflow": 20}

    _engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=not is_sqlite,
        echo=settings.debug,
        **kwargs,
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


def configure_redis(settings: Settings) -> None:
    """Create Redis client — may be a fakeredis in tests."""
    global _redis_client
    _redis_client = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        max_connections=20,
    )


async def create_tables() -> None:
    """DDL bootstrap — creates tables that don't exist. Tests + dev only."""
    from app.models.project import ProjectModel  # noqa: F401  ensure model loaded
    assert _engine is not None
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_connections() -> None:
    """Graceful shutdown — dispose all connection pools."""
    if _engine:
        await _engine.dispose()
    if _redis_client:
        await _redis_client.aclose()


@asynccontextmanager
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Context-manager dependency: yields a per-request async DB session."""
    assert _session_factory is not None, "Database not configured"
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_redis() -> aioredis.Redis:
    """Returns shared Redis client."""
    assert _redis_client is not None, "Redis not configured"
    return _redis_client
