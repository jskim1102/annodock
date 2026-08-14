"""Owner-scoped project hierarchy and project-level class catalogs."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import CurrentUserDep
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
from app.services.cleanup import stage_training_run_deletion
from app.services.quota import (
    dataset_accounted_bytes,
    decrease_bytes_used,
    path_tree_bytes,
)
from app.services.storage import (
    StorageBoundaryError,
    contained_storage_path,
    finalize_staged_deletion,
    restore_staged_deletion,
    stage_dataset_deletion,
)


router = APIRouter(prefix="/api/projects", tags=["projects"])
Session = Annotated[AsyncSession, Depends(get_session)]
DatasetStatus = Literal["pending", "processing", "ready", "failed"]


class ProjectClassCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        if "\x00" in normalized:
            raise ValueError("name must not contain null bytes")
        return normalized

    @field_validator("color")
    @classmethod
    def normalize_color(cls, value: str) -> str:
        return value.upper()


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    classes: list[ProjectClassCreate] = Field(default_factory=list, max_length=200)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        if "\x00" in normalized:
            raise ValueError("name must not contain null bytes")
        return normalized

    @model_validator(mode="after")
    def reject_duplicate_classes(self) -> ProjectCreate:
        names = [item.name for item in self.classes]
        if len(names) != len(set(names)):
            raise ValueError("class names must be unique")
        return self


class ProjectName(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        if "\x00" in normalized:
            raise ValueError("name must not contain null bytes")
        return normalized


class ProjectClassRow(BaseModel):
    class_id: int
    name: str
    color: str


class ActiveJobRow(BaseModel):
    job_id: int
    state: Literal["queued", "running", "awaiting_class_resolution"]
    phase: str
    total: int
    processed: int
    failed: int


class ProjectDatasetSourceRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    name: str
    image_count: int
    labeled_image_count: int
    annotation_count: int
    class_count: int
    created_at: datetime
    status: DatasetStatus
    is_merged: bool
    active_job: ActiveJobRow | None


class ProjectDatasetRow(ProjectDatasetSourceRow):
    source_datasets: list[ProjectDatasetSourceRow]


class ProjectRow(BaseModel):
    id: int
    name: str
    created_at: datetime
    updated_at: datetime
    archived: bool
    dataset_count: int
    image_count: int
    annotation_count: int
    class_count: int
    classes: list[ProjectClassRow]
    datasets: list[ProjectDatasetRow]


class ProjectPage(BaseModel):
    items: list[ProjectRow]
    total: int


class ProjectClassImageCountRow(ProjectClassRow):
    image_count: int


class ProjectClassImageCountPage(BaseModel):
    items: list[ProjectClassImageCountRow]


class ProjectRenamed(BaseModel):
    id: int
    name: str


class ProjectClassUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        if "\x00" in normalized:
            raise ValueError("name must not contain null bytes")
        return normalized

    @field_validator("color")
    @classmethod
    def normalize_color(cls, value: str | None) -> str | None:
        return value.upper() if value is not None else None

    @model_validator(mode="after")
    def require_any_field(self) -> ProjectClassUpdate:
        if self.name is None and self.color is None:
            raise ValueError("name 또는 color 중 하나는 필요합니다")
        return self


def _active_job(job: UploadJob | None) -> ActiveJobRow | None:
    if job is None:
        return None
    return ActiveJobRow(
        job_id=job.id,
        state=job.state,
        phase=job.phase,
        total=job.total,
        processed=job.processed,
        failed=job.failed,
    )


async def _project_rows(
    session: AsyncSession,
    owner_id: int,
    *,
    project_id: int | None = None,
) -> list[ProjectRow]:
    statement = select(Project).where(Project.owner_id == owner_id)
    if project_id is not None:
        statement = statement.where(Project.id == project_id)
    projects = (
        await session.scalars(
            statement.order_by(Project.created_at.desc(), Project.id.desc())
        )
    ).all()
    if not projects:
        return []

    project_ids = [project.id for project in projects]
    latest_active_job_id = (
        select(func.max(UploadJob.id))
        .where(
            UploadJob.dataset_id == Dataset.id,
            UploadJob.state.in_(
                ("queued", "running", "awaiting_class_resolution")
            ),
        )
        .correlate(Dataset)
        .scalar_subquery()
    )
    source_dataset_ids = select(DatasetMergeSource.source_dataset_id)
    dataset_rows = (
        await session.execute(
            select(Dataset, UploadJob)
            .outerjoin(UploadJob, UploadJob.id == latest_active_job_id)
            .where(
                Dataset.project_id.in_(project_ids),
                Dataset.owner_id == owner_id,
                Dataset.is_placeholder.is_(False),
                ~Dataset.id.in_(source_dataset_ids),
            )
            .order_by(Dataset.created_at.desc(), Dataset.id.desc())
        )
    ).all()
    visible_dataset_ids = [dataset.id for dataset, _ in dataset_rows]
    source_rows = (
        (
            await session.execute(
                select(
                    DatasetMergeSource.merged_dataset_id,
                    Dataset,
                    UploadJob,
                )
                .join(
                    Dataset,
                    Dataset.id == DatasetMergeSource.source_dataset_id,
                )
                .outerjoin(UploadJob, UploadJob.id == latest_active_job_id)
                .where(
                    DatasetMergeSource.merged_dataset_id.in_(
                        visible_dataset_ids
                    ),
                    Dataset.owner_id == owner_id,
                    Dataset.is_placeholder.is_(False),
                )
                .order_by(
                    DatasetMergeSource.merged_dataset_id,
                    DatasetMergeSource.position,
                )
            )
        ).all()
        if visible_dataset_ids
        else []
    )
    class_rows = (
        await session.scalars(
            select(ProjectClass)
            .where(ProjectClass.project_id.in_(project_ids))
            .order_by(ProjectClass.project_id, ProjectClass.class_id)
        )
    ).all()
    counted_dataset_ids = visible_dataset_ids + [
        source_dataset.id for _, source_dataset, _ in source_rows
    ]
    labeled_image_counts: dict[int, int] = (
        dict(
            (
                await session.execute(
                    select(
                        Image.dataset_id,
                        func.count(func.distinct(Annotation.image_id)),
                    )
                    .join(Annotation, Annotation.image_id == Image.id)
                    .where(Image.dataset_id.in_(counted_dataset_ids))
                    .group_by(Image.dataset_id)
                )
            ).all()
        )
        if counted_dataset_ids
        else {}
    )

    sources_by_merged_id: dict[int, list[ProjectDatasetSourceRow]] = {}
    for merged_dataset_id, source_dataset, source_job in source_rows:
        sources_by_merged_id.setdefault(merged_dataset_id, []).append(
            ProjectDatasetSourceRow(
                id=source_dataset.id,
                project_id=source_dataset.project_id,
                name=source_dataset.name,
                image_count=source_dataset.image_count,
                labeled_image_count=labeled_image_counts.get(
                    source_dataset.id, 0
                ),
                annotation_count=source_dataset.annotation_count,
                class_count=source_dataset.class_count,
                created_at=source_dataset.created_at,
                status=source_dataset.status,
                is_merged=source_dataset.is_merged,
                active_job=_active_job(source_job),
            )
        )

    datasets_by_project: dict[int, list[ProjectDatasetRow]] = {}
    for dataset, job in dataset_rows:
        datasets_by_project.setdefault(dataset.project_id, []).append(
            ProjectDatasetRow(
                id=dataset.id,
                project_id=dataset.project_id,
                name=dataset.name,
                image_count=dataset.image_count,
                labeled_image_count=labeled_image_counts.get(dataset.id, 0),
                annotation_count=dataset.annotation_count,
                class_count=dataset.class_count,
                created_at=dataset.created_at,
                status=dataset.status,
                is_merged=dataset.is_merged,
                active_job=_active_job(job),
                source_datasets=sources_by_merged_id.get(dataset.id, []),
            )
        )
    classes_by_project: dict[int, list[ProjectClassRow]] = {}
    for item in class_rows:
        classes_by_project.setdefault(item.project_id, []).append(
            ProjectClassRow(
                class_id=item.class_id,
                name=item.name,
                color=item.color,
            )
        )

    rows: list[ProjectRow] = []
    for project in projects:
        datasets = datasets_by_project.get(project.id, [])
        classes = classes_by_project.get(project.id, [])
        rows.append(
            ProjectRow(
                id=project.id,
                name=project.name,
                created_at=project.created_at,
                updated_at=project.updated_at,
                archived=project.archived_at is not None,
                dataset_count=len(datasets),
                image_count=sum(item.image_count for item in datasets),
                annotation_count=sum(
                    item.annotation_count for item in datasets
                ),
                class_count=len(classes),
                classes=classes,
                datasets=datasets,
            )
        )
    return rows


async def _owned_project_or_404(
    session: AsyncSession,
    project_id: int,
    owner_id: int,
    *,
    for_update: bool = False,
) -> Project:
    statement = select(Project).where(
        Project.id == project_id,
        Project.owner_id == owner_id,
    )
    if for_update:
        statement = statement.with_for_update()
    project = await session.scalar(statement)
    if project is None:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
    return project


async def _run_accounted_bytes(
    request: Request,
    run: TrainingRun,
) -> int:
    if run.artifacts_deleted_at is not None:
        return 0
    if run.artifact_bytes is not None:
        return int(run.artifact_bytes)
    try:
        run_root = contained_storage_path(
            request.app.state.settings.storage_dir,
            run.out_dir,
        )
    except StorageBoundaryError:
        return 0
    return await asyncio.to_thread(path_tree_bytes, run_root / "artifacts")


@router.post("", status_code=201, response_model=ProjectRow)
async def create_project(
    body: ProjectCreate,
    session: Session,
    current_user: CurrentUserDep,
) -> ProjectRow:
    project = Project(owner_id=current_user.id, name=body.name)
    session.add(project)
    try:
        await session.flush()
        session.add_all(
            [
                ProjectClass(
                    project_id=project.id,
                    class_id=class_id,
                    name=item.name,
                    color=item.color,
                )
                for class_id, item in enumerate(body.classes)
            ]
        )
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="이미 있는 프로젝트 또는 클래스 이름입니다.",
        ) from error

    rows = await _project_rows(
        session,
        current_user.id,
        project_id=project.id,
    )
    return rows[0]


@router.get("", response_model=ProjectPage)
async def list_projects(
    session: Session,
    current_user: CurrentUserDep,
) -> ProjectPage:
    rows = await _project_rows(session, current_user.id)
    return ProjectPage(items=rows, total=len(rows))


@router.get(
    "/{project_id}/class-image-counts",
    response_model=ProjectClassImageCountPage,
)
async def get_project_class_image_counts(
    project_id: int,
    dataset_ids: Annotated[list[int], Query(min_length=1, max_length=200)],
    session: Session,
    current_user: CurrentUserDep,
) -> ProjectClassImageCountPage:
    await _owned_project_or_404(session, project_id, current_user.id)
    selected_dataset_ids = list(dict.fromkeys(dataset_ids))
    selected_count = await session.scalar(
        select(func.count())
        .select_from(Dataset)
        .where(
            Dataset.id.in_(selected_dataset_ids),
            Dataset.project_id == project_id,
            Dataset.owner_id == current_user.id,
            Dataset.status == "ready",
            Dataset.is_placeholder.is_(False),
        )
    )
    if selected_count != len(selected_dataset_ids):
        raise HTTPException(status_code=404, detail="데이터셋을 찾을 수 없습니다.")

    counts = (
        select(
            Annotation.class_id.label("class_id"),
            func.count(func.distinct(Image.id)).label("image_count"),
        )
        .select_from(Annotation)
        .join(Image, Image.id == Annotation.image_id)
        .where(Image.dataset_id.in_(selected_dataset_ids))
        .group_by(Annotation.class_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                ProjectClass,
                func.coalesce(counts.c.image_count, 0),
            )
            .outerjoin(counts, counts.c.class_id == ProjectClass.class_id)
            .where(ProjectClass.project_id == project_id)
            .order_by(ProjectClass.class_id)
        )
    ).all()
    return ProjectClassImageCountPage(
        items=[
            ProjectClassImageCountRow(
                class_id=project_class.class_id,
                name=project_class.name,
                color=project_class.color,
                image_count=int(image_count),
            )
            for project_class, image_count in rows
        ]
    )


@router.get("/{project_id}", response_model=ProjectRow)
async def get_project(
    project_id: int,
    session: Session,
    current_user: CurrentUserDep,
) -> ProjectRow:
    rows = await _project_rows(
        session,
        current_user.id,
        project_id=project_id,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
    return rows[0]


@router.patch("/{project_id}", response_model=ProjectRenamed)
async def rename_project(
    project_id: int,
    body: ProjectName,
    session: Session,
    current_user: CurrentUserDep,
) -> ProjectRenamed:
    project = await _owned_project_or_404(
        session,
        project_id,
        current_user.id,
        for_update=True,
    )
    project.name = body.name
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="이미 있는 프로젝트 이름입니다.",
        ) from error
    return ProjectRenamed(id=project_id, name=body.name)


@router.patch(
    "/{project_id}/classes/{class_id}",
    response_model=ProjectClassRow,
)
async def update_project_class(
    project_id: int,
    class_id: int,
    body: ProjectClassUpdate,
    session: Session,
    current_user: CurrentUserDep,
) -> ProjectClassRow:
    # 프로젝트 잠금으로 rename 전파와 학습 제출 스냅샷을 직렬화한다
    # (datasets.rename_dataset_class 와 같은 계약).
    project = await _owned_project_or_404(
        session,
        project_id,
        current_user.id,
        for_update=True,
    )
    project_class = await session.get(ProjectClass, (project_id, class_id))
    if project_class is None:
        raise HTTPException(status_code=404, detail="클래스를 찾을 수 없습니다.")
    try:
        if body.name is not None:
            project_class.name = body.name
            # 프로젝트 안 모든 실데이터셋의 동일 class_id 에 이름 전파
            await session.execute(
                update(DatasetClass)
                .where(
                    DatasetClass.class_id == class_id,
                    DatasetClass.dataset_id.in_(
                        select(Dataset.id).where(
                            Dataset.project_id == project_id,
                            Dataset.owner_id == current_user.id,
                            Dataset.is_placeholder.is_(False),
                        )
                    ),
                )
                .values(name=body.name)
            )
        if body.color is not None:
            project_class.color = body.color
        project.updated_at = func.now()
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="이미 있는 클래스 이름입니다.",
        ) from error
    return ProjectClassRow(
        class_id=class_id,
        name=project_class.name,
        color=project_class.color,
    )


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    project_id: int,
    request: Request,
    session: Session,
    current_user: CurrentUserDep,
    confirm: Annotated[bool, Query()] = False,
) -> Response:
    project = await _owned_project_or_404(
        session,
        project_id,
        current_user.id,
        for_update=True,
    )
    datasets = list(
        (
            await session.scalars(
                select(Dataset)
                .where(
                    Dataset.project_id == project_id,
                    Dataset.owner_id == current_user.id,
                )
                .order_by(Dataset.id)
                .with_for_update()
            )
        ).all()
    )
    dataset_ids = [dataset.id for dataset in datasets]
    runs = (
        list(
            (
                await session.scalars(
                    select(TrainingRun)
                    .where(
                        TrainingRun.dataset_id.in_(dataset_ids),
                        TrainingRun.owner_id == current_user.id,
                    )
                    .order_by(TrainingRun.id)
                    .with_for_update()
                )
            ).all()
        )
        if dataset_ids
        else []
    )
    active_runs = [
        run for run in runs if run.state in {"running", "canceling"}
    ]
    if active_runs:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "project-active-runs",
                "message": (
                    "진행 중이거나 취소 중인 학습이 있어 프로젝트를 "
                    "삭제할 수 없습니다. 학습이 끝난 뒤 다시 시도하세요."
                ),
                "runs": [
                    {
                        "id": run.id,
                        "dataset_id": run.dataset_id,
                        "dataset_name": run.dataset_name,
                        "state": run.state,
                    }
                    for run in active_runs
                ],
            },
        )

    visible_datasets = [
        dataset for dataset in datasets if not dataset.is_placeholder
    ]
    if visible_datasets and not confirm:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "project-delete-confirmation-required",
                "requires_confirmation": True,
                "warning": "이 작업은 되돌릴 수 없습니다.",
                "datasets": [
                    {"id": dataset.id, "name": dataset.name}
                    for dataset in visible_datasets
                ],
            },
        )

    storage_dir = request.app.state.settings.storage_dir
    staged_deletions = []
    accounted_bytes = 0
    try:
        for dataset in datasets:
            accounted_bytes += await dataset_accounted_bytes(
                session,
                dataset.id,
            )
            if dataset.storage_path:
                staged_deletions.append(
                    await asyncio.to_thread(
                        stage_dataset_deletion,
                        storage_dir,
                        dataset.storage_path,
                    )
                )
        for run in runs:
            accounted_bytes += await _run_accounted_bytes(request, run)
            staged_deletions.append(
                await asyncio.to_thread(
                    stage_training_run_deletion,
                    storage_dir,
                    run.out_dir,
                )
            )

        for run in runs:
            await session.delete(run)
        await session.flush()
        for dataset in datasets:
            await session.delete(dataset)
        await session.flush()
        await session.delete(project)
        await session.commit()
    except Exception:
        await session.rollback()
        for staged in reversed(staged_deletions):
            await asyncio.to_thread(restore_staged_deletion, staged)
        raise

    for staged in staged_deletions:
        await asyncio.to_thread(finalize_staged_deletion, staged)
    if accounted_bytes:
        await decrease_bytes_used(
            session,
            current_user.id,
            accounted_bytes,
        )
        await session.commit()
    return Response(status_code=204)
