"""Workspace membership and cleanup inside the caller's transaction."""

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import ensure_personal_workspace
from app.models import (
    Image,
    MediaObject,
    Project,
    TrainingRun,
    Workspace,
    WorkspaceInvite,
    WorkspaceMember,
    WorkspaceStorage,
)
from app.services.quota import decrease_bytes_used
from app.services.assignments import reclaim_workspace_assignments


async def team_workspace(session: AsyncSession, workspace_id: int) -> Workspace:
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="워크스페이스를 찾을 수 없습니다.")
    if workspace.kind == "personal":
        raise HTTPException(status_code=409, detail={"code": "personal_workspace_fixed"})
    return workspace


async def create_team_workspace(
    session: AsyncSession, *, user_id: int, name: str, limit: int,
) -> Workspace:
    personal = await ensure_personal_workspace(session, user_id)
    # All team creation by this account shares a persistent serialization row,
    # including accounts that have not created their personal workspace yet.
    await session.execute(
        select(Workspace.id).where(Workspace.id == personal.id).with_for_update()
    )
    created_count = await session.scalar(
        select(func.count()).select_from(Workspace).where(
            Workspace.kind == "team", Workspace.created_by == user_id,
        )
    )
    if created_count >= limit:
        raise HTTPException(
            status_code=409,
            detail={"code": "team_limit_reached", "limit": limit},
        )
    workspace = Workspace(kind="team", name=name, created_by=user_id)
    session.add(workspace)
    await session.flush()
    session.add_all([
        WorkspaceMember(workspace_id=workspace.id, user_id=user_id, role="owner"),
        WorkspaceStorage(workspace_id=workspace.id, bytes_used=0, quota_limit_bytes=None),
    ])
    await session.flush()
    return workspace


async def delete_team_workspace(session: AsyncSession, workspace_id: int) -> None:
    workspace = await team_workspace(session, workspace_id)
    projects = (await session.execute(
        select(Project.id, Project.name)
        .where(Project.workspace_id == workspace_id).order_by(Project.id)
    )).all()
    runs = (await session.execute(
        select(TrainingRun.id, TrainingRun.dataset_name)
        .where(TrainingRun.workspace_id == workspace_id).order_by(TrainingRun.id)
    )).all()
    if projects or runs:
        detail = {
            "code": "workspace_not_empty",
            "projects": [{"id": row.id, "name": row.name} for row in projects],
        }
        if runs:
            detail["runs"] = [
                {"id": row.id, "dataset_name": row.dataset_name}
                for row in runs
            ]
        raise HTTPException(status_code=409, detail=detail)
    await release_unreferenced_workspace_media(session, workspace_id)
    await session.delete(workspace)


async def workspace_member(
    session: AsyncSession, workspace_id: int, user_id: int,
) -> WorkspaceMember:
    if not 0 < user_id <= 2**31 - 1:
        raise HTTPException(status_code=404, detail="멤버를 찾을 수 없습니다.")
    member = await session.get(WorkspaceMember, (workspace_id, user_id))
    if member is None:
        raise HTTPException(status_code=404, detail="멤버를 찾을 수 없습니다.")
    return member


async def transfer_workspace_owner(
    session: AsyncSession, *, workspace_id: int, actor_id: int, user_id: int,
) -> None:
    await team_workspace(session, workspace_id)
    target = await workspace_member(session, workspace_id, user_id)
    if actor_id == user_id:
        return
    owner = await workspace_member(session, workspace_id, actor_id)
    owner.role = "admin"
    # The partial unique index is immediate: demote before promoting while the
    # workspace lock keeps other ownership mutations outside this transaction.
    await session.flush()
    target.role = "owner"
    await session.flush()


async def change_workspace_member_role(
    session: AsyncSession, *, workspace_id: int, user_id: int, role: str,
) -> WorkspaceMember:
    await team_workspace(session, workspace_id)
    member = await workspace_member(session, workspace_id, user_id)
    if member.role == "owner":
        raise HTTPException(status_code=409, detail={"code": "owner_role_fixed"})
    if role not in {"admin", "owner"}:
        await revoke_created_workspace_invites(session, user_id, workspace_id=workspace_id)
    if role == "viewer":
        await reclaim_workspace_assignments(session, workspace_id, user_id)
    member.role = role
    return member


async def remove_workspace_member(
    session: AsyncSession, *, workspace_id: int, user_id: int, leaving: bool = False,
) -> None:
    await team_workspace(session, workspace_id)
    member = await workspace_member(session, workspace_id, user_id)
    if member.role == "owner":
        raise HTTPException(status_code=409, detail={
            "code": "owner_must_transfer" if leaving else "owner_role_fixed",
        })
    await revoke_created_workspace_invites(session, user_id, workspace_id=workspace_id)
    await reclaim_workspace_assignments(session, workspace_id, user_id)
    await session.delete(member)


async def revoke_created_workspace_invites(
    session: AsyncSession, user_id: int, *, workspace_id: int | None = None,
) -> None:
    """Revoke links in the caller's membership/account deletion transaction."""
    statement = update(WorkspaceInvite).where(
        WorkspaceInvite.created_by == user_id,
        WorkspaceInvite.revoked_at.is_(None),
    )
    if workspace_id is not None:
        statement = statement.where(WorkspaceInvite.workspace_id == workspace_id)
    await session.execute(statement.values(revoked_at=datetime.now(timezone.utc)))


async def lock_account_team_memberships(
    session: AsyncSession, user_id: int,
) -> list[int]:
    """Lock membership/link teams and reject deletion while the user owns any."""
    workspace_ids = list((await session.scalars(
        select(Workspace.id)
        .where(Workspace.kind == "team", or_(
            select(WorkspaceMember.workspace_id).where(
                WorkspaceMember.workspace_id == Workspace.id,
                WorkspaceMember.user_id == user_id,
            ).exists(),
            # Historical links can outlive membership. Lock those teams too
            # before revoking their invites, matching acceptance's lock order.
            select(WorkspaceInvite.id).where(
                WorkspaceInvite.workspace_id == Workspace.id,
                WorkspaceInvite.created_by == user_id,
                WorkspaceInvite.revoked_at.is_(None),
            ).exists(),
        ))
        .order_by(Workspace.id)
        .with_for_update(of=Workspace)
    )).all())
    if not workspace_ids:
        return []
    # Ownership can change while the first query waits for a workspace lock.
    # Use a new statement after all locks, rather than its prior role snapshot.
    rows = (await session.execute(
        select(Workspace.id, Workspace.name, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(Workspace.id.in_(workspace_ids), WorkspaceMember.user_id == user_id)
        .order_by(Workspace.id)
    )).all()
    owned = [{"id": row.id, "name": row.name} for row in rows if row.role == "owner"]
    if owned:
        raise HTTPException(status_code=409, detail={
            "code": "admin-user-team-owner", "workspaces": owned,
        })
    return [row.id for row in rows]


async def release_unreferenced_workspace_media(
    session: AsyncSession,
    workspace_id: int,
) -> None:
    """Remove leftover media metadata and its accounted bytes before deletion.

    Physical image bundles live under dataset directories, which the account
    deletion caller stages and restores with the rest of its files. Media
    objects themselves have no independent filesystem paths.
    """

    released = (
        await session.execute(
            delete(MediaObject)
            .where(
                MediaObject.workspace_id == workspace_id,
                ~select(Image.id)
                .where(Image.media_object_id == MediaObject.id)
                .exists(),
            )
            .returning(
                MediaObject.original_bytes,
                MediaObject.display_bytes,
                MediaObject.thumb_bytes,
            )
            .execution_options(synchronize_session=False)
        )
    ).all()
    if released:
        await decrease_bytes_used(
            session,
            workspace_id,
            sum(original + display + thumb for original, display, thumb in released),
        )
