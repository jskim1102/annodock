"""Read-only admin dashboard API (F26).

Non-admin callers get the same 404 a nonexistent route produces (identical
"Not Found" body, and a catch-all absorbs unknown paths/methods so no 405
betrays the router) — the dashboard is not observable from the outside.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import CurrentUser, get_current_user
from app.models import UserStorage
from app.services.admin import AuthDirectoryUser, is_admin, load_auth_directory

router = APIRouter(prefix="/api/admin", tags=["admin"])

async def require_admin(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CurrentUser:
    if not await is_admin(session, current_user.id):
        raise HTTPException(status_code=404, detail="Not Found")
    return current_user


AdminDep = Annotated[CurrentUser, Depends(require_admin)]


class AdminUserRow(BaseModel):
    id: int
    email: str | None
    username: str | None
    created_at: str
    login_methods: list[str]
    bytes_used: int


class AdminOverview(BaseModel):
    user_count: int
    storage_total_bytes: int


class AdminUsersResponse(BaseModel):
    users: list[AdminUserRow]


async def _usage_by_owner(session: AsyncSession) -> dict[int, int]:
    rows = await session.execute(
        select(UserStorage.owner_id, UserStorage.bytes_used)
    )
    return {owner_id: bytes_used for owner_id, bytes_used in rows}


def _row(user: AuthDirectoryUser, usage: dict[int, int]) -> AdminUserRow:
    return AdminUserRow(
        id=user.id,
        email=user.email,
        username=user.username,
        created_at=user.created_at.isoformat(),
        login_methods=list(user.login_methods),
        bytes_used=usage.get(user.id, 0),
    )


async def _directory(request: Request) -> list[AuthDirectoryUser]:
    settings = request.app.state.settings
    try:
        return await load_auth_directory(settings.auth_database_url)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.get("/overview", response_model=AdminOverview)
async def admin_overview(
    request: Request,
    _admin: AdminDep,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AdminOverview:
    directory = await _directory(request)
    usage = await _usage_by_owner(session)
    # Sum only accounts present in the directory: an auth-side deletion can
    # leave an orphaned user_storage row, and the headline must equal the
    # per-user column it sits above.
    total = sum(usage.get(user.id, 0) for user in directory)
    return AdminOverview(
        user_count=len(directory),
        storage_total_bytes=total,
    )


@router.get("/users", response_model=AdminUsersResponse)
async def admin_users(
    request: Request,
    _admin: AdminDep,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AdminUsersResponse:
    directory = await _directory(request)
    usage = await _usage_by_owner(session)
    rows = [_row(user, usage) for user in directory]
    rows.sort(key=lambda item: (-item.bytes_used, item.id))
    return AdminUsersResponse(users=rows)


@router.api_route(
    "/{rest:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def admin_catch_all(rest: str, _admin: AdminDep) -> None:
    # Unknown admin paths and mismatched methods behave exactly like any
    # nonexistent route — for admins and non-admins alike.
    raise HTTPException(status_code=404, detail="Not Found")
