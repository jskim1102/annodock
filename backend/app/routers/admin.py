"""Admin dashboard API (F26) plus admin-only user deletion.

Non-admin callers get the same 404 a nonexistent route produces (identical
"Not Found" body, and a catch-all absorbs unknown paths/methods so no 405
betrays the router) — the dashboard is not observable from the outside.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session, set_local_lock_timeout
from app.deps import CurrentUser, get_current_user
from app.models import (
    AdminUser,
    Dataset,
    Membership,
    Organization,
    Project,
    TrainingRun,
    UploadSession,
    UserStorage,
)
from app.services.admin import (
    AuthDirectoryUser,
    delete_auth_account,
    is_admin,
    load_auth_directory,
)
from app.services.cleanup import contained_training_run_path
from app.services.quota import (
    apply_dataset_storage_release,
    plan_dataset_storage_release,
    set_quota_limit,
)
from app.services.storage import (
    finalize_staged_deletions,
    restore_staged_deletions,
    stage_deletions_async,
)
from app.services.uploads import upload_directory

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
    limit_bytes: int


class AdminOverview(BaseModel):
    user_count: int
    storage_total_bytes: int


class AdminUsersResponse(BaseModel):
    users: list[AdminUserRow]


class AdminQuotaUpdate(BaseModel):
    limit_bytes: int = Field(gt=0, le=2**63 - 1, strict=True)


class AdminQuotaUpdateResponse(BaseModel):
    user_id: int
    limit_bytes: int


async def _storage_by_owner(
    session: AsyncSession,
) -> dict[int, tuple[int, int | None]]:
    rows = await session.execute(
        select(
            UserStorage.owner_id,
            UserStorage.bytes_used,
            UserStorage.quota_limit_bytes,
        )
    )
    return {
        owner_id: (bytes_used, quota_limit_bytes)
        for owner_id, bytes_used, quota_limit_bytes in rows
    }


def _row(
    user: AuthDirectoryUser,
    storage: dict[int, tuple[int, int | None]],
    default_limit_bytes: int,
) -> AdminUserRow:
    bytes_used, quota_override = storage.get(user.id, (0, None))
    return AdminUserRow(
        id=user.id,
        email=user.email,
        username=user.username,
        created_at=user.created_at.isoformat(),
        login_methods=list(user.login_methods),
        bytes_used=bytes_used,
        limit_bytes=(
            quota_override
            if quota_override is not None
            else default_limit_bytes
        ),
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
    storage = await _storage_by_owner(session)
    # Sum only accounts present in the directory: an auth-side deletion can
    # leave an orphaned user_storage row, and the headline must equal the
    # per-user column it sits above.
    total = sum(storage.get(user.id, (0, None))[0] for user in directory)
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
    storage = await _storage_by_owner(session)
    rows = [
        _row(
            user,
            storage,
            request.app.state.settings.quota_bytes_per_user,
        )
        for user in directory
    ]
    rows.sort(key=lambda item: (-item.bytes_used, item.id))
    return AdminUsersResponse(users=rows)


@router.patch(
    "/users/{user_id}/quota",
    response_model=AdminQuotaUpdateResponse,
)
async def admin_update_user_quota(
    user_id: int,
    body: AdminQuotaUpdate,
    request: Request,
    _admin: AdminDep,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AdminQuotaUpdateResponse:
    directory = await _directory(request)
    if not any(user.id == user_id for user in directory):
        raise HTTPException(status_code=404, detail="Not Found")

    await set_local_lock_timeout(session)
    limit_bytes = await set_quota_limit(
        session,
        user_id,
        body.limit_bytes,
    )
    await session.commit()
    return AdminQuotaUpdateResponse(
        user_id=user_id,
        limit_bytes=limit_bytes,
    )


@router.delete("/users/{user_id}", status_code=204)
async def admin_delete_user(
    user_id: int,
    request: Request,
    admin: AdminDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    confirm: Annotated[bool, Query()] = False,
) -> Response:
    if user_id == admin.id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "admin-self-delete",
                "message": "자기 자신의 계정은 삭제할 수 없습니다.",
            },
        )
    directory = await _directory(request)
    target = next((user for user in directory if user.id == user_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Not Found")
    if await is_admin(session, user_id):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "admin-target-admin",
                "message": (
                    "관리자 계정은 삭제할 수 없습니다. "
                    "관리자 권한을 회수한 뒤 다시 시도하세요."
                ),
            },
        )

    projects = list(
        (
            await session.scalars(
                select(Project)
                .where(Project.owner_id == user_id)
                .order_by(Project.id)
                .with_for_update()
            )
        ).all()
    )
    datasets = list(
        (
            await session.scalars(
                select(Dataset)
                .where(Dataset.owner_id == user_id)
                .order_by(Dataset.id)
                .with_for_update()
            )
        ).all()
    )
    runs = list(
        (
            await session.scalars(
                select(TrainingRun)
                .where(TrainingRun.owner_id == user_id)
                .order_by(TrainingRun.id)
                .with_for_update()
            )
        ).all()
    )
    active_runs = [run for run in runs if run.state in {"running", "canceling"}]
    if active_runs:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "admin-user-active-runs",
                "message": (
                    "진행 중이거나 취소 중인 학습이 있어 사용자를 "
                    "삭제할 수 없습니다. 학습이 끝난 뒤 다시 시도하세요."
                ),
            },
        )

    bytes_used = (
        await session.scalar(
            select(UserStorage.bytes_used).where(
                UserStorage.owner_id == user_id
            )
        )
        or 0
    )
    visible_datasets = [
        dataset for dataset in datasets if not dataset.is_placeholder
    ]
    if not confirm:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "admin-user-delete-confirmation-required",
                "requires_confirmation": True,
                "warning": "이 작업은 되돌릴 수 없습니다.",
                "email": target.email,
                "username": target.username,
                "project_count": len(projects),
                "dataset_count": len(visible_datasets),
                "bytes_used": bytes_used,
            },
        )

    dataset_ids = [dataset.id for dataset in datasets]
    if dataset_ids:
        await set_local_lock_timeout(session)
        upload_ids = list(
            (
                await session.scalars(
                    select(UploadSession.id)
                    .where(UploadSession.dataset_id.in_(dataset_ids))
                    .order_by(UploadSession.id)
                    .with_for_update()
                )
            ).all()
        )
    else:
        upload_ids = []

    storage_dir = request.app.state.settings.storage_dir
    release_plan = await plan_dataset_storage_release(session, dataset_ids)
    deletion_paths = [
        dataset.storage_path for dataset in datasets if dataset.storage_path
    ]
    deletion_paths.extend(
        upload_directory(request.app.state.settings, upload_id)
        for upload_id in upload_ids
    )
    deletion_paths.extend(
        contained_training_run_path(storage_dir, run.out_dir) for run in runs
    )

    staged_deletions = await stage_deletions_async(storage_dir, deletion_paths)
    try:
        for run in runs:
            await session.delete(run)
        await session.flush()
        for dataset in datasets:
            await session.delete(dataset)
        await session.flush()
        await apply_dataset_storage_release(session, release_plan)
        for project in projects:
            await session.delete(project)
        await session.execute(
            delete(Membership).where(Membership.user_id == user_id)
        )
        await session.execute(
            delete(Organization).where(Organization.owner_id == user_id)
        )
        await session.execute(
            delete(AdminUser).where(AdminUser.owner_id == user_id)
        )
        await session.execute(
            delete(UserStorage).where(UserStorage.owner_id == user_id)
        )
        await session.commit()
    except BaseException as error:
        restore_staged_deletions(reversed(staged_deletions))
        try:
            await session.rollback()
        except BaseException as rollback_error:
            error.add_note(
                "admin user delete rollback also failed: "
                f"{type(rollback_error).__name__}"
            )
        raise

    await asyncio.to_thread(finalize_staged_deletions, staged_deletions)

    settings = request.app.state.settings
    try:
        await delete_auth_account(settings.auth_database_url, user_id)
    except RuntimeError as error:
        # The project-side data is already gone; retrying the same delete
        # only removes the remaining login account.
        raise HTTPException(status_code=502, detail=str(error)) from error
    return Response(status_code=204)


@router.api_route(
    "/{rest:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def admin_catch_all(rest: str, _admin: AdminDep) -> None:
    # Unknown admin paths and mismatched methods behave exactly like any
    # nonexistent route — for admins and non-admins alike.
    raise HTTPException(status_code=404, detail="Not Found")
