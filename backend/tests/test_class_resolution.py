from __future__ import annotations

import io
import zipfile
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from PIL import Image as PillowImage
from sqlalchemy import func, select

from app.models import (
    Annotation,
    Dataset,
    DatasetClass,
    Image,
    ImportIssue,
    ProjectClass,
    UploadJob,
)
from app.services.collect import CollectedFile
from app.services.ingest import (
    ClassResolutionRequired,
    ingest_collected,
    run_upload_batch_job,
)
from app.services.uploads import upload_directory


pytestmark = pytest.mark.asyncio


def _name(label: str) -> str:
    return f"test-class-resolution-{label}-{uuid4().hex}"


def _collected(path: Path, rel_path: str, kind: str) -> CollectedFile:
    return CollectedFile(
        rel_path=rel_path,
        abs_path=path,
        kind=kind,  # type: ignore[arg-type]
        split="train",
    )


async def _project_dataset_job(
    client: httpx.AsyncClient,
    app,
    *,
    sibling: bool = False,
    upload_draft: bool = False,
) -> tuple[int, int, int, int | None]:
    project = await client.post(
        "/api/projects",
        json={
            "name": _name("project"),
            "classes": [
                {"name": "person", "color": "#EF4444"},
                {"name": "forklift", "color": "#F59E0B"},
            ],
        },
    )
    assert project.status_code == 201
    project_id = project.json()["id"]
    dataset = await client.post(
        "/api/datasets",
        json={
            "name": _name("dataset"),
            "project_id": project_id,
            "upload_draft": upload_draft,
        },
    )
    assert dataset.status_code == 201
    sibling_id: int | None = None
    if sibling:
        sibling_response = await client.post(
            "/api/datasets",
            json={"name": _name("sibling"), "project_id": project_id},
        )
        assert sibling_response.status_code == 201
        sibling_id = sibling_response.json()["id"]

    async with app.state.session_factory() as session:
        job = UploadJob(
            dataset_id=dataset.json()["id"],
            kind="folder",
            state="queued",
            phase="uploading",
            total=0,
            processed=0,
            failed=0,
            upload_ids=[],
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        job_id = job.id
    return project_id, dataset.json()["id"], job_id, sibling_id


def _incoming_files(tmp_path: Path) -> list[CollectedFile]:
    names = tmp_path / "obj.names"
    names.write_text("person\norklift\n", encoding="utf-8")
    image = tmp_path / "worker.jpg"
    PillowImage.new("RGB", (32, 24), (40, 80, 120)).save(image, "JPEG")
    label = tmp_path / "worker.txt"
    label.write_text("1 0.5 0.5 0.2 0.2\n", encoding="utf-8")
    return [
        _collected(names, "obj.names", "classfile"),
        _collected(image, "images/train/worker.jpg", "image"),
        _collected(label, "labels/train/worker.txt", "label"),
    ]


async def _pause_for_resolution(app, job_id: int, files: list[CollectedFile]):
    with pytest.raises(ClassResolutionRequired):
        await ingest_collected(
            app.state.settings,
            app.state.session_factory,
            job_id,
            files,
            require_class_resolution=True,
        )
    async with app.state.session_factory() as session:
        job = await session.get(UploadJob, job_id)
        assert job is not None
        assert job.state == "awaiting_class_resolution"
        assert job.phase == "awaiting_class_resolution"
        assert job.class_resolution_plan is not None
        return job.class_resolution_plan


async def test_project_name_choice_pauses_before_writes_and_resumes(
    client,
    app,
    tmp_path: Path,
) -> None:
    project_id, dataset_id, job_id, _ = await _project_dataset_job(client, app)
    files = _incoming_files(tmp_path)

    plan = await _pause_for_resolution(app, job_id, files)

    assert plan["conflicts"] == [
        {
            "key": "class:1",
            "class_id": 1,
            "source_path": "obj.names",
            "project_name": "forklift",
            "uploaded_name": "orklift",
        }
    ]
    async with app.state.session_factory() as session:
        assert await session.scalar(
            select(func.count(Image.id)).where(Image.dataset_id == dataset_id)
        ) == 0
        assert await session.scalar(
            select(func.count(ImportIssue.id)).where(ImportIssue.job_id == job_id)
        ) == 0

    accepted = await client.post(
        f"/api/jobs/{job_id}/class-resolution",
        json={
            "revision": plan["revision"],
            "resolutions": [
                {"key": "class:1", "action": "use_project"},
            ],
        },
    )
    assert accepted.status_code == 202
    assert accepted.json() == {"job_id": job_id}

    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        files,
        require_class_resolution=True,
    )

    async with app.state.session_factory() as session:
        project_class = await session.get(ProjectClass, (project_id, 1))
        dataset_class = await session.get(DatasetClass, (dataset_id, 1))
        annotation = await session.scalar(
            select(Annotation)
            .join(Image, Image.id == Annotation.image_id)
            .where(Image.dataset_id == dataset_id)
        )
        job = await session.get(UploadJob, job_id)
        assert project_class is not None and project_class.name == "forklift"
        assert dataset_class is not None and dataset_class.name == "forklift"
        assert annotation is not None and annotation.class_id == 1
        assert job is not None and job.state == "done"
        assert job.class_resolution_plan is None
        assert job.class_resolutions is None
        assert await session.scalar(
            select(func.count(ImportIssue.id)).where(
                ImportIssue.job_id == job_id,
                ImportIssue.kind == "class_conflict",
            )
        ) == 0


async def test_uploaded_name_choice_renames_every_project_dataset(
    client,
    app,
    tmp_path: Path,
) -> None:
    project_id, dataset_id, job_id, sibling_id = await _project_dataset_job(
        client,
        app,
        sibling=True,
    )
    assert sibling_id is not None
    files = _incoming_files(tmp_path)
    plan = await _pause_for_resolution(app, job_id, files)

    accepted = await client.post(
        f"/api/jobs/{job_id}/class-resolution",
        json={
            "revision": plan["revision"],
            "resolutions": [
                {"key": "class:1", "action": "use_upload"},
            ],
        },
    )
    assert accepted.status_code == 202

    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        files,
        require_class_resolution=True,
    )

    async with app.state.session_factory() as session:
        project_class = await session.get(ProjectClass, (project_id, 1))
        target_class = await session.get(DatasetClass, (dataset_id, 1))
        sibling_class = await session.get(DatasetClass, (sibling_id, 1))
        annotation = await session.scalar(
            select(Annotation)
            .join(Image, Image.id == Annotation.image_id)
            .where(Image.dataset_id == dataset_id)
        )
        assert project_class is not None and project_class.name == "orklift"
        assert target_class is not None and target_class.name == "orklift"
        assert sibling_class is not None and sibling_class.name == "orklift"
        assert annotation is not None and annotation.class_id == 1


async def test_uploaded_name_choice_rolls_back_with_the_ingest(
    client,
    app,
    tmp_path: Path,
) -> None:
    project_id, dataset_id, job_id, sibling_id = await _project_dataset_job(
        client,
        app,
        sibling=True,
    )
    assert sibling_id is not None
    files = _incoming_files(tmp_path)
    plan = await _pause_for_resolution(app, job_id, files)
    accepted = await client.post(
        f"/api/jobs/{job_id}/class-resolution",
        json={
            "revision": plan["revision"],
            "resolutions": [
                {"key": "class:1", "action": "use_upload"},
            ],
        },
    )
    assert accepted.status_code == 202

    def fail_before_commit() -> None:
        raise RuntimeError("rollback class resolution")

    with pytest.raises(RuntimeError, match="rollback class resolution"):
        await ingest_collected(
            app.state.settings,
            app.state.session_factory,
            job_id,
            files,
            require_class_resolution=True,
            before_commit=fail_before_commit,
        )

    async with app.state.session_factory() as session:
        project_class = await session.get(ProjectClass, (project_id, 1))
        target_class = await session.get(DatasetClass, (dataset_id, 1))
        sibling_class = await session.get(DatasetClass, (sibling_id, 1))
        assert project_class is not None and project_class.name == "forklift"
        assert target_class is not None and target_class.name == "forklift"
        assert sibling_class is not None and sibling_class.name == "forklift"
        assert await session.scalar(
            select(func.count(Image.id)).where(Image.dataset_id == dataset_id)
        ) == 0


async def test_resolution_rejects_missing_choices_and_stale_revision(
    client,
    app,
    tmp_path: Path,
) -> None:
    _, _, job_id, _ = await _project_dataset_job(client, app)
    plan = await _pause_for_resolution(app, job_id, _incoming_files(tmp_path))

    missing = await client.post(
        f"/api/jobs/{job_id}/class-resolution",
        json={"revision": plan["revision"], "resolutions": []},
    )
    stale = await client.post(
        f"/api/jobs/{job_id}/class-resolution",
        json={
            "revision": "0" * 64,
            "resolutions": [
                {"key": "class:1", "action": "use_project"},
            ],
        },
    )

    assert missing.status_code == 422
    assert stale.status_code == 409
    async with app.state.session_factory() as session:
        job = await session.get(UploadJob, job_id)
        assert job is not None
        assert job.state == "awaiting_class_resolution"
        assert job.class_resolutions is None


async def test_uploaded_name_choice_rejects_another_project_class_name(
    client,
    app,
    tmp_path: Path,
) -> None:
    project_id, _, job_id, _ = await _project_dataset_job(client, app)
    plan = await _pause_for_resolution(app, job_id, _incoming_files(tmp_path))
    async with app.state.session_factory() as session:
        session.add(
            ProjectClass(
                project_id=project_id,
                class_id=2,
                name="orklift",
                color="#22C55E",
            )
        )
        await session.commit()

    response = await client.post(
        f"/api/jobs/{job_id}/class-resolution",
        json={
            "revision": plan["revision"],
            "resolutions": [
                {"key": "class:1", "action": "use_upload"},
            ],
        },
    )

    assert response.status_code == 409
    async with app.state.session_factory() as session:
        job = await session.get(UploadJob, job_id)
        assert job is not None
        assert job.state == "awaiting_class_resolution"
        assert job.class_resolutions is None


async def test_same_name_at_another_id_remaps_without_a_prompt(
    client,
    app,
    tmp_path: Path,
) -> None:
    _, dataset_id, job_id, _ = await _project_dataset_job(client, app)
    names = tmp_path / "classes.txt"
    names.write_text("forklift\n", encoding="utf-8")
    image = tmp_path / "forklift.jpg"
    PillowImage.new("RGB", (32, 24), (40, 80, 120)).save(image, "JPEG")
    label = tmp_path / "forklift.txt"
    label.write_text("0 0.5 0.5 0.2 0.2\n", encoding="utf-8")

    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        [
            _collected(names, "classes.txt", "classfile"),
            _collected(image, "images/train/forklift.jpg", "image"),
            _collected(label, "labels/train/forklift.txt", "label"),
        ],
        require_class_resolution=True,
    )

    async with app.state.session_factory() as session:
        annotation = await session.scalar(
            select(Annotation)
            .join(Image, Image.id == Annotation.image_id)
            .where(Image.dataset_id == dataset_id)
        )
        job = await session.get(UploadJob, job_id)
        assert annotation is not None and annotation.class_id == 1
        assert job is not None and job.state == "done"
        assert job.class_resolution_plan is None


async def test_zip_pause_preserves_input_and_resume_reextracts(
    client,
    app,
) -> None:
    project = await client.post(
        "/api/projects",
        json={
            "name": _name("zip-project"),
            "classes": [
                {"name": "person", "color": "#EF4444"},
                {"name": "forklift", "color": "#F59E0B"},
            ],
        },
    )
    dataset = await client.post(
        "/api/datasets",
        json={
            "name": _name("zip-dataset"),
            "project_id": project.json()["id"],
        },
    )
    image_bytes = io.BytesIO()
    PillowImage.new("RGB", (32, 24), (40, 80, 120)).save(
        image_bytes,
        "JPEG",
    )
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("obj.names", "person\norklift\n")
        archive.writestr("images/train/worker.jpg", image_bytes.getvalue())
        archive.writestr(
            "labels/train/worker.txt",
            "1 0.5 0.5 0.2 0.2\n",
        )
    payload = archive_bytes.getvalue()
    created = await client.post(
        f"/api/datasets/{dataset.json()['id']}/uploads",
        json={
            "filename": "incoming.zip",
            "size": len(payload),
            "chunk_size": len(payload),
            "kind": "zip",
            "expected_extracted_size": len(payload),
        },
    )
    upload_id = created.json()["upload_id"]
    assert (
        await client.put(
            f"/api/uploads/{upload_id}/chunks/0",
            content=payload,
        )
    ).status_code == 204
    completed = await client.post(
        f"/api/datasets/{dataset.json()['id']}/upload-batches/complete",
        json={"upload_ids": [upload_id]},
    )
    job_id = completed.json()["job_id"]

    await run_upload_batch_job(
        app.state.settings,
        app.state.session_factory,
        job_id,
        [upload_id],
    )

    waiting = await client.get(f"/api/jobs/{job_id}")
    assert waiting.status_code == 200
    assert waiting.json()["state"] == "awaiting_class_resolution"
    plan = waiting.json()["class_resolution"]
    upload_root = upload_directory(app.state.settings, upload_id)
    assert (upload_root / "source").is_file()
    assert (upload_root / "extracted").is_dir()
    async with app.state.session_factory() as session:
        job = await session.get(UploadJob, job_id)
        assert job is not None and job.upload_ids == [upload_id]

    accepted = await client.post(
        f"/api/jobs/{job_id}/class-resolution",
        json={
            "revision": plan["revision"],
            "resolutions": [
                {"key": "class:1", "action": "use_project"},
            ],
        },
    )
    assert accepted.status_code == 202
    await run_upload_batch_job(
        app.state.settings,
        app.state.session_factory,
        job_id,
        [upload_id],
    )

    finished = await client.get(f"/api/jobs/{job_id}")
    assert finished.json()["state"] == "done"
    assert "class_resolution" not in finished.json()
    assert not upload_root.exists()


async def test_upload_draft_stays_hidden_until_class_resolution_finishes(
    client,
    app,
    tmp_path: Path,
) -> None:
    project_id, dataset_id, job_id, _ = await _project_dataset_job(
        client,
        app,
        upload_draft=True,
    )
    files = _incoming_files(tmp_path)

    plan = await _pause_for_resolution(app, job_id, files)

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        assert dataset.is_placeholder is True
        assert dataset.image_count == 0
    project_before = await client.get(f"/api/projects/{project_id}")
    assert project_before.status_code == 200
    assert all(
        row["id"] != dataset_id
        for row in project_before.json()["datasets"]
    )

    accepted = await client.post(
        f"/api/jobs/{job_id}/class-resolution",
        json={
            "revision": plan["revision"],
            "resolutions": [
                {"key": "class:1", "action": "use_upload"},
            ],
        },
    )
    assert accepted.status_code == 202
    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        files,
        require_class_resolution=True,
    )

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        assert dataset.is_placeholder is False
        assert dataset.status == "ready"
        assert dataset.image_count == 1
        dataset_class = await session.get(DatasetClass, (dataset_id, 1))
        assert dataset_class is not None
        assert dataset_class.name == "orklift"
    project_after = await client.get(f"/api/projects/{project_id}")
    assert project_after.status_code == 200
    assert any(
        row["id"] == dataset_id
        for row in project_after.json()["datasets"]
    )
