"""Create an ordinary dataset snapshot containing selected project classes."""

from __future__ import annotations

import shutil
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.models import (
    Annotation,
    Dataset,
    DatasetClass,
    Image,
    Project,
    ProjectClass,
    UploadJob,
)
from app.services.dataset_merge import (
    DatasetMergeConflict,
    copy_source_images,
)
from app.services.image_names import PairKey
from app.services.quota import increase_bytes_used, quota_status
from app.services.storage import create_dataset_storage, storage_relative_path


class DatasetExtractConflict(ValueError):
    pass


class DatasetExtractNotFound(LookupError):
    pass


class DatasetExtractQuotaExceeded(ValueError):
    pass


@dataclass(frozen=True)
class DatasetExtractResult:
    dataset: Dataset


async def extract_dataset_snapshot(
    settings: Settings,
    session: AsyncSession,
    *,
    name: str,
    dataset_ids: list[int],
    class_ids: list[int],
    owner_id: int,
) -> DatasetExtractResult:
    source_rows = list(
        (
            await session.scalars(
                select(Dataset)
                .where(
                    Dataset.id.in_(dataset_ids),
                    Dataset.owner_id == owner_id,
                    Dataset.is_placeholder.is_(False),
                )
                .order_by(Dataset.id)
                .with_for_update()
            )
        ).all()
    )
    source_by_id = {dataset.id: dataset for dataset in source_rows}
    if len(source_by_id) != len(dataset_ids):
        raise DatasetExtractNotFound("선택한 데이터셋을 찾을 수 없습니다.")
    sources = [source_by_id[dataset_id] for dataset_id in dataset_ids]
    if any(dataset.status != "ready" for dataset in sources):
        raise DatasetExtractConflict(
            "사용 가능한 데이터셋에서만 클래스를 추출할 수 있습니다."
        )

    project_ids = {dataset.project_id for dataset in sources}
    if len(project_ids) != 1:
        raise DatasetExtractConflict(
            "같은 프로젝트의 데이터셋에서만 클래스를 추출할 수 있습니다."
        )
    project_id = sources[0].project_id
    project = await session.scalar(
        select(Project)
        .where(Project.id == project_id, Project.owner_id == owner_id)
        .with_for_update()
    )
    if project is None:
        raise DatasetExtractNotFound("프로젝트를 찾을 수 없습니다.")

    active_job = await session.scalar(
        select(UploadJob.id)
        .where(
            UploadJob.dataset_id.in_(dataset_ids),
            UploadJob.state.in_(
                ("queued", "running", "awaiting_class_resolution")
            ),
        )
        .limit(1)
    )
    if active_job is not None:
        raise DatasetExtractConflict(
            "업로드 처리 중인 데이터셋에서는 클래스를 추출할 수 없습니다."
        )

    project_class_rows = list(
        (
            await session.scalars(
                select(ProjectClass)
                .where(ProjectClass.project_id == project_id)
                .order_by(ProjectClass.class_id)
                .with_for_update()
            )
        ).all()
    )
    project_class_ids = {row.class_id for row in project_class_rows}
    if not set(class_ids).issubset(project_class_ids):
        raise DatasetExtractNotFound(
            "선택한 프로젝트 클래스를 찾을 수 없습니다."
        )
    selected_class_ids = frozenset(class_ids)
    selected_project_class_rows = [
        row
        for row in project_class_rows
        if row.class_id in selected_class_ids
    ]

    image_rows = list(
        (
            await session.scalars(
                select(Image)
                .options(
                    selectinload(Image.annotations),
                    selectinload(Image.media_object),
                )
                .where(
                    Image.dataset_id.in_(dataset_ids),
                    Image.annotations.any(
                        Annotation.class_id.in_(class_ids)
                    ),
                )
                .order_by(Image.dataset_id, Image.id)
            )
        ).all()
    )
    if not image_rows:
        raise DatasetExtractConflict(
            "선택한 클래스 annotation이 있는 이미지가 없습니다."
        )

    images_by_dataset: dict[int, list[Image]] = defaultdict(list)
    class_remap: dict[tuple[int, int], int] = {}
    for image in image_rows:
        images_by_dataset[image.dataset_id].append(image)
        for annotation in image.annotations:
            class_remap[(image.dataset_id, annotation.class_id)] = (
                annotation.class_id
            )

    reserved_keys: set[PairKey] = {
        (image.split, image.stem) for image in image_rows
    }
    dataset = Dataset(
        owner_id=owner_id,
        project_id=project_id,
        name=name,
        status="pending",
        storage_path="",
        is_merged=False,
        is_extracted=True,
    )
    session.add(dataset)
    storage_path: Path | None = None
    try:
        await session.flush()
        storage_path = create_dataset_storage(settings.storage_dir, dataset.id)
        dataset.storage_path = storage_relative_path(
            settings.storage_dir,
            storage_path,
        )
        try:
            copied = copy_source_images(
                settings,
                target_dataset_id=dataset.id,
                owner_id=owner_id,
                target_storage=storage_path,
                sources=sources,
                images_by_dataset=images_by_dataset,
                class_remap=class_remap,
                occupied_keys=set(),
                reserved_keys=reserved_keys,
                included_class_ids=selected_class_ids,
            )
        except DatasetMergeConflict as error:
            raise DatasetExtractConflict(
                "추출할 이미지의 파일명이 올바르지 않습니다."
            ) from error

        # Shared existing media is attached to transient Image rows here. Keep
        # the quota read from autoflushing those rows before add_all below.
        with session.no_autoflush:
            quota = await quota_status(
                session,
                owner_id,
                limit_bytes=settings.quota_bytes_per_user,
                required_bytes=copied.accounted_bytes,
            )
        if not quota.allowed:
            raise DatasetExtractQuotaExceeded(quota.detail)

        session.add_all(
            [
                DatasetClass(
                    dataset_id=dataset.id,
                    class_id=project_class.class_id,
                    name=project_class.name,
                )
                for project_class in selected_project_class_rows
            ]
        )
        session.add_all(copied.images)
        dataset.image_count = len(copied.images)
        dataset.annotation_count = copied.annotation_count
        dataset.class_count = len(selected_project_class_rows)
        dataset.status = "ready"
        project.updated_at = func.now()
        await increase_bytes_used(session, owner_id, copied.accounted_bytes)
        await session.commit()
        await session.refresh(dataset)
        return DatasetExtractResult(dataset=dataset)
    except Exception:
        await session.rollback()
        if storage_path is not None and storage_path.is_dir():
            shutil.rmtree(storage_path)
        raise
