"""Database metadata and async engine helpers."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


SHORT_LOCK_TIMEOUT_MS = 5_000


class Base(DeclarativeBase):
    """Base class shared by every persisted model."""


def get_database_url() -> str:
    """Return the configured database URL, failing closed when it is absent."""

    database_url = os.getenv("DATABASE_URL") or os.getenv("TEST_DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL or TEST_DATABASE_URL must be set")
    return database_url


def create_engine(database_url: str | None = None) -> AsyncEngine:
    """Create an async SQLAlchemy engine for the requested environment."""

    resolved_url = database_url or get_database_url()
    return create_async_engine(
        resolved_url,
        pool_pre_ping=True,
    )


async def set_local_lock_timeout(session: AsyncSession) -> None:
    """Bound lock waits only inside one deliberately short transaction."""

    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    timeout_ms = int(SHORT_LOCK_TIMEOUT_MS)
    if timeout_ms <= 0:
        raise ValueError("SHORT_LOCK_TIMEOUT_MS must be positive")
    await session.execute(
        text(f"SET LOCAL lock_timeout = '{timeout_ms}ms'")
    )


def is_lock_not_available(error: BaseException) -> bool:
    """Return whether an exception chain contains PostgreSQL SQLSTATE 55P03."""

    pending: list[object] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if (
            getattr(current, "sqlstate", None) == "55P03"
            or getattr(current, "pgcode", None) == "55P03"
        ):
            return True
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        for attribute in ("orig", "__cause__", "__context__"):
            nested = getattr(current, attribute, None)
            if nested is not None:
                pending.append(nested)
    return False


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Create sessions that keep model state usable after commits."""

    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield one request-scoped transaction-capable async session."""

    async with request.app.state.session_factory() as session:
        yield session
