"""Atomically consolidate projects without copying their dataset media."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import case, delete, func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import is_lock_not_available
from app.models import (
    Annotation,
    Dataset,
    DatasetClass,
    Image,
    Project,
    ProjectClass,
    UploadBatch,
    UploadJob,
    UploadSession,
)


class ProjectMergeConflict(ValueError):
    pass


class ProjectMergeNotFound(LookupError):
    pass


@dataclass(frozen=True)
class ProjectMergeClassSource:
    project_id: int
    class_id: int


@dataclass(frozen=True)
class ProjectMergeClassPlan:
    name: str
    color: str
    sources: tuple[ProjectMergeClassSource, ...]


@dataclass(frozen=True)
class ProjectMergeResult:
    project_id: int


ACTIVE_UPLOAD_JOB_STATES = ("queued", "running", "awaiting_class_resolution")
ACTIVE_UPLOAD_SESSION_STATES = ("open", "pending")


async def _has_active_upload(
    session: AsyncSession,
    *,
    project_ids: list[int],
    owner_id: int,
) -> bool:
    dataset_ids = select(Dataset.id).where(
        Dataset.project_id.in_(project_ids),
        Dataset.owner_id == owner_id,
    )
    active_job = await session.scalar(
        select(UploadJob.id)
        .where(
            UploadJob.dataset_id.in_(dataset_ids),
            UploadJob.state.in_(ACTIVE_UPLOAD_JOB_STATES),
        )
        .limit(1)
    )
    if active_job is not None:
        return True
    open_session = await session.scalar(
        select(UploadSession.id)
        .where(
            UploadSession.dataset_id.in_(dataset_ids),
            UploadSession.state.in_(ACTIVE_UPLOAD_SESSION_STATES),
        )
        .limit(1)
    )
    if open_session is not None:
        return True
    open_batch = await session.scalar(
        select(UploadBatch.id)
        .where(
            UploadBatch.dataset_id.in_(dataset_ids),
            UploadBatch.state == "open",
        )
        .limit(1)
    )
    return open_batch is not None


def _busy_conflict(error: DBAPIError) -> ProjectMergeConflict | None:
    if not is_lock_not_available(error):
        return None
    return ProjectMergeConflict(
        "다른 데이터 작업이 진행 중입니다. 작업이 끝난 뒤 다시 시도해주세요."
    )


async def _merge_projects(
    session: AsyncSession,
    *,
    name: str,
    project_revisions: dict[int, datetime],
    classes: tuple[ProjectMergeClassPlan, ...],
    owner_id: int,
) -> ProjectMergeResult:
    project_ids = list(project_revisions)
    if await _has_active_upload(
        session,
        project_ids=project_ids,
        owner_id=owner_id,
    ):
        raise ProjectMergeConflict(
            "업로드 중이거나 처리 중인 프로젝트는 통합할 수 없습니다."
        )

    try:
        projects = list(
            (
                await session.scalars(
                    select(Project)
                    .where(
                        Project.id.in_(project_ids),
                        Project.owner_id == owner_id,
                    )
                    .order_by(Project.id)
                    .with_for_update(nowait=True)
                )
            ).all()
        )
    except DBAPIError as error:
        conflict = _busy_conflict(error)
        if conflict is not None:
            raise conflict from error
        raise
    if len(projects) != len(project_ids):
        raise ProjectMergeNotFound("선택한 프로젝트를 찾을 수 없습니다.")
    if any(
        project.updated_at != project_revisions[project.id]
        for project in projects
    ):
        raise ProjectMergeConflict(
            "프로젝트가 변경되었습니다. 목록을 새로고침한 뒤 다시 통합해주세요."
        )

    conflicting_name = await session.scalar(
        select(Project.id)
        .where(
            Project.owner_id == owner_id,
            Project.name == name,
            Project.id.not_in(project_ids),
        )
        .limit(1)
    )
    if conflicting_name is not None:
        raise ProjectMergeConflict("이미 있는 프로젝트 이름입니다.")

    try:
        datasets = list(
            (
                await session.scalars(
                    select(Dataset)
                    .where(
                        Dataset.project_id.in_(project_ids),
                        Dataset.owner_id == owner_id,
                    )
                    .order_by(Dataset.id)
                    .with_for_update(nowait=True)
                )
            ).all()
        )
    except DBAPIError as error:
        conflict = _busy_conflict(error)
        if conflict is not None:
            raise conflict from error
        raise

    # Close the race with an upload created between the preflight and locks.
    if await _has_active_upload(
        session,
        project_ids=project_ids,
        owner_id=owner_id,
    ):
        raise ProjectMergeConflict(
            "업로드 중이거나 처리 중인 프로젝트는 통합할 수 없습니다."
        )

    source_class_rows = (
        await session.execute(
            select(
                ProjectClass.project_id,
                ProjectClass.class_id,
                ProjectClass.name,
            )
            .where(ProjectClass.project_id.in_(project_ids))
            .order_by(ProjectClass.project_id, ProjectClass.class_id)
            .with_for_update(nowait=True)
        )
    ).all()
    source_class_keys = {
        (project_id, class_id)
        for project_id, class_id, _class_name in source_class_rows
    }
    class_remap: dict[tuple[int, int], int] = {}
    for target_class_id, target in enumerate(classes):
        for source in target.sources:
            key = (source.project_id, source.class_id)
            if key in class_remap:
                raise ProjectMergeConflict(
                    "한 원본 클래스는 결과 클래스에 한 번만 포함할 수 있습니다."
                )
            class_remap[key] = target_class_id
    if set(class_remap) != source_class_keys:
        raise ProjectMergeConflict(
            "모든 원본 클래스를 결과 클래스에 정확히 한 번씩 포함해주세요."
        )

    dataset_ids = [dataset.id for dataset in datasets]
    dataset_by_id = {dataset.id: dataset for dataset in datasets}
    dataset_class_rows = (
        (
            await session.execute(
                select(DatasetClass.dataset_id, DatasetClass.class_id)
                .where(DatasetClass.dataset_id.in_(dataset_ids))
                .order_by(DatasetClass.dataset_id, DatasetClass.class_id)
                .with_for_update(nowait=True)
            )
        ).all()
        if dataset_ids
        else []
    )
    dataset_source_memberships = {
        (dataset_id, class_id)
        for dataset_id, class_id in dataset_class_rows
    }
    for dataset_id, class_id in dataset_class_rows:
        dataset = dataset_by_id[dataset_id]
        if (dataset.project_id, class_id) not in class_remap:
            raise ProjectMergeConflict(
                "데이터셋 클래스 정보가 프로젝트 클래스와 일치하지 않습니다."
            )

    annotation_rows = (
        (
            await session.execute(
                select(
                    Dataset.id,
                    Dataset.project_id,
                    Annotation.class_id,
                )
                .select_from(Annotation)
                .join(Image, Image.id == Annotation.image_id)
                .join(Dataset, Dataset.id == Image.dataset_id)
                .where(Dataset.id.in_(dataset_ids))
                .distinct()
            )
        ).all()
        if dataset_ids
        else []
    )
    for dataset_id, project_id, class_id in annotation_rows:
        if (
            (project_id, class_id) not in class_remap
            or (dataset_id, class_id) not in dataset_source_memberships
        ):
            raise ProjectMergeConflict(
                "어노테이션 클래스 정보가 데이터셋과 일치하지 않습니다."
            )

    target = Project(
        owner_id=owner_id,
        name=f"__project_merge_{uuid4().hex}",
    )
    session.add(target)
    await session.flush()
    session.add_all(
        [
            ProjectClass(
                project_id=target.id,
                class_id=class_id,
                name=item.name,
                color=item.color,
            )
            for class_id, item in enumerate(classes)
        ]
    )

    dataset_ids_by_project: dict[int, list[int]] = {
        project_id: [] for project_id in project_ids
    }
    for dataset in datasets:
        dataset_ids_by_project[dataset.project_id].append(dataset.id)
    for project_id, scoped_dataset_ids in dataset_ids_by_project.items():
        if not scoped_dataset_ids:
            continue
        scoped_remap = {
            old_class_id: new_class_id
            for (source_project_id, old_class_id), new_class_id in class_remap.items()
            if source_project_id == project_id
        }
        if not scoped_remap:
            continue
        await session.execute(
            Annotation.__table__.update()
            .where(
                Annotation.image_id.in_(
                    select(Image.id).where(
                        Image.dataset_id.in_(scoped_dataset_ids)
                    )
                ),
                Annotation.class_id.in_(scoped_remap),
            )
            .values(
                class_id=case(
                    *(
                        (Annotation.class_id == old_class_id, new_class_id)
                        for old_class_id, new_class_id in scoped_remap.items()
                    ),
                    else_=Annotation.class_id,
                ),
                updated_at=func.now(),
            )
        )

    target_name_by_id = {
        target_class_id: item.name
        for target_class_id, item in enumerate(classes)
    }
    remapped_memberships: set[tuple[int, int]] = set()
    for dataset_id, old_class_id in dataset_class_rows:
        source_project_id = dataset_by_id[dataset_id].project_id
        remapped_memberships.add(
            (dataset_id, class_remap[(source_project_id, old_class_id)])
        )
    if dataset_ids:
        await session.execute(
            delete(DatasetClass).where(DatasetClass.dataset_id.in_(dataset_ids))
        )
    session.add_all(
        [
            DatasetClass(
                dataset_id=dataset_id,
                class_id=class_id,
                name=target_name_by_id[class_id],
            )
            for dataset_id, class_id in sorted(remapped_memberships)
        ]
    )
    class_count_by_dataset = {dataset_id: 0 for dataset_id in dataset_ids}
    for dataset_id, _class_id in remapped_memberships:
        class_count_by_dataset[dataset_id] += 1
    for dataset in datasets:
        dataset.project_id = target.id
        dataset.class_count = class_count_by_dataset[dataset.id]

    await session.flush()
    await session.execute(
        delete(Project)
        .where(Project.id.in_(project_ids), Project.owner_id == owner_id)
        .execution_options(synchronize_session=False)
    )
    target.name = name
    await session.commit()
    return ProjectMergeResult(project_id=target.id)


async def merge_projects(
    session: AsyncSession,
    *,
    name: str,
    project_revisions: dict[int, datetime],
    classes: tuple[ProjectMergeClassPlan, ...],
    owner_id: int,
) -> ProjectMergeResult:
    """Consolidate source containers and preserve every dataset/media identity."""

    try:
        return await _merge_projects(
            session,
            name=name,
            project_revisions=project_revisions,
            classes=classes,
            owner_id=owner_id,
        )
    except Exception:
        await session.rollback()
        raise
