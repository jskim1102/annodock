"""Dataset CRUD endpoints backed by precomputed aggregate columns."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import case, delete, func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.class_colors import class_color
from app.db import get_session, is_lock_not_available, set_local_lock_timeout
from app.deps import (
    AdminWorkspaceDep,
    CurrentWorkspaceDep,
    EditorWorkspaceDep,
    check_workspace_role,
)
from app.models import (
    Dataset,
    DatasetClass,
    DatasetMergeSource,
    Image,
    Project,
    ProjectClass,
    TrainingRun,
    UploadJob,
    UploadSession,
)
from app.services.dataset_extract import (
    DatasetExtractConflict,
    DatasetExtractNotFound,
    DatasetExtractQuotaExceeded,
    extract_dataset_snapshot,
)
from app.services.dataset_merge import (
    DatasetMergeConflict,
    DatasetMergeNotFound,
    extend_merged_dataset,
    merge_datasets,
)
from app.services.jobs import enqueue_dataset_merge_job
from app.services.quota import (
    apply_dataset_storage_release,
    decrease_bytes_used,
    plan_dataset_storage_release,
)
from app.services.storage import (
    create_dataset_storage,
    finalize_staged_deletions,
    restore_staged_deletions,
    stage_deletions_async,
    storage_relative_path,
)
from app.services.uploads import upload_directory


router = APIRouter(prefix="/api/datasets", tags=["datasets"])
Session = Annotated[AsyncSession, Depends(get_session)]
DatasetStatus = Literal["pending", "processing", "ready", "failed"]
MergePurpose = Literal["merge", "train"]


class DatasetName(BaseModel):
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


class DatasetCreate(DatasetName):
    project_id: int | None = Field(default=None, gt=0)
    upload_draft: bool = False


class ClassName(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    expected_catalog_version: int | None = Field(default=None, ge=0, strict=True)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        if "\x00" in normalized:
            raise ValueError("name must not contain null bytes")
        return normalized


class DatasetRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    name: str
    image_count: int
    annotation_count: int
    class_count: int
    created_at: datetime
    status: DatasetStatus
    is_merged: bool
    merge_purpose: MergePurpose | None


class SplitRow(BaseModel):
    split: str
    image_count: int


class DatasetDetail(DatasetRow):
    splits: list[SplitRow]
    workspace_id: int
    my_role: str


class ActiveJob(BaseModel):
    job_id: int
    state: Literal["queued", "running", "awaiting_class_resolution"]
    phase: str
    total: int
    processed: int
    failed: int


class DatasetSourceRow(DatasetRow):
    active_job: ActiveJob | None


class DatasetListRow(DatasetSourceRow):
    source_datasets: list[DatasetSourceRow]


class DatasetPage(BaseModel):
    items: list[DatasetListRow]
    total: int


class ClassRow(BaseModel):
    class_id: int
    name: str


class ClassList(BaseModel):
    classes: list[ClassRow]
    catalog_version: int


class ClassUpdated(ClassRow, ClassList):
    pass


class DatasetRenamed(BaseModel):
    id: int
    name: str


class DatasetMergeRequest(DatasetName):
    dataset_ids: list[int] = Field(min_length=2, max_length=200)
    purpose: MergePurpose = "merge"

    @field_validator("dataset_ids")
    @classmethod
    def validate_dataset_ids(cls, value: list[int]) -> list[int]:
        if any(dataset_id <= 0 for dataset_id in value):
            raise ValueError("dataset ids must be positive")
        if len(set(value)) != len(value):
            raise ValueError("dataset ids must be unique")
        return value


class DatasetMergeSourcesRequest(BaseModel):
    dataset_ids: list[int] = Field(min_length=1, max_length=200)
    purpose: MergePurpose = "merge"

    @field_validator("dataset_ids")
    @classmethod
    def validate_dataset_ids(cls, value: list[int]) -> list[int]:
        if any(dataset_id <= 0 for dataset_id in value):
            raise ValueError("dataset ids must be positive")
        if len(set(value)) != len(value):
            raise ValueError("dataset ids must be unique")
        return value


class DatasetExtractRequest(DatasetName):
    model_config = ConfigDict(extra="forbid")

    dataset_ids: list[int] = Field(min_length=1, max_length=200)
    class_ids: list[int] = Field(min_length=1, max_length=200)

    @field_validator("dataset_ids")
    @classmethod
    def validate_dataset_ids(cls, value: list[int]) -> list[int]:
        if any(dataset_id <= 0 for dataset_id in value):
            raise ValueError("dataset ids must be positive")
        if len(set(value)) != len(value):
            raise ValueError("dataset ids must be unique")
        return value

    @field_validator("class_ids")
    @classmethod
    def validate_class_ids(cls, value: list[int]) -> list[int]:
        if any(class_id < 0 for class_id in value):
            raise ValueError("class ids must be non-negative")
        if len(set(value)) != len(value):
            raise ValueError("class ids must be unique")
        return value


async def _dataset_or_404(
    session: AsyncSession,
    dataset_id: int,
    workspace_id: int,
    *,
    for_update: bool = False,
) -> Dataset:
    statement = select(Dataset).where(
        Dataset.id == dataset_id,
        Dataset.workspace_id == workspace_id,
        Dataset.is_placeholder.is_(False),
    )
    if for_update:
        statement = statement.with_for_update()
    dataset = await session.scalar(statement)
    if dataset is None:
        raise HTTPException(status_code=404, detail="데이터셋을 찾을 수 없습니다.")
    return dataset


def _duplicate_name() -> HTTPException:
    return HTTPException(status_code=409, detail="이미 있는 이름입니다.")


def _active_job(job: UploadJob | None) -> ActiveJob | None:
    if job is None:
        return None
    return ActiveJob(
        job_id=job.id,
        state=job.state,
        phase=job.phase,
        total=job.total,
        processed=job.processed,
        failed=job.failed,
    )


def _source_row(dataset: Dataset, job: UploadJob | None) -> DatasetSourceRow:
    return DatasetSourceRow(
        **DatasetRow.model_validate(dataset).model_dump(),
        active_job=_active_job(job),
    )


@router.post("", status_code=201, response_model=DatasetRow)
async def create_dataset(
    body: DatasetCreate,
    request: Request,
    session: Session,
    current_workspace: EditorWorkspaceDep,
) -> Dataset:
    if body.project_id is None:
        check_workspace_role(current_workspace, "admin")
        project = await session.scalar(
            select(Project).where(
                Project.workspace_id == current_workspace.id,
                Project.name == body.name,
            ).with_for_update()
        )
        if project is None:
            project = Project(workspace_id=current_workspace.id, name=body.name)
            session.add(project)
            await session.flush()
    else:
        project = await session.scalar(
            select(Project)
            .where(
                Project.id == body.project_id,
                Project.workspace_id == current_workspace.id,
            )
            .with_for_update()
        )
        if project is None:
            raise HTTPException(
                status_code=404,
                detail="프로젝트를 찾을 수 없습니다.",
            )

    project_classes = (
        await session.scalars(
            select(ProjectClass)
            .where(ProjectClass.project_id == project.id)
            .order_by(ProjectClass.class_id)
        )
    ).all()
    placeholder = await session.scalar(
        select(Dataset)
        .where(
            Dataset.project_id == project.id,
            Dataset.workspace_id == current_workspace.id,
            Dataset.is_placeholder.is_(True),
            # Only migration-era untouched placeholders are reusable. Upload
            # drafts can own a paused job and must never be repurposed.
            ~Dataset.classes.any(),
            ~Dataset.images.any(),
            ~Dataset.upload_sessions.any(),
            ~Dataset.upload_jobs.any(),
        )
        .order_by(Dataset.id)
        .limit(1)
        .with_for_update()
    )
    if placeholder is None:
        dataset = Dataset(
            workspace_id=current_workspace.id,
            project_id=project.id,
            name=body.name,
            status="pending",
            storage_path="",
            class_count=len(project_classes),
            is_placeholder=body.upload_draft,
        )
        session.add(dataset)
    else:
        dataset = placeholder
        dataset.name = body.name
        dataset.status = "pending"
        dataset.is_placeholder = body.upload_draft
        dataset.class_count = len(project_classes)
    try:
        await session.flush()
        if not dataset.storage_path:
            path = create_dataset_storage(
                request.app.state.settings.storage_dir,
                dataset.id,
            )
            dataset.storage_path = storage_relative_path(
                request.app.state.settings.storage_dir,
                path,
            )
        session.add_all(
            [
                DatasetClass(
                    dataset_id=dataset.id,
                    class_id=item.class_id,
                    name=item.name,
                )
                for item in project_classes
            ]
        )
        if not body.upload_draft:
            project.updated_at = func.now()
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise _duplicate_name() from error
    except Exception:
        await session.rollback()
        raise
    await session.refresh(dataset)
    return dataset


@router.get("", response_model=DatasetPage)
async def list_datasets(
    session: Session,
    current_workspace: CurrentWorkspaceDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> DatasetPage:
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
    rows = (
        await session.execute(
            select(Dataset, UploadJob)
            .outerjoin(UploadJob, UploadJob.id == latest_active_job_id)
            .where(
                Dataset.workspace_id == current_workspace.id,
                Dataset.is_placeholder.is_(False),
                ~Dataset.id.in_(source_dataset_ids),
            )
            .order_by(
                Dataset.created_at.desc(),
                Dataset.upload_group_id.desc().nulls_last(),
                Dataset.upload_part_index.asc().nulls_last(),
                Dataset.id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
    ).all()
    total = await session.scalar(
        select(func.count())
        .select_from(Dataset)
        .where(
            Dataset.workspace_id == current_workspace.id,
            Dataset.is_placeholder.is_(False),
            ~Dataset.id.in_(source_dataset_ids),
        )
    )
    dataset_ids = [dataset.id for dataset, _ in rows]
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
                    DatasetMergeSource.merged_dataset_id.in_(dataset_ids),
                    Dataset.workspace_id == current_workspace.id,
                    Dataset.is_placeholder.is_(False),
                )
                .order_by(
                    DatasetMergeSource.merged_dataset_id,
                    DatasetMergeSource.position,
                )
            )
        ).all()
        if dataset_ids
        else []
    )
    sources_by_merged_id: dict[int, list[DatasetSourceRow]] = {}
    for merged_dataset_id, source_dataset, source_job in source_rows:
        sources_by_merged_id.setdefault(merged_dataset_id, []).append(
            _source_row(source_dataset, source_job)
        )
    return DatasetPage(
        items=[
            DatasetListRow(
                **DatasetRow.model_validate(dataset).model_dump(),
                active_job=_active_job(job),
                source_datasets=sources_by_merged_id.get(dataset.id, []),
            )
            for dataset, job in rows
        ],
        total=total or 0,
    )


@router.post("/merge", status_code=201, response_model=DatasetListRow)
async def create_merged_dataset(
    body: DatasetMergeRequest,
    request: Request,
    response: Response,
    session: Session,
    current_workspace: AdminWorkspaceDep,
) -> DatasetListRow:
    try:
        result = await merge_datasets(
            request.app.state.settings,
            session,
            name=body.name,
            dataset_ids=body.dataset_ids,
            workspace_id=current_workspace.id,
            purpose=body.purpose,
        )
    except DatasetMergeNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except DatasetMergeConflict as error:
        raise HTTPException(status_code=409, detail=error.detail) from error
    except IntegrityError as error:
        raise _duplicate_name() from error
    if (
        result.job is not None
        and result.job.state == "queued"
        and request.app.state.auto_start_jobs
    ):
        enqueue_dataset_merge_job(request.app, result.job.id)
    if result.reused:
        response.status_code = 200
    elif result.job is not None:
        response.status_code = 202
    else:
        response.status_code = 201
    return DatasetListRow(
        **DatasetRow.model_validate(result.dataset).model_dump(),
        active_job=_active_job(result.job),
        source_datasets=[_source_row(source, None) for source in result.sources],
    )


@router.post("/extract", status_code=201, response_model=DatasetRow)
async def create_extracted_dataset(
    body: DatasetExtractRequest,
    request: Request,
    session: Session,
    current_workspace: AdminWorkspaceDep,
) -> Dataset:
    try:
        result = await extract_dataset_snapshot(
            request.app.state.settings,
            session,
            name=body.name,
            dataset_ids=body.dataset_ids,
            class_ids=body.class_ids,
            workspace_id=current_workspace.id,
        )
    except DatasetExtractNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except DatasetExtractConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except DatasetExtractQuotaExceeded as error:
        raise HTTPException(status_code=413, detail=str(error)) from error
    except IntegrityError as error:
        raise _duplicate_name() from error
    return result.dataset


@router.post(
    "/{dataset_id}/merge-sources",
    response_model=DatasetListRow,
)
async def add_merged_dataset_sources(
    dataset_id: int,
    body: DatasetMergeSourcesRequest,
    request: Request,
    session: Session,
    current_workspace: AdminWorkspaceDep,
) -> DatasetListRow:
    if dataset_id in body.dataset_ids:
        raise HTTPException(
            status_code=422,
            detail="대상 병합 데이터셋은 추가 원본이 될 수 없습니다.",
        )
    try:
        result = await extend_merged_dataset(
            request.app.state.settings,
            session,
            merged_dataset_id=dataset_id,
            dataset_ids=body.dataset_ids,
            workspace_id=current_workspace.id,
            purpose=body.purpose,
        )
    except DatasetMergeNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except DatasetMergeConflict as error:
        raise HTTPException(status_code=409, detail=error.detail) from error
    except IntegrityError as error:
        raise HTTPException(
            status_code=409,
            detail="병합 원본 관계가 변경되었습니다. 목록을 새로고침해 주세요.",
        ) from error
    return DatasetListRow(
        **DatasetRow.model_validate(result.dataset).model_dump(),
        active_job=None,
        source_datasets=[_source_row(source, None) for source in result.sources],
    )


@router.get("/{dataset_id}", response_model=DatasetDetail)
async def get_dataset(
    dataset_id: int,
    session: Session,
    current_workspace: CurrentWorkspaceDep,
) -> DatasetDetail:
    dataset = await _dataset_or_404(session, dataset_id, current_workspace.id)
    priority = case(
        (Image.split == "train", 0),
        (Image.split == "val", 1),
        (Image.split == "test", 2),
        else_=3,
    )
    split_rows = (
        await session.execute(
            select(Image.split, func.count(Image.id))
            .where(Image.dataset_id == dataset_id, Image.split.is_not(None))
            .group_by(Image.split)
            .order_by(priority, Image.split)
        )
    ).all()
    return DatasetDetail(
        **DatasetRow.model_validate(dataset).model_dump(),
        workspace_id=current_workspace.id,
        my_role=current_workspace.role,
        splits=[
            SplitRow(split=split_name, image_count=count)
            for split_name, count in split_rows
            if split_name is not None
        ],
    )


async def _dataset_catalog(session: AsyncSession, dataset_id: int) -> ClassList:
    rows = (await session.execute(
        select(Dataset.class_catalog_version, DatasetClass.class_id, DatasetClass.name)
        .outerjoin(DatasetClass, DatasetClass.dataset_id == Dataset.id)
        .where(Dataset.id == dataset_id).order_by(DatasetClass.class_id)
    )).all()
    if not rows:
        raise HTTPException(status_code=404, detail="데이터셋을 찾을 수 없습니다.")
    return ClassList(
        catalog_version=rows[0].class_catalog_version,
        classes=[ClassRow(class_id=row.class_id, name=row.name)
                 for row in rows if row.class_id is not None],
    )


@router.get("/{dataset_id}/classes", response_model=ClassList)
async def get_dataset_classes(
    dataset_id: int,
    session: Session,
    current_workspace: CurrentWorkspaceDep,
) -> ClassList:
    await _dataset_or_404(session, dataset_id, current_workspace.id)
    return await _dataset_catalog(session, dataset_id)


@router.patch("/{dataset_id}", response_model=DatasetRenamed)
async def rename_dataset(
    dataset_id: int,
    body: DatasetName,
    session: Session,
    current_workspace: AdminWorkspaceDep,
) -> DatasetRenamed:
    dataset = await _dataset_or_404(session, dataset_id, current_workspace.id)
    dataset.name = body.name
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise _duplicate_name() from error
    return DatasetRenamed(id=dataset.id, name=dataset.name)


@router.patch(
    "/{dataset_id}/classes/{class_id}",
    response_model=ClassUpdated,
)
async def rename_dataset_class(
    dataset_id: int,
    class_id: int,
    body: ClassName,
    session: Session,
    current_workspace: AdminWorkspaceDep,
) -> ClassUpdated:
    # Renames wait behind autosave, with the parent and all siblings locked
    # in the same order as ingestion and dataset mutations.
    try:
        await set_local_lock_timeout(session, timeout_ms=2500)
        dataset = await _dataset_or_404(
            session,
            dataset_id,
            current_workspace.id,
        )
        project = await session.scalar(
            select(Project)
            .where(
                Project.id == dataset.project_id,
                Project.workspace_id == current_workspace.id,
            )
            .with_for_update()
        )
        if project is None:
            raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
        await session.execute(select(Dataset.id).where(
            Dataset.project_id == project.id,
            Dataset.workspace_id == current_workspace.id,
        ).order_by(Dataset.id).with_for_update())
        current_version = await session.scalar(select(Dataset.class_catalog_version).where(
            Dataset.id == dataset_id, Dataset.project_id == project.id,
            Dataset.workspace_id == current_workspace.id,
        ))
        if current_version is None:
            raise HTTPException(status_code=404, detail="데이터셋을 찾을 수 없습니다.")
        if body.expected_catalog_version is not None and body.expected_catalog_version != current_version:
            raise HTTPException(status_code=409, detail={"code": "class_catalog_changed"})
        dataset_class = await session.get(DatasetClass, (dataset_id, class_id))
        if dataset_class is None:
            raise HTTPException(status_code=404, detail="클래스를 찾을 수 없습니다.")
        project_class = await session.get(
            ProjectClass,
            (dataset.project_id, class_id),
        )
        if project_class is None:
            project_class = ProjectClass(
                project_id=dataset.project_id,
                class_id=class_id,
                name=body.name,
                color=class_color(class_id),
            )
            session.add(project_class)
        else:
            project_class.name = body.name
        await session.execute(
            update(DatasetClass)
            .where(
                DatasetClass.class_id == class_id,
                DatasetClass.dataset_id.in_(
                    select(Dataset.id).where(
                        Dataset.project_id == dataset.project_id,
                        Dataset.workspace_id == current_workspace.id,
                        Dataset.is_placeholder.is_(False),
                    )
                ),
            )
            .values(name=body.name)
        )
        project.updated_at = func.now()
        try:
            await session.flush()
            catalog = await _dataset_catalog(session, dataset_id)
            await session.commit()
        except IntegrityError as error:
            await session.rollback()
            raise HTTPException(
                status_code=409,
                detail="이미 있는 클래스 이름입니다.",
            ) from error
        return ClassUpdated(class_id=class_id, name=body.name, **catalog.model_dump())
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="이미 있는 클래스 이름입니다.",
        ) from error
    except DBAPIError as error:
        await session.rollback()
        if is_lock_not_available(error):
            raise HTTPException(status_code=409, detail="다른 데이터 작업이 진행 중이에요. 잠시 후 다시 시도해 주세요.") from error
        raise


@router.delete("/{dataset_id}", status_code=204)
async def delete_dataset(
    dataset_id: int,
    request: Request,
    session: Session,
    current_workspace: AdminWorkspaceDep,
) -> Response:
    dataset = await _dataset_or_404(
        session,
        dataset_id,
        current_workspace.id,
        for_update=True,
    )
    # Delete only the selected dataset reference; independent snapshots survive.
    targets = [dataset]
    target_ids = [item.id for item in targets]
    active_run_id = await session.scalar(
        select(TrainingRun.id)
        .where(
            TrainingRun.dataset_id.in_(target_ids),
            TrainingRun.workspace_id == current_workspace.id,
            TrainingRun.state.in_(("running", "canceling")),
        )
        .limit(1)
    )
    if active_run_id is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "진행 중인 학습이 참조하는 데이터셋은 삭제할 수 없습니다. "
                "학습이 끝나거나 취소된 뒤 다시 시도하세요."
            ),
        )
    workspace_id = dataset.workspace_id
    storage_dir = request.app.state.settings.storage_dir
    await set_local_lock_timeout(session)
    release_plan = await plan_dataset_storage_release(session, target_ids)
    upload_ids = list(
        (
            await session.scalars(
                select(UploadSession.id)
                .where(UploadSession.dataset_id.in_(target_ids))
                .order_by(UploadSession.id)
                .with_for_update()
            )
        ).all()
    )
    deletion_paths = [item.storage_path for item in targets]
    deletion_paths.extend(
        upload_directory(request.app.state.settings, upload_id)
        for upload_id in upload_ids
    )
    staged_list = await stage_deletions_async(storage_dir, deletion_paths)
    try:
        # A snapshot with a deleted source no longer represents its remaining
        # membership. Detach all its sources atomically with the deletion so
        # it cannot be reused as a snapshot of that smaller source set.
        affected_merges = select(DatasetMergeSource.merged_dataset_id).where(
            DatasetMergeSource.source_dataset_id.in_(target_ids)
        )
        await session.execute(
            delete(DatasetMergeSource).where(
                DatasetMergeSource.merged_dataset_id.in_(affected_merges)
            )
        )
        for item in targets:
            await session.delete(item)
        await session.flush()
        await apply_dataset_storage_release(session, release_plan)
        await decrease_bytes_used(
            session,
            workspace_id,
            release_plan.released_bytes,
        )
        await session.commit()
    except BaseException as error:
        restore_staged_deletions(reversed(staged_list))
        try:
            await session.rollback()
        except BaseException as rollback_error:
            error.add_note(
                "dataset delete rollback also failed: "
                f"{type(rollback_error).__name__}"
            )
        raise
    await asyncio.to_thread(finalize_staged_deletions, staged_list)
    return Response(status_code=204)
