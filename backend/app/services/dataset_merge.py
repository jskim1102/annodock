"""Create an independent dataset snapshot from multiple source datasets."""

from __future__ import annotations

import asyncio
import os
import shutil
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.class_colors import class_color
from app.config import Settings
from app.models import (
    Annotation,
    Dataset,
    DatasetClass,
    DatasetMergeSource,
    Image,
    Project,
    ProjectClass,
    TrainingRun,
    UploadJob,
)
from app.services.image_names import (
    PairKey,
    available_pair_key,
    replace_filename_stem,
)
from app.services.quota import (
    dataset_accounted_bytes,
    decrease_bytes_used,
    increase_bytes_used,
)
from app.services.storage import (
    contained_storage_path,
    create_dataset_storage,
    finalize_staged_deletions,
    restore_staged_deletions,
    stage_deletions_async,
    storage_relative_path,
)


class DatasetMergeConflict(ValueError):
    def __init__(self, detail: str | dict[str, Any]) -> None:
        super().__init__(detail if isinstance(detail, str) else detail["code"])
        self.detail = detail


class DatasetMergeNotFound(LookupError):
    pass


@dataclass(frozen=True)
class DatasetMergeResult:
    dataset: Dataset
    sources: list[Dataset]
    reused: bool = False


@dataclass(frozen=True)
class _CopiedImages:
    images: list[Image]
    annotation_count: int
    accounted_bytes: int
    created_files: list[Path]


def _link_or_copy(source: Path, target: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"병합할 이미지 파일이 없습니다: {source.name}")
    if target.exists():
        raise FileExistsError(f"병합 대상 파일이 이미 있습니다: {target.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        try:
            shutil.copy2(source, target)
        except Exception:
            target.unlink(missing_ok=True)
            raise


def _safe_filename(value: str) -> str:
    filename = Path(value).name
    if (
        not filename
        or filename in {".", ".."}
        or filename != value
        or "\x00" in filename
    ):
        raise DatasetMergeConflict("병합할 이미지의 파일명이 올바르지 않습니다.")
    return filename


def copy_source_images(
    settings: Settings,
    *,
    target_dataset_id: int,
    target_storage: Path,
    sources: list[Dataset],
    images_by_dataset: dict[int, list[Image]],
    class_remap: dict[tuple[int, int], int],
    occupied_keys: set[PairKey],
    reserved_keys: set[PairKey],
    created_files: list[Path] | None = None,
    included_class_ids: frozenset[int] | None = None,
) -> _CopiedImages:
    copied_images: list[Image] = []
    annotation_count = 0
    accounted_bytes = 0
    recorded_files = created_files if created_files is not None else []

    for source in sources:
        for source_image in images_by_dataset[source.id]:
            annotations = []
            for annotation in source_image.annotations:
                target_class_id = class_remap[
                    (source.id, annotation.class_id)
                ]
                if (
                    included_class_ids is not None
                    and target_class_id not in included_class_ids
                ):
                    continue
                annotations.append(
                    Annotation(
                        class_id=target_class_id,
                        cx=annotation.cx,
                        cy=annotation.cy,
                        w=annotation.w,
                        h=annotation.h,
                        serialized_bytes=annotation.serialized_bytes,
                    )
                )
            if included_class_ids is not None and not annotations:
                continue

            filename = _safe_filename(source_image.filename)
            key = (source_image.split, source_image.stem)
            storage_key = key
            if key in occupied_keys:
                storage_key = available_pair_key(
                    key,
                    occupied_keys | reserved_keys,
                    Path(filename).suffix,
                )
            occupied_keys.add(storage_key)
            stem = storage_key[1]
            merged_filename = replace_filename_stem(filename, stem)
            split_directory = (
                source_image.split
                if source_image.split in {"train", "val", "test"}
                else "unsplit"
            )
            original_target = (
                target_storage
                / "merged"
                / "original"
                / split_directory
                / merged_filename
            )
            thumb_target = (
                target_storage
                / "merged"
                / "thumbs"
                / split_directory
                / f"{stem}.jpg"
            )
            source_original = contained_storage_path(
                settings.storage_dir,
                source_image.file_path,
            )
            source_thumb = contained_storage_path(
                settings.storage_dir,
                source_image.thumb_path,
            )
            _link_or_copy(source_original, original_target)
            recorded_files.append(original_target)
            _link_or_copy(source_thumb, thumb_target)
            recorded_files.append(thumb_target)
            original_bytes = (
                source_image.original_bytes or source_original.stat().st_size
            )
            thumb_bytes = source_image.thumb_bytes or source_thumb.stat().st_size

            display_target: Path | None = None
            display_bytes = 0
            if source_image.display_path is not None:
                source_display = contained_storage_path(
                    settings.storage_dir,
                    source_image.display_path,
                )
                display_target = (
                    target_storage
                    / "merged"
                    / "display"
                    / split_directory
                    / f"{stem}.jpg"
                )
                _link_or_copy(source_display, display_target)
                recorded_files.append(display_target)
                display_bytes = (
                    source_image.display_bytes or source_display.stat().st_size
                )
            accounted_bytes += original_bytes + display_bytes + thumb_bytes

            annotation_count += len(annotations)
            copied_images.append(
                Image(
                    dataset_id=target_dataset_id,
                    stem=stem,
                    filename=merged_filename,
                    rel_path=f"images/{split_directory}/{merged_filename}",
                    split=source_image.split,
                    width=source_image.width,
                    height=source_image.height,
                    file_path=storage_relative_path(
                        settings.storage_dir,
                        original_target,
                    ),
                    display_path=(
                        storage_relative_path(
                            settings.storage_dir,
                            display_target,
                        )
                        if display_target
                        else None
                    ),
                    thumb_path=storage_relative_path(
                        settings.storage_dir,
                        thumb_target,
                    ),
                    original_bytes=original_bytes,
                    display_bytes=display_bytes,
                    thumb_bytes=thumb_bytes,
                    box_count=len(annotations),
                    has_label_source=source_image.has_label_source,
                    is_modified=source_image.is_modified,
                    annotations=annotations,
                )
            )

    return _CopiedImages(
        images=copied_images,
        annotation_count=annotation_count,
        accounted_bytes=accounted_bytes,
        created_files=recorded_files,
    )


def _remove_created_files(paths: list[Path], *, stop_at: Path) -> None:
    for path in reversed(paths):
        path.unlink(missing_ok=True)
        parent = path.parent
        while parent != stop_at and stop_at in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


async def merge_datasets(
    settings: Settings,
    session: AsyncSession,
    *,
    name: str,
    dataset_ids: list[int],
    owner_id: int,
) -> DatasetMergeResult:
    source_rows = (
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
    source_by_id = {dataset.id: dataset for dataset in source_rows}
    if len(source_by_id) != len(dataset_ids):
        raise DatasetMergeNotFound("선택한 데이터셋을 찾을 수 없습니다.")
    sources = [source_by_id[dataset_id] for dataset_id in dataset_ids]
    if any(dataset.status != "ready" for dataset in sources):
        raise DatasetMergeConflict("사용 가능한 데이터셋만 합칠 수 있습니다.")
    if any(dataset.is_merged for dataset in sources):
        raise DatasetMergeConflict("병합 데이터셋은 다시 합칠 수 없습니다.")
    project_ids = {dataset.project_id for dataset in sources}
    if len(project_ids) != 1:
        raise DatasetMergeConflict(
            "같은 프로젝트의 데이터셋만 합칠 수 있습니다."
        )
    project_id = sources[0].project_id
    project = await session.scalar(
        select(Project)
        .where(
            Project.id == project_id,
            Project.owner_id == owner_id,
        )
        .with_for_update()
    )
    if project is None:
        raise DatasetMergeNotFound("프로젝트를 찾을 수 없습니다.")

    overlapping_merge_ids = list(
        (
            await session.scalars(
                select(DatasetMergeSource.merged_dataset_id)
                .where(
                    DatasetMergeSource.source_dataset_id.in_(dataset_ids)
                )
                .distinct()
                .order_by(DatasetMergeSource.merged_dataset_id)
            )
        ).all()
    )
    if overlapping_merge_ids:
        existing_merges = {
            dataset.id: dataset
            for dataset in (
                await session.scalars(
                    select(Dataset)
                    .where(
                        Dataset.id.in_(overlapping_merge_ids),
                        Dataset.owner_id == owner_id,
                        Dataset.project_id == project_id,
                        Dataset.is_placeholder.is_(False),
                    )
                    .order_by(Dataset.id)
                )
            ).all()
        }
        membership_rows = (
            await session.execute(
                select(
                    DatasetMergeSource.merged_dataset_id,
                    DatasetMergeSource.source_dataset_id,
                )
                .where(
                    DatasetMergeSource.merged_dataset_id.in_(
                        overlapping_merge_ids
                    )
                )
                .order_by(
                    DatasetMergeSource.merged_dataset_id,
                    DatasetMergeSource.position,
                )
            )
        ).all()
        source_ids_by_merge: dict[int, list[int]] = defaultdict(list)
        for merged_dataset_id, source_dataset_id in membership_rows:
            source_ids_by_merge[merged_dataset_id].append(source_dataset_id)

        requested_source_ids = set(dataset_ids)
        for merged_dataset_id in overlapping_merge_ids:
            existing = existing_merges.get(merged_dataset_id)
            existing_source_ids = source_ids_by_merge[merged_dataset_id]
            if (
                existing is not None
                and set(existing_source_ids) == requested_source_ids
            ):
                return DatasetMergeResult(
                    dataset=existing,
                    sources=[
                        source_by_id[source_id]
                        for source_id in existing_source_ids
                    ],
                    reused=True,
                )

        conflict_id = next(
            (
                merged_dataset_id
                for merged_dataset_id in overlapping_merge_ids
                if merged_dataset_id in existing_merges
            ),
            None,
        )
        if conflict_id is not None:
            existing = existing_merges[conflict_id]
            raise DatasetMergeConflict(
                {
                    "code": "dataset_merge_source_overlap",
                    "merged_dataset": {
                        "id": existing.id,
                        "name": existing.name,
                        "source_dataset_ids": source_ids_by_merge[conflict_id],
                    },
                }
            )
        raise DatasetMergeConflict(
            "이미 다른 병합 데이터셋에 포함된 원본은 다시 합칠 수 없습니다."
        )

    merged_membership = await session.scalar(
        select(DatasetMergeSource.merged_dataset_id)
        .where(
            DatasetMergeSource.merged_dataset_id.in_(dataset_ids)
        )
        .limit(1)
    )
    if merged_membership is not None:
        raise DatasetMergeConflict(
            "이미 다른 병합 데이터셋에 포함된 원본은 다시 합칠 수 없습니다."
        )
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
        raise DatasetMergeConflict("업로드 처리 중인 데이터셋은 합칠 수 없습니다.")

    class_rows = (
        await session.scalars(
            select(DatasetClass)
            .where(DatasetClass.dataset_id.in_(dataset_ids))
            .order_by(DatasetClass.dataset_id, DatasetClass.class_id)
        )
    ).all()
    classes_by_dataset: dict[int, list[DatasetClass]] = defaultdict(list)
    for row in class_rows:
        classes_by_dataset[row.dataset_id].append(row)

    image_rows = (
        await session.scalars(
            select(Image)
            .options(selectinload(Image.annotations))
            .where(Image.dataset_id.in_(dataset_ids))
            .order_by(Image.dataset_id, Image.id)
        )
    ).all()
    images_by_dataset: dict[int, list[Image]] = defaultdict(list)
    for image in image_rows:
        images_by_dataset[image.dataset_id].append(image)

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
    source_class_name: dict[tuple[int, int], str] = {}

    for source in sources:
        for source_class in classes_by_dataset[source.id]:
            source_class_name[(source.id, source_class.class_id)] = (
                source_class.name
            )
        for image in images_by_dataset[source.id]:
            for annotation in image.annotations:
                key = (source.id, annotation.class_id)
                source_class_name.setdefault(
                    key,
                    f"class_{annotation.class_id}",
                )

    project_class_id_by_name = {
        row.name: row.class_id for row in project_class_rows
    }
    missing_names = sorted(
        set(source_class_name.values()) - set(project_class_id_by_name)
    )
    next_class_id = max(
        (row.class_id for row in project_class_rows),
        default=-1,
    ) + 1
    for class_name in missing_names:
        project_class = ProjectClass(
            project_id=project_id,
            class_id=next_class_id,
            name=class_name,
            color=class_color(next_class_id),
        )
        session.add(project_class)
        project_class_rows.append(project_class)
        project_class_id_by_name[class_name] = next_class_id
        next_class_id += 1
    if missing_names:
        project.updated_at = func.now()

    class_remap = {
        key: project_class_id_by_name[class_name]
        for key, class_name in source_class_name.items()
    }

    reserved_keys: set[PairKey] = {
        (image.split, image.stem) for image in image_rows
    }
    occupied_keys: set[PairKey] = set()
    dataset = Dataset(
        owner_id=owner_id,
        project_id=sources[0].project_id,
        name=name,
        status="pending",
        storage_path="",
        is_merged=True,
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
        copied = copy_source_images(
            settings,
            target_dataset_id=dataset.id,
            target_storage=storage_path,
            sources=sources,
            images_by_dataset=images_by_dataset,
            class_remap=class_remap,
            occupied_keys=occupied_keys,
            reserved_keys=reserved_keys,
        )

        session.add_all(
            [
                DatasetClass(
                    dataset_id=dataset.id,
                    class_id=project_class.class_id,
                    name=project_class.name,
                )
                for project_class in project_class_rows
            ]
        )
        session.add_all(copied.images)
        session.add_all(
            [
                DatasetMergeSource(
                    merged_dataset_id=dataset.id,
                    source_dataset_id=source.id,
                    position=position,
                )
                for position, source in enumerate(sources)
            ]
        )
        dataset.image_count = len(copied.images)
        dataset.annotation_count = copied.annotation_count
        dataset.class_count = len(project_class_rows)
        dataset.status = "ready" if copied.images else "failed"
        await increase_bytes_used(session, owner_id, copied.accounted_bytes)
        await session.commit()
        await session.refresh(dataset)
        return DatasetMergeResult(dataset=dataset, sources=sources)
    except Exception:
        await session.rollback()
        if storage_path is not None and storage_path.is_dir():
            shutil.rmtree(storage_path)
        raise


async def extend_merged_dataset(
    settings: Settings,
    session: AsyncSession,
    *,
    merged_dataset_id: int,
    dataset_ids: list[int],
    owner_id: int,
) -> DatasetMergeResult:
    requested_ids = [merged_dataset_id, *dataset_ids]
    dataset_rows = (
        await session.scalars(
            select(Dataset)
            .where(
                Dataset.id.in_(requested_ids),
                Dataset.owner_id == owner_id,
                Dataset.is_placeholder.is_(False),
            )
            .order_by(Dataset.id)
            .with_for_update()
        )
    ).all()
    dataset_by_id = {dataset.id: dataset for dataset in dataset_rows}
    if len(dataset_by_id) != len(set(requested_ids)):
        raise DatasetMergeNotFound("선택한 데이터셋을 찾을 수 없습니다.")
    target = dataset_by_id[merged_dataset_id]
    sources = [dataset_by_id[dataset_id] for dataset_id in dataset_ids]
    if merged_dataset_id in dataset_ids:
        raise DatasetMergeConflict("대상 병합 데이터셋은 추가 원본이 될 수 없습니다.")
    if not target.is_merged:
        raise DatasetMergeConflict("병합 데이터셋만 확장할 수 있습니다.")
    if target.status != "ready" or any(source.status != "ready" for source in sources):
        raise DatasetMergeConflict("사용 가능한 데이터셋만 합칠 수 있습니다.")
    if any(source.project_id != target.project_id for source in sources):
        raise DatasetMergeConflict("같은 프로젝트의 데이터셋만 합칠 수 있습니다.")

    project = await session.scalar(
        select(Project)
        .where(
            Project.id == target.project_id,
            Project.owner_id == owner_id,
        )
        .with_for_update()
    )
    if project is None:
        raise DatasetMergeNotFound("프로젝트를 찾을 수 없습니다.")

    target_memberships = list(
        (
            await session.execute(
                select(
                    DatasetMergeSource.source_dataset_id,
                    DatasetMergeSource.position,
                )
                .where(DatasetMergeSource.merged_dataset_id == target.id)
                .order_by(DatasetMergeSource.position)
                .with_for_update()
            )
        ).all()
    )
    if not target_memberships:
        raise DatasetMergeConflict(
            "원본 정보가 없는 병합 데이터셋은 확장할 수 없습니다."
        )

    losing_merges = [source for source in sources if source.is_merged]
    losing_ids = [dataset.id for dataset in losing_merges]
    losing_memberships = list(
        (
            await session.execute(
                select(
                    DatasetMergeSource.merged_dataset_id,
                    DatasetMergeSource.source_dataset_id,
                    DatasetMergeSource.position,
                )
                .where(DatasetMergeSource.merged_dataset_id.in_(losing_ids))
                .order_by(
                    DatasetMergeSource.merged_dataset_id,
                    DatasetMergeSource.position,
                )
                .with_for_update()
            )
        ).all()
    ) if losing_ids else []
    memberships_by_merge: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for losing_id, source_id, position in losing_memberships:
        memberships_by_merge[losing_id].append((source_id, position))
    if any(not memberships_by_merge[dataset.id] for dataset in losing_merges):
        raise DatasetMergeConflict(
            "원본 정보가 없는 병합 데이터셋은 통합할 수 없습니다."
        )

    linked_source_rows = (
        await session.execute(
            select(
                DatasetMergeSource.source_dataset_id,
                DatasetMergeSource.merged_dataset_id,
            ).where(
                DatasetMergeSource.source_dataset_id.in_(
                    [source.id for source in sources]
                )
            )
        )
    ).all()
    if linked_source_rows:
        linked_id, linked_merge_id = linked_source_rows[0]
        if linked_merge_id == target.id:
            raise DatasetMergeConflict(
                "이미 대상 병합 데이터셋에 포함된 원본입니다."
            )
        raise DatasetMergeConflict(
            "다른 병합 데이터셋에 포함된 원본은 해당 병합 데이터셋을 선택해 통합하세요."
        )

    leaf_source_ids = [
        source_id for source_id, _position in target_memberships
    ]
    for source in sources:
        if source.is_merged:
            leaf_source_ids.extend(
                source_id for source_id, _position in memberships_by_merge[source.id]
            )
        else:
            leaf_source_ids.append(source.id)
    leaf_rows = (
        await session.scalars(
            select(Dataset)
            .where(
                Dataset.id.in_(leaf_source_ids),
                Dataset.owner_id == owner_id,
                Dataset.project_id == target.project_id,
                Dataset.is_placeholder.is_(False),
            )
        )
    ).all()
    leaf_by_id = {dataset.id: dataset for dataset in leaf_rows}
    if len(leaf_by_id) != len(set(leaf_source_ids)):
        raise DatasetMergeNotFound("병합 원본 데이터셋을 찾을 수 없습니다.")
    result_sources = [leaf_by_id[dataset_id] for dataset_id in leaf_source_ids]

    active_dataset_ids = list({target.id, *dataset_ids, *leaf_source_ids})
    active_job = await session.scalar(
        select(UploadJob.id)
        .where(
            UploadJob.dataset_id.in_(active_dataset_ids),
            UploadJob.state.in_(
                ("queued", "running", "awaiting_class_resolution")
            ),
        )
        .limit(1)
    )
    if active_job is not None:
        raise DatasetMergeConflict("업로드 처리 중인 데이터셋은 합칠 수 없습니다.")
    active_run = await session.scalar(
        select(TrainingRun.id)
        .where(
            TrainingRun.dataset_id.in_(losing_ids),
            TrainingRun.owner_id == owner_id,
            TrainingRun.state.in_(("running", "canceling")),
        )
        .limit(1)
    ) if losing_ids else None
    if active_run is not None:
        raise DatasetMergeConflict(
            "진행 중인 학습이 참조하는 병합 데이터셋은 통합할 수 없습니다."
        )

    incoming_ids = [source.id for source in sources]
    class_rows = (
        await session.scalars(
            select(DatasetClass)
            .where(DatasetClass.dataset_id.in_(incoming_ids))
            .order_by(DatasetClass.dataset_id, DatasetClass.class_id)
        )
    ).all()
    classes_by_dataset: dict[int, list[DatasetClass]] = defaultdict(list)
    for row in class_rows:
        classes_by_dataset[row.dataset_id].append(row)
    image_rows = (
        await session.scalars(
            select(Image)
            .options(selectinload(Image.annotations))
            .where(Image.dataset_id.in_(incoming_ids))
            .order_by(Image.dataset_id, Image.id)
        )
    ).all()
    images_by_dataset: dict[int, list[Image]] = defaultdict(list)
    for image in image_rows:
        images_by_dataset[image.dataset_id].append(image)

    project_class_rows = list(
        (
            await session.scalars(
                select(ProjectClass)
                .where(ProjectClass.project_id == target.project_id)
                .order_by(ProjectClass.class_id)
                .with_for_update()
            )
        ).all()
    )
    source_class_name: dict[tuple[int, int], str] = {}
    for source in sources:
        for source_class in classes_by_dataset[source.id]:
            source_class_name[(source.id, source_class.class_id)] = source_class.name
        for image in images_by_dataset[source.id]:
            for annotation in image.annotations:
                source_class_name.setdefault(
                    (source.id, annotation.class_id),
                    f"class_{annotation.class_id}",
                )

    project_class_id_by_name = {
        row.name: row.class_id for row in project_class_rows
    }
    missing_names = sorted(
        set(source_class_name.values()) - set(project_class_id_by_name)
    )
    next_class_id = max(
        (row.class_id for row in project_class_rows),
        default=-1,
    ) + 1
    for class_name in missing_names:
        project_class = ProjectClass(
            project_id=target.project_id,
            class_id=next_class_id,
            name=class_name,
            color=class_color(next_class_id),
        )
        session.add(project_class)
        project_class_rows.append(project_class)
        project_class_id_by_name[class_name] = next_class_id
        next_class_id += 1
    class_remap = {
        key: project_class_id_by_name[class_name]
        for key, class_name in source_class_name.items()
    }

    existing_keys = set(
        (
            await session.execute(
                select(Image.split, Image.stem).where(
                    Image.dataset_id == target.id
                )
            )
        ).all()
    )
    reserved_keys = existing_keys | {
        (image.split, image.stem) for image in image_rows
    }
    target_storage = contained_storage_path(
        settings.storage_dir,
        target.storage_path,
    )
    if not target_storage.is_dir():
        raise DatasetMergeConflict("대상 병합 데이터셋의 저장소가 없습니다.")

    copied_files: list[Path] = []
    staged_deletions = []
    try:
        copied = copy_source_images(
            settings,
            target_dataset_id=target.id,
            target_storage=target_storage,
            sources=sources,
            images_by_dataset=images_by_dataset,
            class_remap=class_remap,
            occupied_keys=set(existing_keys),
            reserved_keys=reserved_keys,
            created_files=copied_files,
        )
        session.add_all(copied.images)

        target_class_ids = set(
            (
                await session.scalars(
                    select(DatasetClass.class_id).where(
                        DatasetClass.dataset_id == target.id
                    )
                )
            ).all()
        )
        session.add_all(
            [
                DatasetClass(
                    dataset_id=target.id,
                    class_id=project_class.class_id,
                    name=project_class.name,
                )
                for project_class in project_class_rows
                if project_class.class_id not in target_class_ids
            ]
        )

        next_position = max(
            (position for _source_id, position in target_memberships),
            default=-1,
        ) + 1
        for source in sources:
            if source.is_merged:
                for source_id, _position in memberships_by_merge[source.id]:
                    await session.execute(
                        update(DatasetMergeSource)
                        .where(
                            DatasetMergeSource.merged_dataset_id == source.id,
                            DatasetMergeSource.source_dataset_id == source_id,
                        )
                        .values(
                            merged_dataset_id=target.id,
                            position=next_position,
                        )
                    )
                    next_position += 1
            else:
                session.add(
                    DatasetMergeSource(
                        merged_dataset_id=target.id,
                        source_dataset_id=source.id,
                        position=next_position,
                    )
                )
                next_position += 1

        losing_accounted_bytes = 0
        for losing_merge in losing_merges:
            losing_accounted_bytes += await dataset_accounted_bytes(
                session,
                losing_merge.id,
            )

        staged_deletions = await stage_deletions_async(
            settings.storage_dir,
            [losing_merge.storage_path for losing_merge in losing_merges],
        )
        for losing_merge in losing_merges:
            await session.delete(losing_merge)

        target.image_count += len(copied.images)
        target.annotation_count += copied.annotation_count
        target.class_count = len(project_class_rows)
        project.updated_at = func.now()
        quota_delta = copied.accounted_bytes - losing_accounted_bytes
        if quota_delta > 0:
            await increase_bytes_used(session, owner_id, quota_delta)
        elif quota_delta < 0:
            await decrease_bytes_used(session, owner_id, -quota_delta)
        await session.commit()
        await session.refresh(target)
    except BaseException as error:
        try:
            restored = restore_staged_deletions(reversed(staged_deletions))
            if not restored:
                error.add_note("one or more merged dataset paths could not be restored")
        except BaseException as restore_error:
            error.add_note(
                "merged dataset restore also failed: "
                f"{type(restore_error).__name__}"
            )
        try:
            _remove_created_files(copied_files, stop_at=target_storage)
        except BaseException as cleanup_error:
            error.add_note(
                "merged dataset copied-file cleanup also failed: "
                f"{type(cleanup_error).__name__}"
            )
        try:
            await session.rollback()
        except BaseException as rollback_error:
            error.add_note(
                "merged dataset rollback also failed: "
                f"{type(rollback_error).__name__}"
            )
        raise

    await asyncio.to_thread(finalize_staged_deletions, staged_deletions)
    return DatasetMergeResult(dataset=target, sources=result_sources)
