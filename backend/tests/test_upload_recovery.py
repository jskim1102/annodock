from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from app.models import UploadJob
from app.services import jobs as jobs_service


pytestmark = pytest.mark.asyncio


async def test_startup_requeues_interrupted_and_queued_upload_jobs(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = await client.post(
        "/api/datasets",
        json={"name": f"test-upload-recovery-{uuid4().hex}"},
    )
    assert dataset.status_code == 201
    dataset_id = dataset.json()["id"]
    async with app.state.session_factory() as session:
        interrupted = UploadJob(
            dataset_id=dataset_id,
            kind="folder",
            state="running",
            phase="deriving",
            total=10,
            processed=6,
            failed=0,
            ingest_cursor=4,
            upload_ids=[101],
        )
        queued = UploadJob(
            dataset_id=dataset_id,
            kind="folder",
            state="queued",
            phase="uploading",
            total=0,
            processed=0,
            failed=0,
            upload_ids=[102],
        )
        done = UploadJob(
            dataset_id=dataset_id,
            kind="folder",
            state="done",
            phase="done",
            total=1,
            processed=1,
            failed=0,
            ingest_cursor=1,
            upload_ids=[103],
        )
        session.add_all([interrupted, queued, done])
        await session.commit()
        interrupted_id = interrupted.id
        queued_id = queued.id
        done_id = done.id

    dispatched: list[tuple[int, list[int]]] = []
    monkeypatch.setattr(
        jobs_service,
        "enqueue_upload_batch_job",
        lambda _app, job_id, upload_ids: dispatched.append(
            (job_id, upload_ids)
        ),
    )

    recovered = await jobs_service.recover_upload_jobs(app)

    assert recovered == [interrupted_id, queued_id]
    assert dispatched == [
        (interrupted_id, [101]),
        (queued_id, [102]),
    ]
    async with app.state.session_factory() as session:
        interrupted_row = await session.get(UploadJob, interrupted_id)
        queued_row = await session.get(UploadJob, queued_id)
        done_row = await session.get(UploadJob, done_id)
        assert interrupted_row is not None and interrupted_row.state == "queued"
        assert interrupted_row.ingest_cursor == 4
        assert queued_row is not None and queued_row.state == "queued"
        assert done_row is not None and done_row.state == "done"
