"""Persistent upload-job state and in-process task dispatch."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import is_retryable_lock_error
from app.models import Dataset, ImportIssue, UploadJob


PhaseObserver = Callable[[str], None]


async def transition_job(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: int,
    phase: str,
    *,
    state: str | None = None,
    total: int | None = None,
    processed: int | None = None,
    failed: int | None = None,
    image_total: int | None = None,
    image_processed: int | None = None,
    observer: PhaseObserver | None = None,
) -> None:
    async with session_factory() as session:
        job = await session.get(UploadJob, job_id)
        if job is None:
            raise LookupError(f"upload job {job_id} does not exist")
        job.phase = phase
        if state is not None:
            job.state = state
        if total is not None:
            job.total = total
        if processed is not None:
            job.processed = processed
        if failed is not None:
            job.failed = failed
        if image_total is not None:
            job.image_total = image_total
        if image_processed is not None:
            job.image_processed = image_processed
        await session.commit()
    if observer is not None:
        observer(phase)


async def fail_job(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: int,
    detail: str,
    *,
    path: str = "",
) -> None:
    async with session_factory() as session:
        job = await session.get(UploadJob, job_id)
        if job is None:
            return
        if job.state == "failed":
            return
        dataset = await session.get(Dataset, job.dataset_id)
        job.state = "failed"
        job.failed += 1
        if dataset is not None:
            if dataset.upload_group_id is None:
                failed_datasets = [dataset]
            else:
                failed_datasets = list(
                    (
                        await session.scalars(
                            select(Dataset).where(
                                Dataset.workspace_id == dataset.workspace_id,
                                Dataset.upload_group_id
                                == dataset.upload_group_id,
                            )
                        )
                    ).all()
                )
            for failed_dataset in failed_datasets:
                if failed_dataset.status != "ready":
                    failed_dataset.status = "failed"
        session.add(
            ImportIssue(
                job_id=job_id,
                kind="rejected_file",
                path=path,
                detail=detail[:2000],
            )
        )
        await session.commit()


async def requeue_upload_after_lock_failure(
    session_factory: async_sessionmaker[AsyncSession], job_id: int, detail: str,
) -> None:
    """Persist a bounded retry budget so a process restart cannot reset it."""
    async with session_factory() as session:
        job = await session.scalar(select(UploadJob).where(UploadJob.id == job_id).with_for_update())
        if job is None or job.state not in {"queued", "running"}:
            return
        retries = await session.scalar(select(func.count(ImportIssue.id)).where(
            ImportIssue.job_id == job_id, ImportIssue.kind == "upload_lock_retry",
        ))
        if (retries or 0) < 3:
            job.state = "queued"
            job.phase = "queued"
            session.add(ImportIssue(job_id=job_id, kind="upload_lock_retry", path="",
                detail=f"잠금 오류로 업로드를 다시 처리합니다 ({(retries or 0) + 1}/3): {detail}"[:2000]))
            await session.commit()
            return
    await fail_job(session_factory, job_id, f"잠금 오류 재시도 3회 초과. 업로드 파일을 보존했습니다: {detail}")


async def claim_upload_job(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: int,
) -> bool:
    """Claim one queued job so duplicate in-process dispatch is harmless."""

    async with session_factory() as session:
        job = await session.scalar(
            select(UploadJob)
            .where(UploadJob.id == job_id)
            .with_for_update()
        )
        if job is None or job.state != "queued":
            return False
        job.state = "running"
        job.phase = "assembling"
        await session.commit()
        return True


def enqueue_upload_job(
    application: FastAPI,
    job_id: int,
    upload_id: int,
) -> None:
    enqueue_upload_batch_job(application, job_id, [upload_id])


def enqueue_upload_batch_job(
    application: FastAPI,
    job_id: int,
    upload_ids: list[int],
) -> None:
    from app.services.ingest import run_upload_batch_job

    task_name = f"dataset-ingest-{job_id}"
    if any(
        not task.done() and task.get_name() == task_name
        for task in application.state.job_tasks
    ):
        return
    async def dispatch() -> None:
        # Keep the retry chain in the existing tracked task: duplicate enqueue
        # remains harmless and shutdown cancels the entire chain together.
        while True:
            try:
                await run_upload_batch_job(
                    application.state.settings, application.state.session_factory, job_id, upload_ids,
                )
                return
            except Exception as error:
                if not is_retryable_lock_error(error):
                    raise
                async with application.state.session_factory() as session:
                    state = await session.scalar(select(UploadJob.state).where(UploadJob.id == job_id))
                if state != "queued":
                    return

    task = asyncio.create_task(dispatch(), name=task_name)
    application.state.job_tasks.add(task)
    task.add_done_callback(application.state.job_tasks.discard)


def enqueue_dataset_merge_job(application: FastAPI, job_id: int) -> None:
    from app.services.dataset_merge import run_dataset_merge_job

    task_name = f"dataset-merge-{job_id}"
    if any(
        not task.done() and task.get_name() == task_name
        for task in application.state.job_tasks
    ):
        return
    task = asyncio.create_task(
        run_dataset_merge_job(
            application.state.settings,
            application.state.session_factory,
            job_id,
        ),
        name=task_name,
    )
    application.state.job_tasks.add(task)
    task.add_done_callback(application.state.job_tasks.discard)


async def recover_upload_jobs(application: FastAPI) -> list[int]:
    """Requeue durable upload work left queued or running by a restart."""

    async with application.state.session_factory() as session:
        jobs = list(
            (
                await session.scalars(
                    select(UploadJob)
                    .where(UploadJob.state.in_(("queued", "running")))
                    .order_by(UploadJob.id)
                    .with_for_update()
                )
            ).all()
        )
        recoverable_uploads: list[tuple[int, list[int]]] = []
        recoverable_merges: list[int] = []
        for job in jobs:
            if job.kind == "dataset_merge":
                job.state = "queued"
                job.phase = "queued"
                recoverable_merges.append(job.id)
                continue
            upload_ids = list(job.upload_ids or [])
            if not upload_ids:
                continue
            job.state = "queued"
            recoverable_uploads.append((job.id, upload_ids))
        await session.commit()

    for job_id, upload_ids in recoverable_uploads:
        enqueue_upload_batch_job(application, job_id, upload_ids)
    for job_id in recoverable_merges:
        enqueue_dataset_merge_job(application, job_id)
    return [
        *[job_id for job_id, _upload_ids in recoverable_uploads],
        *recoverable_merges,
    ]
