"""Read-only admin dashboard queries (F26).

Account identity (email, signup time) lives in the auth-service database,
which this project treats as read-only. This module therefore keeps two
concerns apart: admin membership is a row in this project's ``admin_users``
table, while directory rows are fetched from the auth database over a
dedicated engine that only ever runs SELECTs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.models import AdminUser


@dataclass(frozen=True, slots=True)
class AuthDirectoryUser:
    """One auth-service account as shown on the dashboard."""

    id: int
    email: str | None
    username: str | None
    created_at: datetime
    login_methods: tuple[str, ...]


def _login_methods(has_password: bool, providers: list[str] | None) -> tuple[str, ...]:
    """Collapse identity rows into display labels; 'local' means password."""

    methods: list[str] = []
    if has_password or (providers and "local" in providers):
        methods.append("이메일")
    for provider in sorted(set(providers or [])):
        if provider != "local":
            methods.append(provider)
    return tuple(methods)


_auth_engine: AsyncEngine | None = None
_auth_engine_url: str | None = None


async def is_admin(session: AsyncSession, owner_id: int) -> bool:
    """Return whether the requesting user holds an admin grant."""

    grant = await session.scalar(
        select(AdminUser.owner_id).where(AdminUser.owner_id == owner_id)
    )
    return grant is not None


def _engine_for(auth_database_url: str) -> AsyncEngine:
    global _auth_engine, _auth_engine_url
    if _auth_engine is None or _auth_engine_url != auth_database_url:
        stale = _auth_engine
        _auth_engine = create_async_engine(
            auth_database_url, pool_pre_ping=True, pool_size=2
        )
        _auth_engine_url = auth_database_url
        if stale is not None:
            # Old pool is disposed asynchronously; nothing awaits it because
            # the swap happens mid-request — dispose_auth_engine() at app
            # shutdown covers the final instance.
            import asyncio

            asyncio.get_running_loop().create_task(stale.dispose())
    return _auth_engine


async def dispose_auth_engine() -> None:
    """Release the lazily-created auth-directory pool (app lifespan exit)."""

    global _auth_engine, _auth_engine_url
    if _auth_engine is not None:
        await _auth_engine.dispose()
        _auth_engine = None
        _auth_engine_url = None


async def load_auth_directory(
    auth_database_url: str | None,
) -> list[AuthDirectoryUser]:
    """Fetch every account from the auth database, newest first.

    Raises RuntimeError when the directory is not configured — the router
    turns that into a 503 so a wiring gap never reads as "no users".
    """

    if not auth_database_url:
        raise RuntimeError("AUTH_DATABASE_URL is not configured")
    engine = _engine_for(auth_database_url)
    try:
        return await _query_directory(engine)
    except (SQLAlchemyError, OSError) as error:
        # A real outage (host down, bad credentials) must surface as the
        # documented 503, not an opaque 500 via the DBAPIError handler.
        raise RuntimeError(f"auth directory unavailable: {error}") from error


async def _query_directory(engine: AsyncEngine) -> list[AuthDirectoryUser]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT u.id, u.email, u.username, u.created_at,"
                " (u.password_hash IS NOT NULL) AS has_password,"
                " array_remove(array_agg(DISTINCT ai.provider), NULL)"
                " AS providers"
                " FROM users u"
                " LEFT JOIN auth_identities ai ON ai.user_id = u.id"
                " GROUP BY u.id"
                " ORDER BY u.created_at DESC, u.id DESC"
            )
        )
        return [
            AuthDirectoryUser(
                id=row.id,
                email=row.email,
                username=row.username,
                created_at=row.created_at,
                login_methods=_login_methods(
                    bool(row.has_password), list(row.providers or [])
                ),
            )
            for row in rows
        ]
