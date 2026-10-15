"""Hashed invitation links and membership changes in one transaction."""

from datetime import datetime, timedelta, timezone
import hashlib
import secrets

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Workspace, WorkspaceInvite, WorkspaceMember
from app.services.workspaces import team_workspace


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _available_to_new_member(invite: WorkspaceInvite) -> None:
    if invite.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail={"code": "invite_expired"})
    if invite.max_uses is not None and invite.uses >= invite.max_uses:
        raise HTTPException(status_code=410, detail={"code": "invite_exhausted"})


async def create_workspace_invite(
    session: AsyncSession, *, workspace_id: int, user_id: int, role: str,
    expires_in_days: int, max_uses: int | None,
) -> tuple[WorkspaceInvite, str]:
    await team_workspace(session, workspace_id)
    token = secrets.token_urlsafe(32)
    invite = WorkspaceInvite(
        workspace_id=workspace_id, role=role, token_hash=_token_hash(token),
        created_by=user_id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=expires_in_days),
        max_uses=max_uses, uses=0,
    )
    session.add(invite)
    await session.flush()
    return invite, token


async def active_workspace_invites(
    session: AsyncSession, workspace_id: int,
) -> list[WorkspaceInvite]:
    await team_workspace(session, workspace_id)
    return list((await session.scalars(
        select(WorkspaceInvite).where(
            WorkspaceInvite.workspace_id == workspace_id,
            WorkspaceInvite.revoked_at.is_(None),
            WorkspaceInvite.expires_at > datetime.now(timezone.utc),
            or_(WorkspaceInvite.max_uses.is_(None),
                WorkspaceInvite.uses < WorkspaceInvite.max_uses),
        ).order_by(WorkspaceInvite.created_at.desc(), WorkspaceInvite.id.desc())
    )).all())


async def revoke_workspace_invite(
    session: AsyncSession, *, workspace_id: int, invite_id: int,
) -> None:
    await team_workspace(session, workspace_id)
    if not 0 < invite_id <= 2**31 - 1:
        raise HTTPException(status_code=404, detail="초대 링크를 찾을 수 없습니다.")
    invite = await session.scalar(
        select(WorkspaceInvite).where(
            WorkspaceInvite.id == invite_id,
            WorkspaceInvite.workspace_id == workspace_id,
            WorkspaceInvite.revoked_at.is_(None),
        ).with_for_update()
    )
    if invite is None:
        raise HTTPException(status_code=404, detail="초대 링크를 찾을 수 없습니다.")
    invite.revoked_at = datetime.now(timezone.utc)


async def _invite_context(
    session: AsyncSession, token: str, *, lock: bool,
) -> tuple[WorkspaceInvite, Workspace]:
    token_hash = _token_hash(token)
    workspace_id = await session.scalar(
        select(WorkspaceInvite.workspace_id).where(WorkspaceInvite.token_hash == token_hash)
    )
    if workspace_id is None:
        raise HTTPException(status_code=404, detail="초대 링크를 찾을 수 없습니다.")
    workspace_query = select(Workspace).where(Workspace.id == workspace_id)
    if lock:
        # Revoke and team deletion already lock the workspace before its
        # invitation rows. Acceptance takes the same order to avoid deadlocks.
        workspace_query = workspace_query.with_for_update()
    workspace = await session.scalar(workspace_query)
    if workspace is None or workspace.kind != "team":
        raise HTTPException(status_code=404, detail="초대 링크를 찾을 수 없습니다.")
    invite_query = select(WorkspaceInvite).where(
        WorkspaceInvite.token_hash == token_hash,
        WorkspaceInvite.workspace_id == workspace_id,
    )
    if lock:
        invite_query = invite_query.with_for_update()
    invite = await session.scalar(invite_query)
    if invite is None:
        raise HTTPException(status_code=404, detail="초대 링크를 찾을 수 없습니다.")
    creator_role = await session.scalar(select(WorkspaceMember.role).where(
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == invite.created_by,
    ))
    # Check eligibility before the already-member shortcut, so a removed
    # creator cannot recover their previous role through their own link.
    if creator_role not in {"admin", "owner"} or invite.role == "viewer":
        raise HTTPException(status_code=410, detail={"code": "invite_revoked"})
    if invite.revoked_at is not None:
        raise HTTPException(status_code=404, detail="초대 링크를 찾을 수 없습니다.")
    return invite, workspace


async def preview_workspace_invite(
    session: AsyncSession, *, token: str, user_id: int,
) -> tuple[WorkspaceInvite, Workspace, WorkspaceMember | None]:
    invite, workspace = await _invite_context(session, token, lock=False)
    member = await session.get(WorkspaceMember, (workspace.id, user_id))
    if member is None:
        _available_to_new_member(invite)
    return invite, workspace, member


async def accept_workspace_invite(
    session: AsyncSession, *, token: str, user_id: int,
) -> tuple[int, str, bool]:
    invite, workspace = await _invite_context(session, token, lock=True)
    member = await session.get(WorkspaceMember, (workspace.id, user_id))
    if member is not None:
        # A successful last-use acceptance must be safely retryable. Existing
        # members retain their current role even when this link grants another.
        return workspace.id, member.role, True
    _available_to_new_member(invite)
    session.add(WorkspaceMember(
        workspace_id=workspace.id, user_id=user_id, role=invite.role,
    ))
    invite.uses += 1
    await session.flush()
    return workspace.id, invite.role, False
