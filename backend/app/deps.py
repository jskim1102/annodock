"""FastAPI dependencies shared by authenticated API routers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any
import re

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import (
    Dataset, Image, Project, TrainingRun, UploadJob, UploadSession,
    Workspace, WorkspaceMember,
)


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """The auth-service user established for one request."""

    id: int
    subject: str
    claims: Mapping[str, Any]


def get_current_user(request: Request) -> CurrentUser:
    """Return middleware-verified identity, failing closed if it is absent."""

    current_user = getattr(request.state, "current_user", None)
    if not isinstance(current_user, CurrentUser):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="인증이 필요합니다.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return current_user


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


@dataclass(frozen=True, slots=True)
class CurrentWorkspace:
    """The member-verified resource scope of one authenticated request."""

    id: int
    kind: str
    role: str


async def find_personal_workspace(session: AsyncSession, user_id: int) -> Workspace | None:
    return await session.scalar(
        select(Workspace)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(Workspace.kind == "personal", Workspace.created_by == user_id,
               WorkspaceMember.user_id == user_id)
    )


async def ensure_personal_workspace(session: AsyncSession, user_id: int) -> Workspace:
    """Create a personal scope atomically within the caller's transaction."""
    workspace = await find_personal_workspace(session, user_id)
    if workspace is not None:
        return workspace
    workspace_id = await session.scalar(
        insert(Workspace).values(kind="personal", name="개인", created_by=user_id)
        .on_conflict_do_nothing(
            index_elements=[Workspace.created_by],
            index_where=Workspace.kind == "personal",
        ).returning(Workspace.id)
    )
    if workspace_id is None:
        workspace = await session.scalar(select(Workspace).where(
            Workspace.kind == "personal", Workspace.created_by == user_id,
        ))
    else:
        workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise RuntimeError("personal workspace creation did not produce a row")
    await session.execute(
        insert(WorkspaceMember).values(workspace_id=workspace.id, user_id=user_id, role="owner")
        .on_conflict_do_nothing(index_elements=[WorkspaceMember.workspace_id, WorkspaceMember.user_id])
    )
    return workspace


async def get_current_workspace(
    request: Request,
    current_user: CurrentUserDep,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CurrentWorkspace:
    raw = request.headers.get("X-Workspace-Id")
    if raw is None:
        workspace = await find_personal_workspace(session, current_user.id)
        if workspace is None:
            workspace = await ensure_personal_workspace(session, current_user.id)
            # GET routes also persist first-time creation. Existing personal
            # scopes must not add a commit to the resource's transaction.
            await session.commit()
        return CurrentWorkspace(workspace.id, workspace.kind, "owner")
    if not re.fullmatch(r"[0-9]+", raw) or not raw.strip("0"):
        raise HTTPException(status_code=400, detail={
            "code": "invalid_workspace_header",
            "message": "워크스페이스 번호는 양의 정수여야 합니다.",
        })
    normalized = raw.lstrip("0")
    if len(normalized) > 10 or int(normalized) > 2**31 - 1:
        raise HTTPException(status_code=404, detail="워크스페이스를 찾을 수 없습니다.")
    row = (await session.execute(
        select(Workspace.id, Workspace.kind, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(Workspace.id == int(normalized), WorkspaceMember.user_id == current_user.id)
    )).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="워크스페이스를 찾을 수 없습니다.")
    return CurrentWorkspace(row.id, row.kind, row.role)


CurrentWorkspaceDep = Annotated[CurrentWorkspace, Depends(get_current_workspace)]


ROLE_ORDER = {"viewer": 0, "editor": 1, "admin": 2, "owner": 3}


def check_workspace_role(workspace: CurrentWorkspace, minimum_role: str) -> None:
    if ROLE_ORDER.get(workspace.role, -1) < ROLE_ORDER[minimum_role]:
        raise HTTPException(status_code=403, detail={
            "code": "insufficient_role", "required": minimum_role,
            "actual": workspace.role,
        })


async def _check_path_resource_scope(
    request: Request, session: AsyncSession, workspace_id: int,
) -> None:
    # Only denied callers need this extra lookup: allowed callers retain the
    # endpoint's existing scope checks and locking order. Do not reveal a
    # foreign resource by changing its established 404 into a role error.
    for parameter, model in (
        ("project_id", Project), ("dataset_id", Dataset),
        ("image_id", Image), ("upload_id", UploadSession),
        ("job_id", UploadJob), ("run_id", TrainingRun),
    ):
        if parameter not in request.path_params:
            continue
        try:
            resource_id = int(request.path_params[parameter])
        except (ValueError, TypeError):
            continue  # The endpoint's typed path validation handles this.
        if not 0 < resource_id <= 2**31 - 1:
            raise HTTPException(status_code=404, detail="Not Found")
        if model in (Image, UploadSession, UploadJob):
            query = select(Dataset.workspace_id).join(
                model, model.dataset_id == Dataset.id,
            ).where(model.id == resource_id)
        else:
            query = select(model.workspace_id).where(model.id == resource_id)
        if await session.scalar(query) != workspace_id:
            raise HTTPException(status_code=404, detail="Not Found")


def require_role(minimum_role: str):
    if minimum_role not in ROLE_ORDER:
        raise ValueError("unknown workspace role")

    async def dependency(
        request: Request,
        current_workspace: CurrentWorkspaceDep,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> CurrentWorkspace:
        if ROLE_ORDER.get(current_workspace.role, -1) < ROLE_ORDER[minimum_role]:
            await _check_path_resource_scope(request, session, current_workspace.id)
        check_workspace_role(current_workspace, minimum_role)
        return current_workspace

    dependency.minimum_role = minimum_role
    return dependency


ViewerWorkspaceDep = Annotated[CurrentWorkspace, Depends(require_role("viewer"))]
EditorWorkspaceDep = Annotated[CurrentWorkspace, Depends(require_role("editor"))]
AdminWorkspaceDep = Annotated[CurrentWorkspace, Depends(require_role("admin"))]
OwnerWorkspaceDep = Annotated[CurrentWorkspace, Depends(require_role("owner"))]


def require_workspace_role(minimum_role: str):
    """Resolve workspace management URLs by their path, ignoring the header."""
    if minimum_role not in ROLE_ORDER:
        raise ValueError("unknown workspace role")

    async def dependency(
        workspace_id: int,
        request: Request,
        current_user: CurrentUserDep,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> CurrentWorkspace:
        if not 0 < workspace_id <= 2**31 - 1:
            raise HTTPException(status_code=404, detail="Not Found")
        query = select(Workspace).where(Workspace.id == workspace_id)
        if request.method not in {"GET", "HEAD"}:
            # Serialize membership/ownership changes on their workspace. Read
            # membership AFTER acquiring the lock so a waiting request cannot
            # act with a role which a preceding request has already revoked.
            query = query.with_for_update()
        workspace = await session.scalar(query)
        member = await session.get(WorkspaceMember, (workspace_id, current_user.id))
        if workspace is None or member is None:
            raise HTTPException(status_code=404, detail="Not Found")
        result = CurrentWorkspace(workspace.id, workspace.kind, member.role)
        check_workspace_role(result, minimum_role)
        return result

    dependency.minimum_role = minimum_role
    return dependency


PathViewerWorkspaceDep = Annotated[CurrentWorkspace, Depends(require_workspace_role("viewer"))]
PathAdminWorkspaceDep = Annotated[CurrentWorkspace, Depends(require_workspace_role("admin"))]
PathOwnerWorkspaceDep = Annotated[CurrentWorkspace, Depends(require_workspace_role("owner"))]
