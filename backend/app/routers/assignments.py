"""Workspace-scoped image claims, labeling lifecycle and assignment management."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import AdminWorkspaceDep, CurrentUserDep, EditorWorkspaceDep, ViewerWorkspaceDep
from app.routers.images import ImageRow
from app.services import assignments


router = APIRouter(prefix="/api", tags=["assignments"])
Session = Annotated[AsyncSession, Depends(get_session)]
UserId = Annotated[int, Field(gt=0, le=2**31 - 1, strict=True)]


class ClaimInput(BaseModel):
    split: str | None = Field(default=None, min_length=1, max_length=64)


class ClaimResult(BaseModel):
    image: ImageRow | None
    position: int | None = None


class DistributeInput(BaseModel):
    user_ids: list[UserId] = Field(min_length=1)


class ReclaimInput(BaseModel):
    user_id: UserId


class AssignmentInput(BaseModel):
    assignee_id: UserId | None


@router.post("/datasets/{dataset_id}/claim", response_model=ClaimResult, response_model_exclude_unset=True)
async def claim(
    dataset_id: int, body: ClaimInput, session: Session,
    current_workspace: EditorWorkspaceDep, current_user: CurrentUserDep,
) -> ClaimResult:
    await assignments.lock_actor_workspace(session, current_workspace.id, current_user.id, "editor")
    await assignments.dataset_or_404(session, dataset_id, current_workspace.id)
    image, position = await assignments.claim_image(
        session, dataset_id=dataset_id, actor_id=current_user.id, split=body.split,
    )
    await session.commit()
    if image is None:
        return ClaimResult(image=None)
    return ClaimResult(image=ImageRow.model_validate(image), position=position)


@router.post("/images/{image_id}/complete", response_model=ImageRow)
async def complete(
    image_id: int, session: Session, current_workspace: EditorWorkspaceDep, current_user: CurrentUserDep,
) -> ImageRow:
    role = await assignments.lock_actor_workspace(session, current_workspace.id, current_user.id, "editor")
    image = await assignments.locked_image_or_404(session, image_id, current_workspace.id)
    assignments.complete_image(image, actor_id=current_user.id, role=role)
    await session.commit()
    return ImageRow.model_validate(image)


@router.post("/images/{image_id}/uncomplete", response_model=ImageRow)
async def uncomplete(
    image_id: int, session: Session, current_workspace: EditorWorkspaceDep, current_user: CurrentUserDep,
) -> ImageRow:
    role = await assignments.lock_actor_workspace(session, current_workspace.id, current_user.id, "editor")
    image = await assignments.locked_image_or_404(session, image_id, current_workspace.id)
    assignments.uncomplete_image(image, actor_id=current_user.id, role=role)
    await session.commit()
    return ImageRow.model_validate(image)


@router.post("/datasets/{dataset_id}/assignments/distribute")
async def distribute(
    dataset_id: int, body: DistributeInput, session: Session,
    current_workspace: AdminWorkspaceDep, current_user: CurrentUserDep,
) -> dict:
    await assignments.lock_actor_workspace(session, current_workspace.id, current_user.id, "admin")
    await assignments.dataset_or_404(session, dataset_id, current_workspace.id)
    counts = await assignments.distribute_images(
        session, dataset_id=dataset_id, workspace_id=current_workspace.id, user_ids=body.user_ids,
    )
    await session.commit()
    return {"assigned": counts}


@router.post("/datasets/{dataset_id}/assignments/reclaim")
async def reclaim(
    dataset_id: int, body: ReclaimInput, session: Session,
    current_workspace: AdminWorkspaceDep, current_user: CurrentUserDep,
) -> dict:
    await assignments.lock_actor_workspace(session, current_workspace.id, current_user.id, "admin")
    await assignments.dataset_or_404(session, dataset_id, current_workspace.id)
    count = await assignments.reclaim_images(session, dataset_ids=[dataset_id], user_id=body.user_id)
    await session.commit()
    return {"reclaimed": count}


@router.patch("/images/{image_id}/assignment", response_model=ImageRow)
async def reassign(
    image_id: int, body: AssignmentInput, session: Session,
    current_workspace: AdminWorkspaceDep, current_user: CurrentUserDep,
) -> ImageRow:
    await assignments.lock_actor_workspace(session, current_workspace.id, current_user.id, "admin")
    image = await assignments.locked_image_or_404(session, image_id, current_workspace.id)
    if image.label_status == "labeled":
        raise HTTPException(status_code=409, detail={"code": "invalid_label_state"})
    if body.assignee_id is not None:
        await assignments.eligible_assignees(session, current_workspace.id, [body.assignee_id])
    assignments.assign_image(image, body.assignee_id)
    await session.commit()
    return ImageRow.model_validate(image)


@router.get("/datasets/{dataset_id}/progress")
async def progress(dataset_id: int, session: Session, current_workspace: ViewerWorkspaceDep) -> dict:
    await assignments.dataset_or_404(session, dataset_id, current_workspace.id)
    return await assignments.dataset_progress(session, dataset_id)
