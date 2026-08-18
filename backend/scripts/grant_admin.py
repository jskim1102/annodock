"""Grant or revoke the admin dashboard for an auth-service account.

Usage (run from backend/ with the project .env loaded by Settings):

    .venv/bin/python scripts/grant_admin.py you@example.com
    .venv/bin/python scripts/grant_admin.py you@example.com --revoke

The account itself lives in the read-only auth database; this script only
resolves the email there and writes the grant row into this project's
``admin_users`` table.

Note: the .env DATABASE_URL uses the docker network hostname; when running
this script on the host, override it the way dev.sh does:

    DATABASE_URL="postgresql+asyncpg://postgres:<pw>@localhost:5435/deeplabel" \
        .venv/bin/python scripts/grant_admin.py you@example.com
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.models import AdminUser


async def _resolve_user_id(auth_database_url: str, email: str) -> int:
    engine = create_async_engine(auth_database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT id, email FROM users WHERE lower(email) = lower(:email)"),
                    {"email": email},
                )
            ).first()
    finally:
        await engine.dispose()
    if row is None:
        raise SystemExit(f"auth 계정을 찾을 수 없습니다: {email}")
    return int(row.id)


async def _apply(owner_id: int, revoke: bool) -> str:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            if revoke:
                result = await connection.execute(
                    delete(AdminUser).where(AdminUser.owner_id == owner_id)
                )
                return "회수" if result.rowcount else "회수 (이미 admin 아님 — 변경 없음)"
            result = await connection.execute(
                pg_insert(AdminUser)
                .values(owner_id=owner_id)
                .on_conflict_do_nothing(index_elements=["owner_id"])
            )
            return "부여" if result.rowcount else "부여 (이미 admin — 변경 없음)"
    finally:
        await engine.dispose()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email", help="auth-service 계정 이메일")
    parser.add_argument(
        "--revoke", action="store_true", help="관리자 권한을 회수한다"
    )
    args = parser.parse_args()

    settings = get_settings()
    if not settings.auth_database_url:
        raise SystemExit("AUTH_DATABASE_URL 이 설정돼 있지 않습니다 (.env)")

    owner_id = await _resolve_user_id(settings.auth_database_url, args.email)
    action = await _apply(owner_id, args.revoke)
    print(f"admin {action} 완료: {args.email} (user id {owner_id})")


if __name__ == "__main__":
    asyncio.run(main())
