"""Workspace and member management using the path's workspace scope."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import (
    CurrentUserDep,
    PathAdminWorkspaceDep,
    PathOwnerWorkspaceDep,
    PathViewerWorkspaceDep,
    ensure_personal_workspace,
)
from app.models import Workspace, WorkspaceMember
from app.services.admin import load_auth_directory
from app.services.workspaces import (
    change_workspace_member_role,
    create_team_workspace,
    delete_team_workspace,
    remove_workspace_member,
    team_workspace,
    transfer_workspace_owner,
)

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])
Session = Annotated[AsyncSession, Depends(get_session)]


class WorkspaceName(BaseModel):
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("워크스페이스 이름을 입력해 주세요.")
        return name


class WorkspaceRow(BaseModel):
    id: int
    kind: str
    name: str
    role: str


class WorkspaceList(BaseModel):
    items: list[WorkspaceRow]


class OwnerTransfer(BaseModel):
    user_id: int = Field(gt=0, le=2**31 - 1, strict=True)


class MemberRoleUpdate(BaseModel):
    role: Literal["admin", "editor"]


class MemberRoleRow(BaseModel):
    user_id: int
    role: str


class MemberRow(MemberRoleRow):
    email: str | None
    username: str | None
    joined_at: datetime


class MemberList(BaseModel):
    items: list[MemberRow]


def _workspace_row(workspace: Workspace, role: str) -> WorkspaceRow:
    return WorkspaceRow(
        id=workspace.id, kind=workspace.kind, name=workspace.name, role=role,
    )


@router.get("", response_model=WorkspaceList)
async def list_workspaces(current_user: CurrentUserDep, session: Session) -> WorkspaceList:
    await ensure_personal_workspace(session, current_user.id)
    await session.commit()
    rows = (await session.execute(
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == current_user.id)
        .order_by(Workspace.kind == "team", Workspace.id)
    )).all()
    return WorkspaceList(items=[_workspace_row(workspace, role) for workspace, role in rows])


@router.post("", status_code=201, response_model=WorkspaceRow)
async def create_workspace(
    body: WorkspaceName, request: Request, current_user: CurrentUserDep, session: Session,
) -> WorkspaceRow:
    workspace = await create_team_workspace(
        session, user_id=current_user.id, name=body.name,
        limit=request.app.state.settings.team_create_limit,
    )
    await session.commit()
    return _workspace_row(workspace, "owner")


@router.patch("/{workspace_id}", response_model=WorkspaceRow)
async def rename_workspace(
    workspace_id: int, body: WorkspaceName,
    current_workspace: PathAdminWorkspaceDep, session: Session,
) -> WorkspaceRow:
    workspace = await team_workspace(session, current_workspace.id)
    workspace.name = body.name
    await session.commit()
    return _workspace_row(workspace, current_workspace.role)


@router.delete("/{workspace_id}", status_code=204)
async def delete_workspace(
    workspace_id: int, current_workspace: PathOwnerWorkspaceDep, session: Session,
) -> Response:
    await delete_team_workspace(session, current_workspace.id)
    await session.commit()
    return Response(status_code=204)


@router.post("/{workspace_id}/owner", response_model=WorkspaceRow)
async def transfer_owner(
    workspace_id: int, body: OwnerTransfer, current_workspace: PathOwnerWorkspaceDep,
    current_user: CurrentUserDep, session: Session,
) -> WorkspaceRow:
    await transfer_workspace_owner(
        session, workspace_id=current_workspace.id,
        actor_id=current_user.id, user_id=body.user_id,
    )
    workspace = await team_workspace(session, current_workspace.id)
    await session.commit()
    return _workspace_row(workspace, "owner" if current_user.id == body.user_id else "admin")


@router.get("/{workspace_id}/members", response_model=MemberList)
async def list_members(
    workspace_id: int, request: Request,
    current_workspace: PathViewerWorkspaceDep, session: Session,
) -> MemberList:
    members = (await session.scalars(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == current_workspace.id,
        ).order_by(WorkspaceMember.created_at, WorkspaceMember.user_id)
    )).all()
    try:
        directory = await load_auth_directory(request.app.state.settings.auth_database_url)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    users = {user.id: user for user in directory}
    return MemberList(items=[
        MemberRow(
            user_id=member.user_id, role=member.role, joined_at=member.created_at,
            email=users[member.user_id].email if member.user_id in users else None,
            username=users[member.user_id].username if member.user_id in users else None,
        )
        for member in members
    ])


@router.patch("/{workspace_id}/members/{user_id}", response_model=MemberRoleRow)
async def update_member_role(
    workspace_id: int, user_id: int, body: MemberRoleUpdate,
    current_workspace: PathAdminWorkspaceDep, session: Session,
) -> MemberRoleRow:
    member = await change_workspace_member_role(
        session, workspace_id=current_workspace.id, user_id=user_id, role=body.role,
    )
    await session.commit()
    return MemberRoleRow(user_id=member.user_id, role=member.role)


@router.delete("/{workspace_id}/members/me", status_code=204)
async def leave_workspace(
    workspace_id: int, current_workspace: PathViewerWorkspaceDep,
    current_user: CurrentUserDep, session: Session,
) -> Response:
    await remove_workspace_member(
        session, workspace_id=current_workspace.id, user_id=current_user.id, leaving=True,
    )
    await session.commit()
    return Response(status_code=204)


@router.delete("/{workspace_id}/members/{user_id}", status_code=204)
async def delete_member(
    workspace_id: int, user_id: int,
    current_workspace: PathAdminWorkspaceDep, session: Session,
) -> Response:
    await remove_workspace_member(session, workspace_id=current_workspace.id, user_id=user_id)
    await session.commit()
    return Response(status_code=204)
