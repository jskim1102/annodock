"""Paginated image metadata and storage-bounded image serving."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import CurrentUserDep
from app.models import Dataset, Image
from app.services.storage import StorageBoundaryError, contained_storage_path


router = APIRouter(prefix="/api", tags=["images"])
Session = Annotated[AsyncSession, Depends(get_session)]

ORIGINAL_MEDIA_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}
THUMB_CACHE_CONTROL = "public, max-age=31536000, immutable"


class ImageRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    stem: str
    filename: str
    split: str | None
    width: int
    height: int
    box_count: int
    is_modified: bool


class ImagePage(BaseModel):
    items: list[ImageRow]
    total: int


async def _dataset_or_404(
    session: AsyncSession,
    dataset_id: int,
    owner_id: int,
) -> Dataset:
    dataset = await session.scalar(
        select(Dataset).where(
            Dataset.id == dataset_id,
            Dataset.owner_id == owner_id,
        )
    )
    if dataset is None:
        raise HTTPException(status_code=404, detail="데이터셋을 찾을 수 없습니다.")
    return dataset


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


def _stored_file_or_404(request: Request, stored_path: str) -> Path:
    try:
        path = contained_storage_path(
            request.app.state.settings.storage_dir,
            stored_path,
        )
    except StorageBoundaryError as error:
        raise HTTPException(
            status_code=404,
            detail="이미지 파일을 찾을 수 없습니다.",
        ) from error
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail="이미지 파일을 찾을 수 없습니다.",
        )
    return path


@router.get("/datasets/{dataset_id}/images", response_model=ImagePage)
async def list_images(
    dataset_id: int,
    session: Session,
    current_user: CurrentUserDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
    split: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
) -> ImagePage:
    await _dataset_or_404(session, dataset_id, current_user.id)
    filters = [Image.dataset_id == dataset_id]
    if split is not None:
        filters.append(Image.split == split)

    items = (
        await session.scalars(
            select(Image)
            .where(*filters)
            .order_by(
                Image.stem,
                Image.split.asc().nullsfirst(),
                Image.id,
            )
            .offset(offset)
            .limit(limit)
        )
    ).all()
    total = await session.scalar(
        select(func.count(Image.id)).where(*filters)
    )
    return ImagePage(
        items=[ImageRow.model_validate(image) for image in items],
        total=total or 0,
    )


@router.get("/images/{image_id}/file")
async def get_image_file(
    image_id: int,
    request: Request,
    session: Session,
    current_user: CurrentUserDep,
) -> FileResponse:
    image = await _image_or_404(session, image_id, current_user.id)
    if image.display_path is not None:
        path = _stored_file_or_404(request, image.display_path)
        media_type = "image/jpeg"
    else:
        path = _stored_file_or_404(request, image.file_path)
        extension = Path(image.filename).suffix.removeprefix(".").lower()
        media_type = ORIGINAL_MEDIA_TYPES.get(
            extension,
            "application/octet-stream",
        )
    return FileResponse(path, media_type=media_type)


@router.get("/images/{image_id}/thumb")
async def get_image_thumbnail(
    image_id: int,
    request: Request,
    session: Session,
    current_user: CurrentUserDep,
) -> FileResponse:
    image = await _image_or_404(session, image_id, current_user.id)
    path = _stored_file_or_404(request, image.thumb_path)
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": THUMB_CACHE_CONTROL},
    )
