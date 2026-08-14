"""Image annotation reads and transactional auto-save replacement."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import CurrentUserDep
from app.models import Annotation, Dataset, Image


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


class AnnotationRead(BaseModel):
    image_id: int
    width: int
    height: int
    boxes: list[BoxRow]


class AnnotationSaved(BaseModel):
    image_id: int
    boxes: list[BoxRow]
    is_modified: bool


async def _image_or_404(
    session: AsyncSession,
    image_id: int,
    owner_id: int,
) -> Image:
    image = await session.scalar(
        select(Image)
        .join(Dataset, Dataset.id == Image.dataset_id)
        .where(
            Image.id == image_id,
            Dataset.owner_id == owner_id,
        )
    )
    if image is None:
        raise HTTPException(status_code=404, detail="이미지를 찾을 수 없습니다.")
    return image


@router.get("/{image_id}/annotations", response_model=AnnotationRead)
async def get_annotations(
    image_id: int,
    session: Session,
    current_user: CurrentUserDep,
) -> AnnotationRead:
    image = await _image_or_404(session, image_id, current_user.id)
    rows = (
        await session.scalars(
            select(Annotation)
            .where(Annotation.image_id == image_id)
            .order_by(Annotation.id)
        )
    ).all()
    return AnnotationRead(
        image_id=image.id,
        width=image.width,
        height=image.height,
        boxes=[BoxRow.model_validate(row) for row in rows],
    )


@router.put("/{image_id}/annotations", response_model=AnnotationSaved)
async def replace_annotations(
    image_id: int,
    body: AnnotationWrite,
    session: Session,
    current_user: CurrentUserDep,
) -> AnnotationSaved:
    locked = (
        await session.execute(
            select(Dataset, Image)
            .join(Image, Image.dataset_id == Dataset.id)
            .where(
                Image.id == image_id,
                Dataset.owner_id == current_user.id,
            )
            .with_for_update()
        )
    ).one_or_none()
    if locked is None:
        raise HTTPException(status_code=404, detail="이미지를 찾을 수 없습니다.")
    dataset, image = locked
    old_count = image.box_count

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
    dataset.annotation_count += len(rows) - old_count
    await session.flush()
    await session.commit()
    return AnnotationSaved(
        image_id=image.id,
        boxes=[BoxRow.model_validate(row) for row in rows],
        is_modified=image.is_modified,
    )
