"""Invitation management and authenticated link acceptance."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import CurrentUserDep, PathAdminWorkspaceDep
from app.services.invites import (
    accept_workspace_invite,
    active_workspace_invites,
    create_workspace_invite,
    preview_workspace_invite,
    revoke_workspace_invite,
)

router = APIRouter(prefix="/api", tags=["invites"])
Session = Annotated[AsyncSession, Depends(get_session)]


class InviteCreate(BaseModel):
    role: Literal["admin", "editor"]
    expires_in_days: int = Field(default=7, ge=1, le=30, strict=True)
    max_uses: int | None = Field(default=None, ge=1, le=2**31 - 1, strict=True)


class InviteRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    expires_at: datetime
    max_uses: int | None
    uses: int
    created_by: int
    created_at: datetime


class InviteCreated(BaseModel):
    invite: InviteRow
    accept_url: str


class InvitePreview(BaseModel):
    workspace_id: int
    workspace_name: str
    role: str
    expires_at: datetime
    already_member: bool


class InviteAccepted(BaseModel):
    workspace_id: int
    role: str
    already_member: bool


@router.post("/workspaces/{workspace_id}/invites", status_code=201, response_model=InviteCreated)
async def create_invite(
    workspace_id: int, body: InviteCreate, current_workspace: PathAdminWorkspaceDep,
    current_user: CurrentUserDep, session: Session,
) -> InviteCreated:
    invite, token = await create_workspace_invite(
        session, workspace_id=current_workspace.id, user_id=current_user.id,
        role=body.role, expires_in_days=body.expires_in_days, max_uses=body.max_uses,
    )
    await session.commit()
    return InviteCreated(invite=InviteRow.model_validate(invite), accept_url=f"/invites/{token}")


@router.get("/workspaces/{workspace_id}/invites", response_model=list[InviteRow])
async def list_invites(
    workspace_id: int, current_workspace: PathAdminWorkspaceDep, session: Session,
) -> list[InviteRow]:
    invites = await active_workspace_invites(session, current_workspace.id)
    return [InviteRow.model_validate(invite) for invite in invites]


@router.delete("/workspaces/{workspace_id}/invites/{invite_id}", status_code=204)
async def revoke_invite(
    workspace_id: int, invite_id: int,
    current_workspace: PathAdminWorkspaceDep, session: Session,
) -> Response:
    await revoke_workspace_invite(
        session, workspace_id=current_workspace.id, invite_id=invite_id,
    )
    await session.commit()
    return Response(status_code=204)


@router.get("/invites/{token}", response_model=InvitePreview)
async def preview_invite(
    token: str, current_user: CurrentUserDep, session: Session,
) -> InvitePreview:
    invite, workspace, member = await preview_workspace_invite(
        session, token=token, user_id=current_user.id,
    )
    return InvitePreview(
        workspace_id=workspace.id, workspace_name=workspace.name,
        role=member.role if member is not None else invite.role,
        expires_at=invite.expires_at, already_member=member is not None,
    )


@router.post("/invites/{token}/accept", response_model=InviteAccepted)
async def accept_invite(
    token: str, current_user: CurrentUserDep, session: Session,
) -> InviteAccepted:
    workspace_id, role, already_member = await accept_workspace_invite(
        session, token=token, user_id=current_user.id,
    )
    await session.commit()
    return InviteAccepted(workspace_id=workspace_id, role=role, already_member=already_member)
