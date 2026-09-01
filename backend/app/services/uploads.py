"""Resumable chunk storage and upload-session state transitions."""

from __future__ import annotations

import asyncio
import filecmp
import hashlib
import math
import os
import shutil
from collections.abc import AsyncIterable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import Integer, any_, bindparam, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Dataset, UploadBatch, UploadJob, UploadSession
from app.services.storage import (
    StorageBoundaryError,
    finalize_staged_deletion,
    restore_staged_deletion,
    stage_dataset_deletion_async,
    storage_root,
)


def upload_directory(settings: Settings, upload_id: int) -> Path:
    return storage_root(settings.storage_dir) / "uploads" / str(upload_id)


def existing_upload_directories(
    settings: Settings,
    upload_ids: Iterable[int],
) -> list[Path]:
    """Resolve only upload directories which actually remain on disk.

    Completed large uploads can retain hundreds of thousands of session rows
    after their temporary directories have been reclaimed.  Scanning the
    uploads root once keeps deletion proportional to the paths that still
    exist instead of issuing one filesystem lookup per historical row.
    """

    target_names = {str(upload_id) for upload_id in upload_ids}
    if not target_names:
        return []
    upload_root = storage_root(settings.storage_dir) / "uploads"
    if not upload_root.exists():
        return []
    if upload_root.is_symlink() or not upload_root.is_dir():
        raise StorageBoundaryError("uploads must be a real directory")
    return sorted(
        (
            entry
            for entry in upload_root.iterdir()
            if entry.name in target_names
        ),
        key=lambda entry: int(entry.name),
    )


def assembled_upload_path(settings: Settings, upload_id: int) -> Path:
    return upload_directory(settings, upload_id) / "source"


def upload_file_key(filename: str) -> str:
    """Return the stable identity used for idempotent manifest session pages."""

    return hashlib.sha256(filename.encode("utf-8")).hexdigest()


def expected_chunk_count(upload: UploadSession) -> int:
    if upload.size == 0:
        return 0
    return math.ceil(upload.size / upload.chunk_size)


def expected_chunk_size(upload: UploadSession, chunk_number: int) -> int:
    count = expected_chunk_count(upload)
    if chunk_number < 0 or chunk_number >= count:
        raise HTTPException(status_code=416, detail="청크 번호가 범위를 벗어났습니다.")
    if chunk_number == count - 1:
        return upload.size - chunk_number * upload.chunk_size
    return upload.chunk_size


def upload_ids_match(upload_ids: Sequence[int]):
    """Match UploadSession.id against ids as ONE array bind parameter.

    asyncpg rejects statements with more than 32767 parameters, so
    ``id.in_(ids)`` breaks for large batches; ``id = ANY($1::int[])``
    stays a single parameter at any batch size.
    """
    return UploadSession.id == any_(
        bindparam(None, value=list(upload_ids), type_=ARRAY(Integer))
    )


async def locked_upload(
    session: AsyncSession,
    upload_id: int,
) -> UploadSession:
    upload = await session.scalar(
        select(UploadSession)
        .where(UploadSession.id == upload_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if upload is None:
        raise HTTPException(status_code=404, detail="업로드 세션을 찾을 수 없습니다.")
    return upload


async def locked_uploads(
    session: AsyncSession,
    upload_ids: list[int],
) -> list[UploadSession]:
    uploads = (
        await session.scalars(
            select(UploadSession)
            .where(upload_ids_match(upload_ids))
            .order_by(UploadSession.id)
            .with_for_update()
        )
    ).all()
    if len(uploads) != len(upload_ids):
        raise HTTPException(status_code=404, detail="업로드 세션을 찾을 수 없습니다.")
    return list(uploads)


def require_open(upload: UploadSession) -> None:
    if upload.state == "aborted":
        raise HTTPException(
            status_code=409,
            detail="중단된 업로드입니다. 처음부터 다시 시작하세요.",
        )
    if upload.state != "open":
        raise HTTPException(status_code=409, detail="이미 완료된 업로드입니다.")


async def abort_upload(
    session: AsyncSession,
    settings: Settings,
    upload_id: int,
) -> None:
    """Abort one unconsumed upload and atomically quarantine its files."""
    upload = await locked_upload(session, upload_id)
    if upload.state not in {"open", "aborted"}:
        raise HTTPException(
            status_code=409,
            detail="이미 처리 중이거나 완료된 업로드는 중단할 수 없습니다.",
        )
    staged = await stage_dataset_deletion_async(
        settings.storage_dir,
        upload_directory(settings, upload_id),
    )
    try:
        if upload.state == "open":
            upload.state = "aborted"
            await session.commit()
    except BaseException as error:
        restore_staged_deletion(staged)
        try:
            await session.rollback()
        except BaseException as rollback_error:
            error.add_note(
                "upload abort rollback also failed: "
                f"{type(rollback_error).__name__}"
            )
        raise
    await asyncio.to_thread(finalize_staged_deletion, staged)


def _publish_chunk(temporary: Path, target: Path) -> None:
    if target.exists():
        if filecmp.cmp(target, temporary, shallow=False):
            temporary.unlink(missing_ok=True)
            return
        raise HTTPException(
            status_code=409,
            detail="같은 번호의 청크 내용이 기존 전송과 다릅니다.",
        )
    os.replace(temporary, target)


def _refresh_upload_activity(path: Path) -> None:
    try:
        os.utime(path, None)
    except FileNotFoundError:
        # Abort/GC may have quarantined the directory.  The final row-state
        # validation below turns that race into the canonical 409 response.
        pass


@dataclass(frozen=True, slots=True)
class ChunkFileUpload:
    upload_id: int
    chunk_number: int
    declared_size: int
    source: BinaryIO


@dataclass(frozen=True, slots=True)
class _StagedChunk:
    upload: ChunkFileUpload
    temporary: Path
    target: Path


def _stage_chunk_files(chunks: Sequence[_StagedChunk]) -> None:
    for chunk in chunks:
        chunk.upload.source.seek(0)
        received_size = 0
        with chunk.temporary.open("xb") as output:
            while content := chunk.upload.source.read(1024 * 1024):
                received_size += len(content)
                if received_size > chunk.upload.declared_size:
                    raise HTTPException(
                        status_code=413,
                        detail="청크 본문이 허용 크기를 초과했습니다.",
                    )
                output.write(content)
        if received_size != chunk.upload.declared_size:
            raise HTTPException(
                status_code=422,
                detail=(
                    "청크 크기가 올바르지 않습니다. "
                    f"필요 {chunk.upload.declared_size}바이트"
                ),
            )


def _publish_chunk_files(chunks: Sequence[_StagedChunk]) -> None:
    # Detect semantic conflicts across the full request before publishing the
    # first new file. A later filesystem failure can still leave an immutable
    # target behind; replay compares it byte-for-byte and converges safely.
    for chunk in chunks:
        if chunk.target.exists() and not filecmp.cmp(
            chunk.target,
            chunk.temporary,
            shallow=False,
        ):
            raise HTTPException(
                status_code=409,
                detail="같은 번호의 청크 내용이 기존 전송과 다릅니다.",
            )
    for chunk in chunks:
        _publish_chunk(chunk.temporary, chunk.target)


def _create_chunk_directories(directories: Sequence[Path]) -> None:
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)


def _remove_staged_chunks(chunks: Sequence[_StagedChunk]) -> None:
    for chunk in chunks:
        chunk.temporary.unlink(missing_ok=True)


async def store_chunk_files(
    session: AsyncSession,
    settings: Settings,
    dataset_id: int,
    chunks: Sequence[ChunkFileUpload],
) -> None:
    """Store a bounded group of chunks with one row-lock/commit cycle."""

    if not chunks or len(chunks) != len(
        {(chunk.upload_id, chunk.chunk_number) for chunk in chunks}
    ):
        raise HTTPException(
            status_code=422,
            detail="청크 배치가 비어 있거나 중복되었습니다.",
        )
    upload_ids = sorted({chunk.upload_id for chunk in chunks})
    uploads = await locked_uploads(session, upload_ids)
    uploads_by_id = {upload.id: upload for upload in uploads}
    for chunk in chunks:
        upload = uploads_by_id[chunk.upload_id]
        if upload.dataset_id != dataset_id:
            raise HTTPException(
                status_code=409,
                detail="서로 다른 데이터셋의 청크는 한 배치로 묶을 수 없습니다.",
            )
        require_open(upload)
        if expected_chunk_size(upload, chunk.chunk_number) != chunk.declared_size:
            raise HTTPException(
                status_code=409,
                detail="업로드 세션의 청크 크기가 변경되었습니다.",
            )

    directories = {
        upload_id: upload_directory(settings, upload_id) / "chunks"
        for upload_id in upload_ids
    }
    await asyncio.to_thread(_create_chunk_directories, list(directories.values()))
    staged = [
        _StagedChunk(
            upload=chunk,
            temporary=(
                directories[chunk.upload_id]
                / f".{chunk.chunk_number}.{uuid4().hex}.tmp"
            ),
            target=directories[chunk.upload_id] / f"{chunk.chunk_number}.part",
        )
        for chunk in chunks
    ]
    try:
        await asyncio.to_thread(_stage_chunk_files, staged)
        await asyncio.to_thread(_publish_chunk_files, staged)
        received_by_upload = {
            upload.id: set(upload.received_chunks)
            for upload in uploads
        }
        for chunk in chunks:
            received_by_upload[chunk.upload_id].add(chunk.chunk_number)
        for upload in uploads:
            upload.received_chunks = sorted(received_by_upload[upload.id])
        # Targets are immutable checkpoints. If this commit is interrupted, a
        # replay accepts their identical bytes and restores the row state.
        await session.commit()
    finally:
        await asyncio.to_thread(_remove_staged_chunks, staged)


async def inspect_chunk(
    session: AsyncSession,
    upload_id: int,
    chunk_number: int,
) -> int:
    upload = await session.get(UploadSession, upload_id)
    if upload is None:
        raise HTTPException(status_code=404, detail="업로드 세션을 찾을 수 없습니다.")
    require_open(upload)
    return expected_chunk_size(upload, chunk_number)


async def store_chunk_stream(
    session: AsyncSession,
    settings: Settings,
    upload_id: int,
    chunk_number: int,
    stream: AsyncIterable[bytes],
    expected_size: int,
) -> None:
    # Validate under the row lock, then leave a filesystem activity marker and
    # release the transaction before waiting on the request body.  The sweeper
    # uses the marker mtime to distinguish an active transfer from a stale one.
    upload = await locked_upload(session, upload_id)
    require_open(upload)
    if expected_chunk_size(upload, chunk_number) != expected_size:
        raise HTTPException(
            status_code=409,
            detail="업로드 세션의 청크 크기가 변경되었습니다.",
        )
    chunk_directory = upload_directory(settings, upload_id) / "chunks"
    chunk_directory.mkdir(parents=True, exist_ok=True)
    target = chunk_directory / f"{chunk_number}.part"
    temporary = chunk_directory / f".{chunk_number}.{uuid4().hex}.tmp"
    output = await asyncio.to_thread(temporary.open, "xb")
    received_size = 0
    try:
        await session.commit()
        async for content in stream:
            if not content:
                continue
            if received_size + len(content) > expected_size:
                raise HTTPException(
                    status_code=413,
                    detail="청크 본문이 허용 크기를 초과했습니다.",
                )
            await asyncio.to_thread(output.write, content)
            received_size += len(content)
            await asyncio.to_thread(_refresh_upload_activity, temporary)
        if not output.closed:
            await asyncio.to_thread(output.close)
        if received_size != expected_size:
            raise HTTPException(
                status_code=422,
                detail=(
                    "청크 크기가 올바르지 않습니다. "
                    f"필요 {expected_size}바이트"
                ),
            )
        upload = await locked_upload(session, upload_id)
        require_open(upload)
        if expected_chunk_size(upload, chunk_number) != expected_size:
            raise HTTPException(
                status_code=409,
                detail="업로드 세션의 청크 크기가 변경되었습니다.",
            )
        await asyncio.to_thread(_publish_chunk, temporary, target)
        upload.received_chunks = sorted(
            {*upload.received_chunks, chunk_number}
        )
        await session.commit()
    finally:
        if not output.closed:
            await asyncio.to_thread(output.close)
        temporary.unlink(missing_ok=True)


def _assemble_chunks(
    settings: Settings,
    upload: UploadSession,
) -> Path:
    directory = upload_directory(settings, upload.id)
    # Session rows are the durable checkpoint and are intentionally committed
    # before directory publication. A crash in that narrow window is repaired
    # here, including zero-byte files that never send a chunk request.
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "source"
    chunk_directory = directory / "chunks"
    if target.exists():
        if target.stat().st_size != upload.size:
            raise HTTPException(status_code=409, detail="조립된 파일 크기가 다릅니다.")
        # A worker can stop after publishing the source but before reclaiming
        # its chunks. The immutable source is the checkpoint in that case.
        if chunk_directory.exists():
            shutil.rmtree(chunk_directory)
        return target

    chunk_count = expected_chunk_count(upload)
    if chunk_count == 1:
        chunk_path = chunk_directory / "0.part"
        if chunk_path.stat().st_size != upload.size:
            raise HTTPException(status_code=409, detail="조립된 파일 크기가 다릅니다.")
        os.replace(chunk_path, target)
        chunk_directory.rmdir()
        return target

    temporary = directory / f".source.{uuid4().hex}.tmp"
    try:
        with temporary.open("wb") as output:
            for chunk_number in range(chunk_count):
                chunk_path = chunk_directory / f"{chunk_number}.part"
                with chunk_path.open("rb") as source:
                    shutil_copyfileobj(source, output)
        if temporary.stat().st_size != upload.size:
            raise HTTPException(status_code=409, detail="조립된 파일 크기가 다릅니다.")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    if chunk_directory.exists():
        shutil.rmtree(chunk_directory)
    return target


async def assemble_uploads(
    settings: Settings,
    uploads: list[UploadSession],
) -> None:
    """Publish immutable upload sources outside the completion request."""

    for upload in uploads:
        await asyncio.to_thread(_assemble_chunks, settings, upload)


def shutil_copyfileobj(source, destination, length: int = 1024 * 1024) -> None:
    while chunk := source.read(length):
        destination.write(chunk)


async def complete_upload(
    session: AsyncSession,
    settings: Settings,
    upload_id: int,
) -> UploadJob:
    upload = await session.get(UploadSession, upload_id)
    if upload is None:
        raise HTTPException(status_code=404, detail="업로드 세션을 찾을 수 없습니다.")
    return await complete_upload_batch(
        session,
        settings,
        upload.dataset_id,
        [upload_id],
    )


async def complete_upload_batch(
    session: AsyncSession,
    settings: Settings,
    dataset_id: int,
    upload_ids: list[int],
) -> UploadJob:
    if not upload_ids or len(upload_ids) != len(set(upload_ids)):
        raise HTTPException(
            status_code=422,
            detail="업로드 세션 목록이 비어 있거나 중복되었습니다.",
        )
    uploads = await locked_uploads(session, upload_ids)
    completed_states = {upload.state for upload in uploads}
    if completed_states == {"complete"}:
        jobs = list(
            (
                await session.scalars(
                    select(UploadJob)
                    .where(UploadJob.dataset_id == dataset_id)
                    .order_by(UploadJob.id.desc())
                )
            ).all()
        )
        requested = set(upload_ids)
        for existing in jobs:
            persisted = list(existing.upload_ids or [])
            if len(persisted) == len(requested) and set(persisted) == requested:
                return existing
        raise HTTPException(
            status_code=409,
            detail="완료된 업로드 세션에 대응하는 작업을 찾을 수 없습니다.",
        )
    if "complete" in completed_states:
        raise HTTPException(
            status_code=409,
            detail="일부만 완료된 업로드 세션 목록은 다시 완료할 수 없습니다.",
        )

    for upload in uploads:
        if upload.dataset_id != dataset_id:
            raise HTTPException(
                status_code=409,
                detail="서로 다른 데이터셋의 업로드는 한 배치로 묶을 수 없습니다.",
            )
        require_open(upload)
        expected = set(range(expected_chunk_count(upload)))
        missing = sorted(expected - set(upload.received_chunks))
        if missing:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "누락된 청크가 있습니다.",
                    "upload_id": upload.id,
                    "missing": missing,
                },
            )

    dataset = await session.get(Dataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="데이터셋을 찾을 수 없습니다.")
    job = UploadJob(
        dataset_id=dataset_id,
        kind=(
            "zip"
            if any(upload.kind == "zip" for upload in uploads)
            else "folder"
            if len(uploads) > 1
            else uploads[0].kind
        ),
        state="queued",
        phase="uploading",
        total=0,
        processed=0,
        failed=0,
        upload_ids=[upload.id for upload in uploads],
    )
    session.add(job)
    for upload in uploads:
        upload.state = "complete"
    if dataset.status == "pending":
        dataset.status = "processing"
    await session.commit()
    await session.refresh(job)
    return job


async def complete_upload_manifest(
    session: AsyncSession,
    settings: Settings,
    dataset_id: int,
    upload_batch_id: UUID,
) -> UploadJob:
    """Atomically seal one durable manifest and return its exactly-once job."""

    upload_batch = await session.scalar(
        select(UploadBatch)
        .where(
            UploadBatch.id == upload_batch_id,
            UploadBatch.dataset_id == dataset_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if upload_batch is None:
        raise HTTPException(status_code=404, detail="업로드 배치를 찾을 수 없습니다.")

    existing_job = await session.scalar(
        select(UploadJob).where(UploadJob.upload_batch_id == upload_batch.id)
    )
    if existing_job is not None:
        return existing_job
    if upload_batch.state != "open":
        raise HTTPException(status_code=409, detail="이미 봉인된 업로드 배치입니다.")

    uploads = list(
        (
            await session.scalars(
                select(UploadSession)
                .where(UploadSession.upload_batch_id == upload_batch.id)
                .order_by(UploadSession.id)
                .with_for_update()
            )
        ).all()
    )
    actual_file_count = len(uploads)
    actual_total_size = sum(upload.size for upload in uploads)
    if (
        actual_file_count != upload_batch.expected_file_count
        or actual_total_size != upload_batch.expected_total_size
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "업로드 배치가 아직 완전하지 않습니다. "
                f"파일 {actual_file_count:,}/{upload_batch.expected_file_count:,}개, "
                f"용량 {actual_total_size:,}/{upload_batch.expected_total_size:,}바이트"
            ),
        )

    for upload in uploads:
        require_open(upload)
        expected = set(range(expected_chunk_count(upload)))
        missing = sorted(expected - set(upload.received_chunks))
        if missing:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "누락된 청크가 있습니다.",
                    "upload_id": upload.id,
                    "missing": missing,
                },
            )

    dataset = await session.get(Dataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="데이터셋을 찾을 수 없습니다.")
    if dataset.upload_group_id is not None:
        raise HTTPException(
            status_code=409,
            detail="자동 분할된 데이터셋에는 새 업로드를 추가할 수 없습니다.",
        )

    job = UploadJob(
        dataset_id=dataset_id,
        upload_batch_id=upload_batch.id,
        kind=(
            "zip"
            if any(upload.kind == "zip" for upload in uploads)
            else "folder"
            if len(uploads) > 1
            else uploads[0].kind
        ),
        state="queued",
        phase="uploading",
        total=0,
        processed=0,
        failed=0,
        upload_ids=[upload.id for upload in uploads],
    )
    session.add(job)
    upload_batch.state = "sealed"
    for upload in uploads:
        upload.state = "complete"
    if dataset.status == "pending":
        dataset.status = "processing"
    await session.commit()
    await session.refresh(job)
    return job
