"""Resumable upload-session HTTP API."""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session, set_local_lock_timeout
from app.deps import CurrentUserDep
from app.models import Dataset, UploadSession
from app.services.uploads import (
    abort_upload,
    complete_upload,
    complete_upload_batch,
    inspect_chunk,
    locked_upload,
    store_chunk_stream,
    upload_directory,
)
from app.services.jobs import enqueue_upload_batch_job, enqueue_upload_job
from app.services.quota import quota_status
from app.services.validate import (
    InsufficientStorage,
    UploadLimitExceeded,
    validate_upload_capacity,
)


router = APIRouter(tags=["uploads"])
Session = Annotated[AsyncSession, Depends(get_session)]


class UploadCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=1024)
    size: int = Field(ge=0)
    chunk_size: int = Field(gt=0)
    kind: Literal["file", "folder", "zip"]
    file_count: int = Field(default=1, ge=1)
    expected_extracted_size: int | None = Field(default=None, ge=0)

    @field_validator("filename")
    @classmethod
    def reject_null_filename(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("filename must not contain null bytes")
        return value


class UploadCreated(BaseModel):
    upload_id: int
    chunk_size: int
    received: list[int]


class UploadState(BaseModel):
    upload_id: int
    chunk_size: int
    received: list[int]
    size: int
    state: Literal["open", "complete", "aborted"]


class JobCreated(BaseModel):
    job_id: int


class UploadBatchComplete(BaseModel):
    upload_ids: list[int] = Field(min_length=1, max_length=200_000)


class UploadBatchPreflight(BaseModel):
    total_size: int = Field(ge=0)
    largest_file_size: int = Field(ge=0)
    file_count: int = Field(ge=1)
    expected_extracted_size: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_sizes(self) -> "UploadBatchPreflight":
        if self.largest_file_size > self.total_size:
            raise ValueError("largest_file_size must not exceed total_size")
        return self


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


async def _upload_or_404(
    session: AsyncSession,
    upload_id: int,
    owner_id: int,
    *,
    for_update: bool = False,
) -> UploadSession:
    statement = (
        select(UploadSession)
        .join(Dataset, Dataset.id == UploadSession.dataset_id)
        .where(
            UploadSession.id == upload_id,
            Dataset.owner_id == owner_id,
        )
    )
    if for_update:
        statement = statement.with_for_update(of=UploadSession)
    upload = await session.scalar(statement)
    if upload is None:
        raise HTTPException(status_code=404, detail="업로드 세션을 찾을 수 없습니다.")
    return upload


@router.delete("/api/uploads/{upload_id}", status_code=204)
async def delete_upload(
    upload_id: int,
    request: Request,
    session: Session,
    current_user: CurrentUserDep,
) -> Response:
    await set_local_lock_timeout(session)
    await _upload_or_404(
        session,
        upload_id,
        current_user.id,
        for_update=True,
    )
    await abort_upload(
        session,
        request.app.state.settings,
        upload_id,
    )
    return Response(status_code=204)


async def _owned_batch_or_404(
    session: AsyncSession,
    dataset_id: int,
    upload_ids: list[int],
    owner_id: int,
) -> None:
    requested_ids = set(upload_ids)
    owned_ids = set(
        (
            await session.scalars(
                select(UploadSession.id)
                .join(Dataset, Dataset.id == UploadSession.dataset_id)
                .where(
                    UploadSession.id.in_(requested_ids),
                    UploadSession.dataset_id == dataset_id,
                    Dataset.owner_id == owner_id,
                )
            )
        ).all()
    )
    if owned_ids != requested_ids:
        raise HTTPException(status_code=404, detail="업로드 세션을 찾을 수 없습니다.")


def _check_capacity(
    request: Request,
    *,
    size: int,
    file_count: int,
    expected_extracted_size: int,
) -> None:
    try:
        validate_upload_capacity(
            request.app.state.settings,
            size=size,
            file_count=file_count,
            expected_extracted_size=expected_extracted_size,
        )
    except UploadLimitExceeded as error:
        raise HTTPException(status_code=413, detail=str(error)) from error
    except InsufficientStorage as error:
        raise HTTPException(
            status_code=507,
            detail={
                "message": "디스크 여유가 부족합니다.",
                "required_bytes": error.required_bytes,
                "available_bytes": error.available_bytes,
            },
        ) from error


async def _check_user_quota(
    session: AsyncSession,
    request: Request,
    owner_id: int,
    required_bytes: int,
) -> None:
    status = await quota_status(
        session,
        owner_id,
        limit_bytes=request.app.state.settings.quota_bytes_per_user,
        required_bytes=required_bytes,
    )
    if not status.allowed:
        raise HTTPException(status_code=413, detail=status.detail)


@router.post(
    "/api/datasets/{dataset_id}/uploads",
    status_code=201,
    response_model=UploadCreated,
)
async def create_upload(
    dataset_id: int,
    body: UploadCreate,
    request: Request,
    session: Session,
    current_user: CurrentUserDep,
) -> UploadCreated:
    await _dataset_or_404(session, dataset_id, current_user.id)
    _check_capacity(
        request,
        size=body.size,
        file_count=body.file_count,
        expected_extracted_size=(
            body.size
            if body.expected_extracted_size is None
            else body.expected_extracted_size
        ),
    )
    await _check_user_quota(
        session,
        request,
        current_user.id,
        max(
            body.size,
            body.size
            if body.expected_extracted_size is None
            else body.expected_extracted_size,
        ),
    )

    upload = UploadSession(
        dataset_id=dataset_id,
        filename=body.filename,
        size=body.size,
        chunk_size=body.chunk_size,
        received_chunks=[],
        kind=body.kind,
        state="open",
    )
    session.add(upload)
    await session.flush()
    upload_id = upload.id
    chunk_size = upload.chunk_size
    # Publish the database row before its directory.  The GC can therefore
    # never observe a real upload directory whose owning row is still hidden
    # inside this transaction and mistake it for an orphan.
    await session.commit()
    try:
        await asyncio.to_thread(
            upload_directory(request.app.state.settings, upload_id).mkdir,
            parents=True,
            exist_ok=False,
        )
    except Exception:
        failed_upload = await locked_upload(session, upload_id)
        failed_upload.state = "aborted"
        await session.commit()
        raise
    return UploadCreated(
        upload_id=upload_id,
        chunk_size=chunk_size,
        received=[],
    )


@router.post(
    "/api/datasets/{dataset_id}/upload-batches/preflight",
    status_code=204,
)
async def preflight_upload_batch(
    dataset_id: int,
    body: UploadBatchPreflight,
    request: Request,
    session: Session,
    current_user: CurrentUserDep,
) -> Response:
    await _dataset_or_404(session, dataset_id, current_user.id)
    _check_capacity(
        request,
        size=body.largest_file_size,
        file_count=body.file_count,
        expected_extracted_size=max(
            body.total_size,
            body.expected_extracted_size,
        ),
    )
    await _check_user_quota(
        session,
        request,
        current_user.id,
        max(body.total_size, body.expected_extracted_size),
    )
    return Response(status_code=204)


@router.get("/api/uploads/{upload_id}", response_model=UploadState)
async def get_upload(
    upload_id: int,
    session: Session,
    current_user: CurrentUserDep,
) -> UploadState:
    upload = await _upload_or_404(session, upload_id, current_user.id)
    return UploadState(
        upload_id=upload.id,
        chunk_size=upload.chunk_size,
        received=sorted(upload.received_chunks),
        size=upload.size,
        state=upload.state,
    )


@router.put("/api/uploads/{upload_id}/chunks/{chunk_number}", status_code=204)
async def put_chunk(
    upload_id: int,
    chunk_number: int,
    request: Request,
    session: Session,
    current_user: CurrentUserDep,
) -> Response:
    await _upload_or_404(session, upload_id, current_user.id)
    expected_size = await inspect_chunk(session, upload_id, chunk_number)
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError as error:
            raise HTTPException(
                status_code=400,
                detail="Content-Length가 올바르지 않습니다.",
            ) from error
        if declared_size > expected_size:
            raise HTTPException(
                status_code=413,
                detail="청크 본문이 허용 크기를 초과했습니다.",
            )
        if declared_size < expected_size:
            raise HTTPException(
                status_code=422,
                detail=f"청크 크기가 올바르지 않습니다. 필요 {expected_size}바이트",
            )
    await store_chunk_stream(
        session,
        request.app.state.settings,
        upload_id,
        chunk_number,
        request.stream(),
        expected_size,
    )
    return Response(status_code=204)


@router.post(
    "/api/uploads/{upload_id}/complete",
    status_code=202,
    response_model=JobCreated,
)
async def finish_upload(
    upload_id: int,
    request: Request,
    session: Session,
    current_user: CurrentUserDep,
) -> JobCreated:
    await _upload_or_404(session, upload_id, current_user.id)
    job = await complete_upload(
        session,
        request.app.state.settings,
        upload_id,
    )
    if request.app.state.auto_start_jobs:
        enqueue_upload_job(request.app, job.id, upload_id)
    return JobCreated(job_id=job.id)


@router.post(
    "/api/datasets/{dataset_id}/upload-batches/complete",
    status_code=202,
    response_model=JobCreated,
)
async def finish_upload_batch(
    dataset_id: int,
    body: UploadBatchComplete,
    request: Request,
    session: Session,
    current_user: CurrentUserDep,
) -> JobCreated:
    await _dataset_or_404(session, dataset_id, current_user.id)
    await _owned_batch_or_404(
        session,
        dataset_id,
        body.upload_ids,
        current_user.id,
    )
    job = await complete_upload_batch(
        session,
        request.app.state.settings,
        dataset_id,
        body.upload_ids,
    )
    if request.app.state.auto_start_jobs:
        enqueue_upload_batch_job(request.app, job.id, body.upload_ids)
    return JobCreated(job_id=job.id)
