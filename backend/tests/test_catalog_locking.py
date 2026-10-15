"""Cross real upload transactions with the other catalog writers."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import io
from uuid import uuid4
import zipfile

import pytest
from PIL import Image as PillowImage
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import is_lock_not_available
from app.models import Dataset, Image, UploadJob
from app.services.ingest import run_upload_batch_job
from app.services.uploads import upload_directory
from tests.test_annotation_version import _finish_tasks
from tests.test_class_resolution import _incoming_files, _pause_for_resolution
from tests.test_dataset_merge import create_ready_dataset


pytestmark = pytest.mark.asyncio


async def _zip_upload(client, dataset_id, *, rename=False):
    image = io.BytesIO()
    PillowImage.new("RGB", (32, 24), (40, 80, 120)).save(image, "JPEG")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as output:
        output.writestr("obj.names", "person\norklift\n" if rename else "person\nforklift\n")
        output.writestr("images/train/incoming.jpg", image.getvalue())
        output.writestr("labels/train/incoming.txt", "1 0.5 0.5 0.2 0.2\n")
    payload = archive.getvalue()
    created = await client.post(f"/api/datasets/{dataset_id}/uploads", json={
        "filename": "incoming.zip", "size": len(payload), "chunk_size": len(payload),
        "kind": "zip", "expected_extracted_size": len(payload),
    })
    assert created.status_code == 201, created.text
    upload_id = created.json()["upload_id"]
    sent = await client.put(f"/api/uploads/{upload_id}/chunks/0", content=payload)
    assert sent.status_code == 204, sent.text
    completed = await client.post(f"/api/datasets/{dataset_id}/upload-batches/complete", json={"upload_ids": [upload_id]})
    assert completed.status_code == 202, completed.text
    return completed.json()["job_id"], upload_id, payload


@pytest.mark.parametrize("scenario", [
    "planning-merge", "checkpoint-extract", "rename-sibling-merge",
    "planning-class-resolution", "planning-extend",
])
async def test_upload_catalog_lock_order_preserves_input(client, app, tmp_path, monkeypatch, scenario):
    project = await client.post("/api/projects", json={
        "name": f"test-catalog-lock-project-{uuid4().hex}",
        "classes": [{"name": "person", "color": "#EF4444"}, {"name": "forklift", "color": "#F59E0B"}],
    })
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    dataset_ids = [await create_ready_dataset(
        client, app, project_id=project_id, suffix=f"catalog-lock-{index}",
        classes={0: "person", 1: "forklift"}, annotation_class_id=1,
        content=f"existing-{index}".encode(),
    ) for index in range(3)]
    upload_dataset = dataset_ids[2] if scenario == "planning-extend" else dataset_ids[0]
    result_name = f"test-catalog-lock-result-{uuid4().hex}"
    expected_success = 201
    if scenario == "planning-extend":
        merged = await client.post("/api/datasets/merge", json={"name": result_name, "dataset_ids": dataset_ids[:2]})
        assert merged.status_code == 201, merged.text
        request_url = f"/api/datasets/{merged.json()['id']}/merge-sources"
        request_body = {"dataset_ids": [upload_dataset]}
        expected_success = 200
    elif scenario == "checkpoint-extract":
        request_url = "/api/datasets/extract"
        request_body = {"name": result_name, "dataset_ids": [upload_dataset], "class_ids": [1]}
    else:
        request_url = "/api/datasets/merge"
        request_body = {"name": result_name, "dataset_ids": dataset_ids[1:] if scenario == "rename-sibling-merge" else dataset_ids[:2]}

    job_id, upload_id, payload = await _zip_upload(client, upload_dataset, rename=scenario == "rename-sibling-merge")

    async def ingest():
        await run_upload_batch_job(app.state.settings, app.state.session_factory, job_id, [upload_id])

    if scenario == "rename-sibling-merge":
        await ingest()
        waiting = await client.get(f"/api/jobs/{job_id}")
        assert waiting.json()["state"] == "awaiting_class_resolution"
        accepted = await client.post(f"/api/jobs/{job_id}/class-resolution", json={
            "revision": waiting.json()["class_resolution"]["revision"],
            "resolutions": [{"key": "class:1", "action": "use_upload"}],
        })
        assert accepted.status_code == 202, accepted.text
    elif scenario == "planning-class-resolution":
        async with app.state.session_factory() as session:
            resolution_job = UploadJob(dataset_id=upload_dataset, kind="folder", state="queued", phase="uploading",
                                       total=0, processed=0, failed=0, upload_ids=[])
            session.add(resolution_job)
            await session.commit()
            resolution_job_id = resolution_job.id
        resolution_source = tmp_path / "resolution-source"
        resolution_source.mkdir()
        plan = await _pause_for_resolution(app, resolution_job_id, _incoming_files(resolution_source))
        request_url = f"/api/jobs/{resolution_job_id}/class-resolution"
        request_body = {"revision": plan["revision"], "resolutions": [{"key": "class:1", "action": "use_project"}]}
        expected_success = 202

    # Context variables also identify HTTP handlers run in middleware child tasks.
    actor = ContextVar("catalog_lock_actor", default=None)
    locked, attempted, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    project_locks = 0
    pause_at = 2 if scenario in ("checkpoint-extract", "rename-sibling-merge") else 1
    pids = {}
    deadlocks = []
    evidence = {"other_dataset_locked_before_project": False}
    original_scalar, original_execute = AsyncSession.scalar, AsyncSession.execute

    async def at_lock(original, session, statement, args, kwargs):
        nonlocal project_locks
        who = actor.get()
        lock = getattr(statement, "_for_update_arg", None)
        is_write = getattr(statement, "is_select", False) and lock is not None and not lock.read
        sql = str(statement).lower()
        project_lock = is_write and "from projects" in sql
        if who == "other" and project_lock and not attempted.is_set():
            pids["other"] = await original_scalar(session, sa.text("SELECT pg_backend_pid()"))
            attempted.set()
        try:
            result = await original(session, statement, *args, **kwargs)
        except BaseException as error:
            cause = error
            while cause is not None:
                if getattr(cause, "sqlstate", None) == "40P01" or getattr(cause, "pgcode", None) == "40P01":
                    deadlocks.append((who, str(error)))
                    break
                cause = cause.__cause__
            raise
        if who == "other" and is_write and "from datasets" in sql and not attempted.is_set():
            evidence["other_dataset_locked_before_project"] = True
        if who == "ingest" and project_lock:
            project_locks += 1
            if project_locks == pause_at:
                pids["ingest"] = await original_scalar(session, sa.text("SELECT pg_backend_pid()"))
                locked.set()  # The real SELECT has returned: the row lock is held.
                await asyncio.wait_for(release.wait(), timeout=20)
        return result

    async def scalar(session, statement, *args, **kwargs):
        return await at_lock(original_scalar, session, statement, args, kwargs)

    async def execute(session, statement, *args, **kwargs):
        return await at_lock(original_execute, session, statement, args, kwargs)

    monkeypatch.setattr(AsyncSession, "scalar", scalar)
    monkeypatch.setattr(AsyncSession, "execute", execute)

    async def as_actor(name, operation):
        token = actor.set(name)
        try:
            return await operation()
        finally:
            actor.reset(token)

    async def other_operation():
        return await client.post(request_url, json=request_body)

    tasks = [asyncio.create_task(as_actor("ingest", ingest))]
    try:
        await asyncio.wait_for(locked.wait(), timeout=10)
        source = upload_directory(app.state.settings, upload_id) / "source"
        assert source.read_bytes() == payload
        tasks.append(asyncio.create_task(as_actor("other", other_operation)))
        await asyncio.wait_for(attempted.wait(), timeout=10)

        async def observe_wait():
            async with app.state.session_factory() as session:
                while not tasks[1].done():
                    if await original_scalar(session, sa.text(
                        "SELECT CAST(:ingest AS INTEGER) = ANY(pg_blocking_pids(CAST(:other AS INTEGER)))"
                    ), pids):
                        return True
            return False  # A documented NOWAIT conflict is also safe to retry.

        evidence["other_waited_for_project"] = await asyncio.wait_for(observe_wait(), timeout=5)
        release.set()
        upload_result, response = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=20)
        assert deadlocks == [], f"{scenario}: {deadlocks}; locks={evidence}"
        if isinstance(upload_result, BaseException):
            assert is_lock_not_available(upload_result), repr(upload_result)
            assert source.read_bytes() == payload
            async with app.state.session_factory() as session:
                assert (await session.get(UploadJob, job_id)).state != "failed"
            await ingest()
        assert not isinstance(response, BaseException), repr(response)
        assert response.status_code in (expected_success, 409), response.text
        async with app.state.session_factory() as session:
            job = await session.get(UploadJob, job_id)
            dataset = await session.get(Dataset, upload_dataset)
            assert job is not None and job.state == "done" and job.ingest_cursor == 1
            assert dataset is not None and dataset.image_count == 2
            images = list(await session.scalars(sa.select(Image).where(Image.dataset_id == upload_dataset)))
            assert len(images) == 2
            assert any(image.filename == "incoming.jpg" for image in images)
            for image in images:
                assert (app.state.settings.storage_dir / image.file_path).is_file()
        # The batch may delete its source only after the image has committed.
        if response.status_code == 409:
            response = await other_operation()
        assert response.status_code == expected_success, response.text
        if scenario == "rename-sibling-merge":
            for dataset_id in dataset_ids:
                catalog = await client.get(f"/api/datasets/{dataset_id}/classes")
                assert {"class_id": 1, "name": "orklift"} in catalog.json()["classes"]
    finally:
        release.set()
        await _finish_tasks(tasks)
