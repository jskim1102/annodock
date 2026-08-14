"""Persistent upload-job state and in-process task dispatch."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import FastAPI
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
        if dataset is not None and dataset.status != "ready":
            dataset.status = "failed"
        session.add(
            ImportIssue(
                job_id=job_id,
                kind="rejected_file",
                path=path,
                detail=detail[:2000],
            )
        )
        await session.commit()


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

    task = asyncio.create_task(
        run_upload_batch_job(
            application.state.settings,
            application.state.session_factory,
            job_id,
            upload_ids,
        ),
        name=f"dataset-ingest-{job_id}",
    )
    application.state.job_tasks.add(task)
    task.add_done_callback(application.state.job_tasks.discard)
