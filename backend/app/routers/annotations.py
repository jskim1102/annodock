"""Image annotation reads and transactional auto-save replacement."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import CurrentUserDep, CurrentWorkspaceDep, EditorWorkspaceDep
from app.models import Annotation, Dataset, DatasetClass, Image
from app.services.assignments import lock_actor_workspace


router = APIRouter(prefix="/api/images", tags=["annotations"])
Session = Annotated[AsyncSession, Depends(get_session)]
Coordinate = Annotated[
    float,
    Field(ge=0.0, le=1.0, allow_inf_nan=False),
]


class BoxInput(BaseModel):
    class_id: int = Field(ge=0)
    cx: Coordinate
    cy: Coordinate
    w: Coordinate
    h: Coordinate


class BoxRow(BoxInput):
    model_config = ConfigDict(from_attributes=True)

    id: int


class AnnotationWrite(BaseModel):
    boxes: list[BoxInput]
    expected_version: int | None = Field(default=None, ge=0, strict=True)
    expected_catalog_version: int | None = Field(default=None, ge=0, strict=True)


class AnnotationState(BaseModel):
    version: int
    label_status: str
    assignee_id: int | None


class AnnotationClass(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    class_id: int
    name: str


class AnnotationRead(AnnotationState):
    image_id: int
    width: int
    height: int
    boxes: list[BoxRow]
    classes: list[AnnotationClass]
    catalog_version: int


class AnnotationSaved(AnnotationState):
    image_id: int
    boxes: list[BoxRow]
    is_modified: bool
    classes: list[AnnotationClass]
    catalog_version: int


async def _catalog(session: AsyncSession, dataset_id: int) -> tuple[int, list[AnnotationClass]]:
    # One statement pairs the generation with its names, including an empty
    # catalog. GET must not lock Dataset: project merge uses Dataset NOWAIT.
    rows = (await session.execute(
        select(Dataset.class_catalog_version, DatasetClass.class_id, DatasetClass.name)
        .outerjoin(DatasetClass, DatasetClass.dataset_id == Dataset.id)
        .where(Dataset.id == dataset_id)
        .order_by(DatasetClass.class_id)
    )).all()
    return rows[0].class_catalog_version, [
        AnnotationClass(class_id=row.class_id, name=row.name)
        for row in rows if row.class_id is not None
    ]


async def _image_or_404(
    session: AsyncSession,
    image_id: int,
    workspace_id: int,
) -> Image:
    image = await session.scalar(
        select(Image)
        .join(Dataset, Dataset.id == Image.dataset_id)
        .where(
            Image.id == image_id,
            Dataset.workspace_id == workspace_id,
        )
        .with_for_update(read=True, of=Image)
    )
    if image is None:
        raise HTTPException(status_code=404, detail="이미지를 찾을 수 없습니다.")
    return image


@router.get("/{image_id}/annotations", response_model=AnnotationRead)
async def get_annotations(
    image_id: int,
    session: Session,
    current_workspace: CurrentWorkspaceDep,
) -> AnnotationRead:
    image = await _image_or_404(session, image_id, current_workspace.id)
    rows = (
        await session.scalars(
            select(Annotation)
            .where(Annotation.image_id == image_id)
            .order_by(Annotation.id)
        )
    ).all()
    catalog_version, classes = await _catalog(session, image.dataset_id)
    return AnnotationRead(
        image_id=image.id,
        width=image.width,
        height=image.height,
        boxes=[BoxRow.model_validate(row) for row in rows],
        version=image.version,
        label_status=image.label_status,
        assignee_id=image.assignee_id,
        # A fresh read after a conflict must pair the catalog with the same
        # image version; a separate classes endpoint can race with a remap.
        classes=classes,
        catalog_version=catalog_version,
    )


@router.put("/{image_id}/annotations", response_model=AnnotationSaved)
async def replace_annotations(
    image_id: int,
    body: AnnotationWrite,
    session: Session,
    current_workspace: EditorWorkspaceDep,
    current_user: CurrentUserDep,
) -> AnnotationSaved:
    role = await lock_actor_workspace(session, current_workspace.id, current_user.id, "editor")
    locked = (
        await session.execute(
            select(Dataset, Image)
            .join(Image, Image.dataset_id == Dataset.id)
            .where(
                Image.id == image_id,
                Dataset.workspace_id == current_workspace.id,
            )
            .with_for_update()
        )
    ).one_or_none()
    if locked is None:
        raise HTTPException(status_code=404, detail="이미지를 찾을 수 없습니다.")
    dataset, image = locked
    if body.expected_version is None:
        raise HTTPException(status_code=422, detail={"code": "expected_version_required"})
    catalog_version, classes = await _catalog(session, dataset.id)
    conflict_code = None
    if body.expected_version != image.version:
        conflict_code = "version_conflict"
    elif body.expected_catalog_version is None:
        raise HTTPException(status_code=422, detail={"code": "expected_catalog_version_required"})
    elif body.expected_catalog_version != catalog_version:
        conflict_code = "class_catalog_changed"
    if conflict_code:
        current_boxes = (await session.scalars(select(Annotation).where(
            Annotation.image_id == image.id,
        ).order_by(Annotation.id))).all()
        raise HTTPException(status_code=409, detail={
            "code": conflict_code,
            "current": {
                "version": image.version, "label_status": image.label_status,
                "assignee_id": image.assignee_id,
                "boxes": [BoxRow.model_validate(row).model_dump() for row in current_boxes],
                "classes": [row.model_dump() for row in classes],
                "catalog_version": catalog_version,
            },
        })
    if image.label_status == "in_progress" and image.assignee_id != current_user.id and role not in {"admin", "owner"}:
        raise HTTPException(status_code=409, detail={
            "code": "image_assigned_to_other", "assignee_id": image.assignee_id,
        })
    old_count = image.box_count

    requested_class_ids = {box.class_id for box in body.boxes}
    if requested_class_ids:
        known_class_ids = {row.class_id for row in classes}
        unknown_class_ids = requested_class_ids - known_class_ids
        if unknown_class_ids:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "unknown_annotation_class",
                    "message": "데이터셋에 없는 클래스는 저장할 수 없습니다.",
                    "class_ids": sorted(unknown_class_ids),
                },
            )

    await session.execute(
        delete(Annotation).where(Annotation.image_id == image_id)
    )
    rows = [
        Annotation(
            image_id=image_id,
            class_id=box.class_id,
            cx=box.cx,
            cy=box.cy,
            w=box.w,
            h=box.h,
        )
        for box in body.boxes
    ]
    session.add_all(rows)
    image.box_count = len(rows)
    image.is_modified = True
    if image.label_status == "unlabeled" and image.assignee_id is None:
        image.label_status = "in_progress"
        image.assignee_id = current_user.id
        image.assigned_at = datetime.now(timezone.utc)
    image.version += 1
    dataset.annotation_count += len(rows) - old_count
    await session.flush()
    await session.commit()
    return AnnotationSaved(
        image_id=image.id,
        boxes=[BoxRow.model_validate(row) for row in rows],
        is_modified=image.is_modified,
        version=image.version,
        label_status=image.label_status,
        assignee_id=image.assignee_id,
        classes=classes,
        catalog_version=catalog_version,
    )
