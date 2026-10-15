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
from app.deps import (
    CurrentUser,
    ensure_personal_workspace,
    find_personal_workspace,
    get_current_user,
)
from app.models import (
    AdminUser,
    Dataset,
    Project,
    TrainingRun,
    UploadSession,
    Workspace,
    WorkspaceMember,
    WorkspaceStorage,
)
from app.services.admin import (
    AuthDirectoryUser,
    delete_auth_account,
    is_admin,
    load_auth_directory,
    load_workspace_usage,
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
from app.services.workspaces import (
    lock_account_team_memberships,
    release_unreferenced_workspace_media,
    remove_workspace_member,
    revoke_created_workspace_invites,
)

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


class AdminWorkspaceBase(BaseModel):
    id: int
    kind: str
    name: str
    bytes_used: int
    limit_bytes: int
    quota_override: bool


class AdminUserWorkspaceRow(AdminWorkspaceBase):
    role: str


class AdminWorkspaceRow(AdminWorkspaceBase):
    owner_user_id: int | None
    member_count: int


class AdminWorkspacesResponse(BaseModel):
    workspaces: list[AdminWorkspaceRow]


class AdminUserRow(BaseModel):
    id: int
    email: str | None
    username: str | None
    created_at: str
    login_methods: list[str]
    bytes_used: int
    limit_bytes: int
    workspaces: list[AdminUserWorkspaceRow]


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


class AdminWorkspaceQuotaUpdateResponse(BaseModel):
    workspace_id: int
    limit_bytes: int


async def _workspace_rows(
    session: AsyncSession, default_limit_bytes: int,
) -> tuple[dict[int, list[AdminUserWorkspaceRow]], list[AdminWorkspaceRow]]:
    by_user: dict[int, list[AdminUserWorkspaceRow]] = {}
    workspaces: dict[int, AdminWorkspaceRow] = {}
    for row in await load_workspace_usage(session):
        fields = dict(id=row.id, kind=row.kind, name=row.name, bytes_used=row.bytes_used,
                      limit_bytes=row.quota_limit_bytes if row.quota_limit_bytes is not None else default_limit_bytes,
                      quota_override=row.quota_limit_bytes is not None)
        if row.id not in workspaces:
            workspaces[row.id] = AdminWorkspaceRow(**fields, owner_user_id=None, member_count=0)
        if row.user_id is not None:
            workspaces[row.id].member_count += 1
            if row.role == "owner":
                workspaces[row.id].owner_user_id = row.user_id
            by_user.setdefault(row.user_id, []).append(AdminUserWorkspaceRow(**fields, role=row.role))
    return by_user, sorted(workspaces.values(), key=lambda row: (-row.bytes_used, row.id))


def _row(
    user: AuthDirectoryUser,
    memberships: dict[int, list[AdminUserWorkspaceRow]],
    default_limit_bytes: int,
) -> AdminUserRow:
    workspaces = memberships.get(user.id, [])
    personal = next((row for row in workspaces if row.kind == "personal"), None)
    return AdminUserRow(
        id=user.id,
        email=user.email,
        username=user.username,
        created_at=user.created_at.isoformat(),
        login_methods=list(user.login_methods),
        bytes_used=personal.bytes_used if personal is not None else 0,
        limit_bytes=personal.limit_bytes if personal is not None else default_limit_bytes,
        workspaces=workspaces,
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
    total = await session.scalar(select(func.coalesce(func.sum(WorkspaceStorage.bytes_used), 0)))
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
    memberships, _workspaces = await _workspace_rows(session, request.app.state.settings.quota_bytes_per_user)
    rows = [
        _row(
            user,
            memberships,
            request.app.state.settings.quota_bytes_per_user,
        )
        for user in directory
    ]
    rows.sort(key=lambda item: (-item.bytes_used, item.id))
    return AdminUsersResponse(users=rows)


@router.get("/workspaces", response_model=AdminWorkspacesResponse)
async def admin_workspaces(
    request: Request, _admin: AdminDep,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AdminWorkspacesResponse:
    await _directory(request)
    _memberships, workspaces = await _workspace_rows(session, request.app.state.settings.quota_bytes_per_user)
    return AdminWorkspacesResponse(workspaces=workspaces)


@router.patch("/workspaces/{workspace_id}/quota", response_model=AdminWorkspaceQuotaUpdateResponse)
async def admin_update_workspace_quota(
    workspace_id: int, body: AdminQuotaUpdate, request: Request, _admin: AdminDep,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AdminWorkspaceQuotaUpdateResponse:
    await _directory(request)
    if not 0 < workspace_id <= 2**31 - 1:
        raise HTTPException(status_code=404, detail="Not Found")
    await set_local_lock_timeout(session)
    workspace = await session.scalar(select(Workspace.id).where(
        Workspace.id == workspace_id,
    ).with_for_update())
    if workspace is None:
        raise HTTPException(status_code=404, detail="Not Found")
    limit_bytes = await set_quota_limit(session, workspace_id, body.limit_bytes)
    await session.commit()
    return AdminWorkspaceQuotaUpdateResponse(workspace_id=workspace_id, limit_bytes=limit_bytes)


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
    workspace = await ensure_personal_workspace(session, user_id)
    limit_bytes = await set_quota_limit(
        session,
        workspace.id,
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

    team_workspace_ids = await lock_account_team_memberships(session, user_id)

    # Admin URLs continue to name auth users. Resolve their personal scope
    # through membership; a workspace id can also name an unrelated team.
    workspace = await find_personal_workspace(session, user_id)
    workspace_id = workspace.id if workspace is not None else None
    projects = list(
        (
            await session.scalars(
                select(Project)
                .where(Project.workspace_id == workspace_id)
                .order_by(Project.id)
                .with_for_update()
            )
        ).all()
    )
    datasets = list(
        (
            await session.scalars(
                select(Dataset)
                .where(Dataset.workspace_id == workspace_id)
                .order_by(Dataset.id)
                .with_for_update()
            )
        ).all()
    )
    runs = list(
        (
            await session.scalars(
                select(TrainingRun)
                .where(TrainingRun.workspace_id == workspace_id)
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
            select(WorkspaceStorage.bytes_used).where(
                WorkspaceStorage.workspace_id == workspace_id
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
        await revoke_created_workspace_invites(session, user_id)
        for team_workspace_id in team_workspace_ids:
            await remove_workspace_member(
                session, workspace_id=team_workspace_id, user_id=user_id,
            )
        for run in runs:
            await session.delete(run)
        await session.flush()
        for dataset in datasets:
            await session.delete(dataset)
        await session.flush()
        await apply_dataset_storage_release(session, release_plan)
        if workspace is not None:
            await release_unreferenced_workspace_media(session, workspace.id)
        for project in projects:
            await session.delete(project)
        await session.execute(
            delete(AdminUser).where(AdminUser.user_id == user_id)
        )
        await session.execute(
            delete(WorkspaceStorage).where(WorkspaceStorage.workspace_id == workspace_id)
        )
        if workspace is not None:
            # Resource FKs are restrictive. Flush their deletions before the
            # personal workspace and its cascading membership rows are removed.
            await session.flush()
            await session.delete(workspace)
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
