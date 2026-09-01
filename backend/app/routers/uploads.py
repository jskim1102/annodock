"""Resumable upload-session HTTP API."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.db import get_session, set_local_lock_timeout
from app.deps import CurrentUserDep
from app.models import Dataset, UploadBatch, UploadJob, UploadSession
from app.services.uploads import (
    ChunkFileUpload,
    abort_upload,
    complete_upload,
    complete_upload_batch,
    complete_upload_manifest,
    inspect_chunk,
    locked_upload,
    locked_uploads,
    store_chunk_files,
    store_chunk_stream,
    upload_directory,
    upload_file_key,
    upload_ids_match,
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
MAX_CHUNK_BATCH_FILES = 128
MAX_CHUNK_BATCH_PAYLOAD_BYTES = 7 * 1024 * 1024
MAX_CHUNK_BATCH_REQUEST_BYTES = 8 * 1024 * 1024
MAX_CHUNK_BATCH_METADATA_BYTES = 64 * 1024


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
    size: int
    state: Literal["open", "complete", "aborted"]


class UploadBatchCreate(BaseModel):
    batch_id: UUID | None = None
    files: list[UploadCreate] = Field(min_length=1, max_length=1_000)


class UploadBatchCreated(BaseModel):
    uploads: list[UploadCreated]


class UploadState(BaseModel):
    upload_id: int
    chunk_size: int
    received: list[int]
    size: int
    state: Literal["open", "complete", "aborted"]


class JobCreated(BaseModel):
    job_id: int


class UploadBatchComplete(BaseModel):
    upload_ids: list[int] = Field(min_length=1, max_length=500_000)


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


class UploadBatchState(BaseModel):
    batch_id: UUID
    state: Literal["open", "sealed"]
    job_id: int | None


class UploadChunkBatchItem(BaseModel):
    upload_id: int = Field(gt=0)
    chunk_number: int = Field(ge=0)
    size: int = Field(ge=0)


class UploadChunkBatchMetadata(BaseModel):
    chunks: list[UploadChunkBatchItem] = Field(
        min_length=1,
        max_length=MAX_CHUNK_BATCH_FILES,
    )

    @model_validator(mode="after")
    def reject_duplicate_chunks(self) -> "UploadChunkBatchMetadata":
        identities = {
            (chunk.upload_id, chunk.chunk_number)
            for chunk in self.chunks
        }
        if len(identities) != len(self.chunks):
            raise ValueError("chunk identities must be unique")
        return self


class _ChunkBatchTooLarge(MultiPartException):
    pass


async def _bounded_chunk_batch_stream(request: Request) -> AsyncIterator[bytes]:
    received = 0
    async for content in request.stream():
        received += len(content)
        if received > MAX_CHUNK_BATCH_REQUEST_BYTES:
            raise _ChunkBatchTooLarge("Chunk batch body too large.")
        yield content


async def _parse_chunk_batch(
    request: Request,
) -> tuple[FormData, UploadChunkBatchMetadata, list[UploadFile]]:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as error:
            raise HTTPException(
                status_code=400,
                detail="Content-Length가 올바르지 않습니다.",
            ) from error
        if declared_length < 0:
            raise HTTPException(
                status_code=400,
                detail="Content-Length가 올바르지 않습니다.",
            )
        if declared_length > MAX_CHUNK_BATCH_REQUEST_BYTES:
            raise HTTPException(
                status_code=413,
                detail="청크 배치 본문이 허용 크기를 초과했습니다.",
            )

    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise HTTPException(
            status_code=415,
            detail="청크 배치는 multipart/form-data 형식이어야 합니다.",
        )
    parser = MultiPartParser(
        request.headers,
        _bounded_chunk_batch_stream(request),
        max_files=MAX_CHUNK_BATCH_FILES,
        max_fields=1,
        max_part_size=MAX_CHUNK_BATCH_METADATA_BYTES,
    )
    try:
        form = await parser.parse()
    except _ChunkBatchTooLarge as error:
        raise HTTPException(
            status_code=413,
            detail="청크 배치 본문이 허용 크기를 초과했습니다.",
        ) from error
    except MultiPartException as error:
        status_code = 413 if "Too many" in str(error) else 400
        raise HTTPException(status_code=status_code, detail=str(error)) from error

    try:
        entries = form.multi_items()
        if any(
            name not in {"metadata", "chunks"}
            or (name == "metadata" and not isinstance(value, str))
            or (name == "chunks" and not isinstance(value, UploadFile))
            for name, value in entries
        ):
            raise HTTPException(
                status_code=422,
                detail="지원하지 않는 청크 배치 필드가 있습니다.",
            )
        metadata_values = [
            value
            for name, value in entries
            if name == "metadata" and isinstance(value, str)
        ]
        files = [
            value
            for name, value in entries
            if name == "chunks" and isinstance(value, UploadFile)
        ]
        if len(metadata_values) != 1:
            raise HTTPException(
                status_code=422,
                detail="청크 배치 메타데이터가 하나 필요합니다.",
            )
        try:
            metadata = UploadChunkBatchMetadata.model_validate_json(
                metadata_values[0]
            )
        except ValidationError as error:
            raise HTTPException(
                status_code=422,
                detail="청크 배치 메타데이터가 올바르지 않습니다.",
            ) from error
        if len(files) != len(metadata.chunks):
            raise HTTPException(
                status_code=422,
                detail="청크 메타데이터와 파일 수가 다릅니다.",
            )
        payload_size = 0
        for item, file in zip(metadata.chunks, files, strict=True):
            if file.size != item.size:
                raise HTTPException(
                    status_code=422,
                    detail="청크 메타데이터와 실제 파일 크기가 다릅니다.",
                )
            payload_size += item.size
        if payload_size > MAX_CHUNK_BATCH_PAYLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail="청크 배치 파일이 허용 크기를 초과했습니다.",
            )
        return form, metadata, files
    except BaseException:
        await form.close()
        raise


async def _dataset_or_404(
    session: AsyncSession,
    dataset_id: int,
    owner_id: int,
    *,
    for_update: bool = False,
) -> Dataset:
    statement = select(Dataset).where(
        Dataset.id == dataset_id,
        Dataset.owner_id == owner_id,
    )
    if for_update:
        statement = statement.with_for_update()
    dataset = await session.scalar(statement)
    if dataset is None:
        raise HTTPException(status_code=404, detail="데이터셋을 찾을 수 없습니다.")
    return dataset


async def _upload_batch_or_404(
    session: AsyncSession,
    dataset_id: int,
    batch_id: UUID,
    owner_id: int,
    *,
    for_update: bool = False,
) -> UploadBatch:
    statement = (
        select(UploadBatch)
        .join(Dataset, Dataset.id == UploadBatch.dataset_id)
        .where(
            UploadBatch.id == batch_id,
            UploadBatch.dataset_id == dataset_id,
            Dataset.owner_id == owner_id,
        )
    )
    if for_update:
        statement = statement.with_for_update(of=UploadBatch)
    upload_batch = await session.scalar(statement)
    if upload_batch is None:
        raise HTTPException(status_code=404, detail="업로드 배치를 찾을 수 없습니다.")
    return upload_batch


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
                    upload_ids_match(requested_ids),
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


def _created_upload(upload: UploadSession) -> UploadCreated:
    return UploadCreated(
        upload_id=upload.id,
        chunk_size=upload.chunk_size,
        received=sorted(upload.received_chunks),
        size=upload.size,
        state=upload.state,
    )


def _manifest_values(body: UploadBatchPreflight) -> tuple[int, int, int, int]:
    return (
        body.file_count,
        body.total_size,
        body.expected_extracted_size,
        body.largest_file_size,
    )


@router.put(
    "/api/datasets/{dataset_id}/upload-batches/{batch_id}",
    response_model=UploadBatchState,
)
async def begin_upload_batch(
    dataset_id: int,
    batch_id: UUID,
    body: UploadBatchPreflight,
    request: Request,
    session: Session,
    current_user: CurrentUserDep,
) -> UploadBatchState:
    """Create or replay one durable manifest before any file sessions."""

    await _dataset_or_404(
        session,
        dataset_id,
        current_user.id,
        for_update=True,
    )
    upload_batch = await session.get(UploadBatch, batch_id)
    if upload_batch is not None:
        if upload_batch.dataset_id != dataset_id:
            raise HTTPException(status_code=404, detail="업로드 배치를 찾을 수 없습니다.")
        persisted = (
            upload_batch.expected_file_count,
            upload_batch.expected_total_size,
            upload_batch.expected_extracted_size,
            upload_batch.largest_file_size,
        )
        if persisted != _manifest_values(body):
            raise HTTPException(
                status_code=409,
                detail="같은 업로드 배치의 파일 수 또는 용량이 변경되었습니다.",
            )
    else:
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
        upload_batch = UploadBatch(
            id=batch_id,
            dataset_id=dataset_id,
            expected_file_count=body.file_count,
            expected_total_size=body.total_size,
            expected_extracted_size=body.expected_extracted_size,
            largest_file_size=body.largest_file_size,
            state="open",
        )
        session.add(upload_batch)
        await session.commit()

    job_id = await session.scalar(
        select(UploadJob.id).where(UploadJob.upload_batch_id == batch_id)
    )
    return UploadBatchState(
        batch_id=upload_batch.id,
        state=upload_batch.state,
        job_id=job_id,
    )


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
        size=body.size,
        state="open",
    )


def _create_upload_directories(
    settings,
    upload_ids: list[int],
    *,
    exist_ok: bool = False,
) -> None:
    for upload_id in upload_ids:
        upload_directory(settings, upload_id).mkdir(
            parents=True,
            exist_ok=exist_ok,
        )


@router.post(
    "/api/datasets/{dataset_id}/uploads/batch",
    status_code=201,
    response_model=UploadBatchCreated,
)
async def create_upload_batch(
    dataset_id: int,
    body: UploadBatchCreate,
    request: Request,
    session: Session,
    current_user: CurrentUserDep,
) -> UploadBatchCreated:
    total_size = sum(item.size for item in body.files)
    total_file_count = sum(item.file_count for item in body.files)
    total_expected_extracted_size = sum(
        item.size
        if item.expected_extracted_size is None
        else item.expected_extracted_size
        for item in body.files
    )

    if body.batch_id is not None:
        upload_batch = await _upload_batch_or_404(
            session,
            dataset_id,
            body.batch_id,
            current_user.id,
            for_update=True,
        )
        if upload_batch.state != "open":
            raise HTTPException(status_code=409, detail="이미 봉인된 업로드 배치입니다.")
        file_keys = [upload_file_key(item.filename) for item in body.files]
        if len(file_keys) != len(set(file_keys)):
            raise HTTPException(
                status_code=422,
                detail="한 요청에 같은 상대 경로가 중복되었습니다.",
            )
        _check_capacity(
            request,
            size=max(item.size for item in body.files),
            file_count=total_file_count,
            expected_extracted_size=total_expected_extracted_size,
        )
        if any(item.size > upload_batch.largest_file_size for item in body.files):
            raise HTTPException(
                status_code=409,
                detail="업로드 파일 크기가 매니페스트와 일치하지 않습니다.",
            )

        existing = list(
            (
                await session.scalars(
                    select(UploadSession).where(
                        UploadSession.upload_batch_id == upload_batch.id,
                        UploadSession.file_key.in_(file_keys),
                    )
                )
            ).all()
        )
        existing_by_key = {upload.file_key: upload for upload in existing}
        missing: list[tuple[UploadCreate, str]] = []
        for item, file_key in zip(body.files, file_keys, strict=True):
            upload = existing_by_key.get(file_key)
            if upload is None:
                missing.append((item, file_key))
                continue
            if (
                upload.filename != item.filename
                or upload.size != item.size
                or upload.chunk_size != item.chunk_size
                or upload.kind != item.kind
            ):
                raise HTTPException(
                    status_code=409,
                    detail=f"{item.filename}: 기존 업로드 세션의 정보가 다릅니다.",
                )
            if upload.state != "open":
                raise HTTPException(
                    status_code=409,
                    detail=f"{item.filename}: 이미 닫힌 업로드 세션입니다.",
                )
        current_count, current_size = (
            await session.execute(
                select(
                    func.count(UploadSession.id),
                    func.coalesce(func.sum(UploadSession.size), 0),
                ).where(UploadSession.upload_batch_id == upload_batch.id)
            )
        ).one()
        next_count = int(current_count) + len(missing)
        next_size = int(current_size) + sum(item.size for item, _key in missing)
        if (
            next_count > upload_batch.expected_file_count
            or next_size > upload_batch.expected_total_size
        ):
            raise HTTPException(
                status_code=409,
                detail="업로드 세션이 매니페스트의 파일 수 또는 용량을 초과합니다.",
            )

        created_by_key: dict[str, UploadSession] = {}
        for item, file_key in missing:
            upload = UploadSession(
                dataset_id=dataset_id,
                upload_batch_id=upload_batch.id,
                file_key=file_key,
                filename=item.filename,
                size=item.size,
                chunk_size=item.chunk_size,
                received_chunks=[],
                kind=item.kind,
                state="open",
            )
            session.add(upload)
            created_by_key[file_key] = upload
        await session.flush()
        uploads = [
            existing_by_key.get(file_key) or created_by_key[file_key]
            for file_key in file_keys
        ]
        upload_ids = [upload.id for upload in uploads]
        # Rows are the durable checkpoint. If directory creation or the
        # response is interrupted, replaying this page reuses the same rows
        # and repairs any missing directories instead of duplicating files.
        await session.commit()
        await asyncio.to_thread(
            _create_upload_directories,
            request.app.state.settings,
            upload_ids,
            exist_ok=True,
        )
        return UploadBatchCreated(
            uploads=[_created_upload(upload) for upload in uploads]
        )

    await _dataset_or_404(session, dataset_id, current_user.id)
    _check_capacity(
        request,
        size=total_size,
        file_count=total_file_count,
        expected_extracted_size=total_expected_extracted_size,
    )
    await _check_user_quota(
        session,
        request,
        current_user.id,
        max(total_size, total_expected_extracted_size),
    )

    uploads = [
        UploadSession(
            dataset_id=dataset_id,
            filename=item.filename,
            size=item.size,
            chunk_size=item.chunk_size,
            received_chunks=[],
            kind=item.kind,
            state="open",
        )
        for item in body.files
    ]
    session.add_all(uploads)
    await session.flush()
    created = [
        _created_upload(upload)
        for upload in uploads
    ]
    upload_ids = [upload.upload_id for upload in created]
    # Keep the same row-before-directory publication order as single-session
    # creation so cleanup never mistakes an uncommitted upload for an orphan.
    await session.commit()
    try:
        await asyncio.to_thread(
            _create_upload_directories,
            request.app.state.settings,
            upload_ids,
        )
    except Exception:
        failed_uploads = await locked_uploads(session, upload_ids)
        for upload in failed_uploads:
            upload.state = "aborted"
        await session.commit()
        raise
    return UploadBatchCreated(uploads=created)


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


@router.post(
    "/api/datasets/{dataset_id}/uploads/chunks/batch",
    status_code=204,
)
async def put_chunk_batch(
    dataset_id: int,
    request: Request,
    session: Session,
    current_user: CurrentUserDep,
) -> Response:
    await _dataset_or_404(session, dataset_id, current_user.id)
    form, metadata, files = await _parse_chunk_batch(request)
    try:
        upload_ids = [chunk.upload_id for chunk in metadata.chunks]
        await _owned_batch_or_404(
            session,
            dataset_id,
            upload_ids,
            current_user.id,
        )
        await store_chunk_files(
            session,
            request.app.state.settings,
            dataset_id,
            [
                ChunkFileUpload(
                    upload_id=item.upload_id,
                    chunk_number=item.chunk_number,
                    declared_size=item.size,
                    source=file.file,
                )
                for item, file in zip(metadata.chunks, files, strict=True)
            ],
        )
    finally:
        await form.close()
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
    if request.app.state.auto_start_jobs and job.state == "queued":
        enqueue_upload_batch_job(request.app, job.id, body.upload_ids)
    return JobCreated(job_id=job.id)


@router.post(
    "/api/datasets/{dataset_id}/upload-batches/{batch_id}/complete",
    status_code=202,
    response_model=JobCreated,
)
async def finish_upload_manifest(
    dataset_id: int,
    batch_id: UUID,
    request: Request,
    session: Session,
    current_user: CurrentUserDep,
) -> JobCreated:
    await _upload_batch_or_404(
        session,
        dataset_id,
        batch_id,
        current_user.id,
    )
    job = await complete_upload_manifest(
        session,
        request.app.state.settings,
        dataset_id,
        batch_id,
    )
    if request.app.state.auto_start_jobs and job.state == "queued":
        enqueue_upload_batch_job(request.app, job.id, list(job.upload_ids or []))
    return JobCreated(job_id=job.id)
