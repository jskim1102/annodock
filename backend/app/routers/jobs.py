"""Upload progress and import-issue query endpoints."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import CurrentUserDep
from app.models import Dataset, ImportIssue, Project, ProjectClass, UploadJob
from app.services.class_resolution import (
    ClassResolutionNameConflict,
    project_renames_for_resolutions,
    validate_class_resolutions,
)
from app.services.jobs import enqueue_upload_batch_job


router = APIRouter(tags=["jobs"])
Session = Annotated[AsyncSession, Depends(get_session)]


class ClassNameConflictResponse(BaseModel):
    key: str
    class_id: int
    source_path: str
    project_name: str
    uploaded_name: str


class ClassResolutionPlanResponse(BaseModel):
    revision: str
    conflicts: list[ClassNameConflictResponse]


class JobResponse(BaseModel):
    job_id: int
    state: Literal[
        "queued",
        "running",
        "awaiting_class_resolution",
        "done",
        "failed",
    ]
    total: int
    processed: int
    failed: int
    phase: str
    class_resolution: ClassResolutionPlanResponse | None


class ClassResolutionChoice(BaseModel):
    key: str = Field(min_length=1, max_length=255)
    action: Literal["use_project", "use_upload"]


class ClassResolutionRequest(BaseModel):
    revision: str = Field(min_length=64, max_length=64)
    resolutions: list[ClassResolutionChoice] = Field(max_length=10_000)


class JobAccepted(BaseModel):
    job_id: int


class IssueResponse(BaseModel):
    kind: Literal[
        "image_without_label",
        "empty_label",
        "label_without_image",
        "broken_image",
        "broken_label",
        "rejected_file",
        "duplicate_skipped",
        "ignored_file",
        "class_conflict",
    ]
    path: str
    detail: str


class IssuePage(BaseModel):
    items: list[IssueResponse]
    total: int


@router.get(
    "/api/jobs/{job_id}",
    response_model=JobResponse,
    response_model_exclude_none=True,
)
async def get_job(
    job_id: int,
    session: Session,
    current_user: CurrentUserDep,
) -> JobResponse:
    job = await session.scalar(
        select(UploadJob)
        .join(Dataset, Dataset.id == UploadJob.dataset_id)
        .where(
            UploadJob.id == job_id,
            Dataset.owner_id == current_user.id,
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
    return JobResponse(
        job_id=job.id,
        state=job.state,
        total=job.total,
        processed=job.processed,
        failed=job.failed,
        phase=job.phase,
        class_resolution=(
            job.class_resolution_plan
            if job.state == "awaiting_class_resolution"
            else None
        ),
    )


@router.post(
    "/api/jobs/{job_id}/class-resolution",
    status_code=202,
    response_model=JobAccepted,
)
async def resolve_class_conflicts(
    job_id: int,
    body: ClassResolutionRequest,
    request: Request,
    session: Session,
    current_user: CurrentUserDep,
) -> JobAccepted:
    job_ref = await session.scalar(
        select(UploadJob)
        .join(Dataset, Dataset.id == UploadJob.dataset_id)
        .where(
            UploadJob.id == job_id,
            Dataset.owner_id == current_user.id,
        )
    )
    if job_ref is None:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")

    # Keep the same lock order as ingestion and class rename: dataset, project,
    # then the job payload. This serializes catalog changes without a cycle.
    dataset = await session.scalar(
        select(Dataset)
        .where(
            Dataset.id == job_ref.dataset_id,
            Dataset.owner_id == current_user.id,
        )
        .with_for_update()
    )
    if dataset is None:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
    project = await session.scalar(
        select(Project)
        .where(Project.id == dataset.project_id)
        .with_for_update()
    )
    if project is None:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
    job = await session.scalar(
        select(UploadJob)
        .where(
            UploadJob.id == job_id,
            UploadJob.dataset_id == dataset.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if job is None:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
    if (
        job.state != "awaiting_class_resolution"
        or not isinstance(job.class_resolution_plan, dict)
    ):
        raise HTTPException(
            status_code=409,
            detail="클래스 명칭 선택을 기다리는 작업이 아닙니다.",
        )

    plan = job.class_resolution_plan
    if body.revision != plan.get("revision"):
        raise HTTPException(
            status_code=409,
            detail="클래스 정보가 변경되었습니다. 다시 확인해 주세요.",
        )
    serialized = [item.model_dump() for item in body.resolutions]
    try:
        actions = validate_class_resolutions(plan, serialized)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    project_classes = {
        row.class_id: row.name
        for row in (
            await session.scalars(
                select(ProjectClass)
                .where(ProjectClass.project_id == project.id)
                .with_for_update()
            )
        ).all()
    }
    try:
        project_renames_for_resolutions(
            plan,
            actions,
            project_classes,
        )
    except ClassResolutionNameConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    upload_ids = list(job.upload_ids)
    if request.app.state.auto_start_jobs and not upload_ids:
        raise HTTPException(
            status_code=409,
            detail="업로드 원본을 찾을 수 없습니다. 다시 업로드해 주세요.",
        )
    job.class_resolutions = serialized
    job.state = "queued"
    job.phase = "uploading"
    await session.commit()

    if request.app.state.auto_start_jobs:
        enqueue_upload_batch_job(request.app, job.id, upload_ids)
    return JobAccepted(job_id=job.id)


@router.get(
    "/api/datasets/{dataset_id}/issues",
    response_model=IssuePage,
)
async def get_issues(
    dataset_id: int,
    session: Session,
    current_user: CurrentUserDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> IssuePage:
    dataset_exists = await session.scalar(
        select(Dataset.id).where(
            Dataset.id == dataset_id,
            Dataset.owner_id == current_user.id,
        )
    )
    if dataset_exists is None:
        raise HTTPException(status_code=404, detail="데이터셋을 찾을 수 없습니다.")
    predicate = UploadJob.dataset_id == dataset_id
    total = await session.scalar(
        select(func.count(ImportIssue.id))
        .join(UploadJob, UploadJob.id == ImportIssue.job_id)
        .where(predicate)
    )
    rows = (
        await session.scalars(
            select(ImportIssue)
            .join(UploadJob, UploadJob.id == ImportIssue.job_id)
            .where(predicate)
            .order_by(ImportIssue.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return IssuePage(
        items=[
            IssueResponse(
                kind=row.kind,
                path=row.path,
                detail=row.detail,
            )
            for row in rows
        ],
        total=total or 0,
    )
