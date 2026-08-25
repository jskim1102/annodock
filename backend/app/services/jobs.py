"""Persistent upload-job state and in-process task dispatch."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
                                Dataset.owner_id == dataset.owner_id,
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
    task = asyncio.create_task(
        run_upload_batch_job(
            application.state.settings,
            application.state.session_factory,
            job_id,
            upload_ids,
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
        recoverable: list[tuple[int, list[int]]] = []
        for job in jobs:
            upload_ids = list(job.upload_ids or [])
            if not upload_ids:
                continue
            job.state = "queued"
            recoverable.append((job.id, upload_ids))
        await session.commit()

    for job_id, upload_ids in recoverable:
        enqueue_upload_batch_job(application, job_id, upload_ids)
    return [job_id for job_id, _upload_ids in recoverable]
