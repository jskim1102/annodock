"""Database metadata and async engine helpers."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


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

    return create_async_engine(database_url or get_database_url(), pool_pre_ping=True)


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Create sessions that keep model state usable after commits."""

    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield one request-scoped transaction-capable async session."""

    async with request.app.state.session_factory() as session:
        yield session
