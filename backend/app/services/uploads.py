"""Resumable chunk storage and upload-session state transitions."""

from __future__ import annotations

import asyncio
import filecmp
import math
import os
from collections.abc import AsyncIterable
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Dataset, UploadJob, UploadSession
from app.services.storage import storage_root


def upload_directory(settings: Settings, upload_id: int) -> Path:
    return storage_root(settings.storage_dir) / "uploads" / str(upload_id)


def assembled_upload_path(settings: Settings, upload_id: int) -> Path:
    return upload_directory(settings, upload_id) / "source"


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
            .where(UploadSession.id.in_(upload_ids))
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
    chunk_directory = upload_directory(settings, upload_id) / "chunks"
    chunk_directory.mkdir(parents=True, exist_ok=True)
    target = chunk_directory / f"{chunk_number}.part"
    temporary = chunk_directory / f".{chunk_number}.{uuid4().hex}.tmp"
    output = await asyncio.to_thread(temporary.open, "xb")
    received_size = 0
    try:
        try:
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
        finally:
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
        temporary.unlink(missing_ok=True)


def _assemble_chunks(
    settings: Settings,
    upload: UploadSession,
) -> Path:
    directory = upload_directory(settings, upload.id)
    target = directory / "source"
    temporary = directory / f".source.{uuid4().hex}.tmp"
    with temporary.open("wb") as output:
        for chunk_number in range(expected_chunk_count(upload)):
            chunk_path = directory / "chunks" / f"{chunk_number}.part"
            with chunk_path.open("rb") as source:
                shutil_copyfileobj(source, output)
    if temporary.stat().st_size != upload.size:
        temporary.unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail="조립된 파일 크기가 다릅니다.")
    os.replace(temporary, target)
    return target


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

    for upload in uploads:
        await asyncio.to_thread(_assemble_chunks, settings, upload)
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
