from __future__ import annotations

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
    Image,
    ImportIssue,
    ProjectClass,
    UploadJob,
)
from app.services.collect import CollectedFile
from app.services.ingest import ingest_collected, run_upload_job
from app.services.storage import contained_storage_path


pytestmark = pytest.mark.asyncio


def make_jpeg(path: Path, color: tuple[int, int, int]) -> None:
    PillowImage.new("RGB", (64, 32), color).save(path, "JPEG")


def item(
    path: Path,
    rel_path: str,
    kind: str,
) -> CollectedFile:
    return CollectedFile(
        rel_path=rel_path,
        abs_path=path,
        kind=kind,  # type: ignore[arg-type]
        split=None,
    )


async def create_dataset_and_job(
    client: httpx.AsyncClient,
    app,
) -> tuple[int, int]:
    created = await client.post(
        "/api/datasets",
        json={"name": f"test-atomic-{uuid4().hex}"},
    )
    dataset_id = created.json()["id"]
    async with app.state.session_factory() as session:
        job = UploadJob(
            dataset_id=dataset_id,
            kind="file",
            state="queued",
            phase="uploading",
            total=0,
            processed=0,
            failed=0,
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        return dataset_id, job.id


async def add_job(app, dataset_id: int) -> int:
    async with app.state.session_factory() as session:
        job = UploadJob(
            dataset_id=dataset_id,
            kind="file",
            state="queued",
            phase="uploading",
            total=0,
            processed=0,
            failed=0,
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        return job.id


async def upload_zip(
    client: httpx.AsyncClient,
    dataset_id: int,
    archive: Path,
) -> tuple[int, int]:
    content = archive.read_bytes()
    created = await client.post(
        f"/api/datasets/{dataset_id}/uploads",
        json={
            "filename": archive.name,
            "size": len(content),
            "chunk_size": max(1, len(content)),
            "kind": "zip",
            "expected_extracted_size": len(content),
        },
    )
    assert created.status_code == 201
    upload_id = created.json()["upload_id"]
    if content:
        sent = await client.put(
            f"/api/uploads/{upload_id}/chunks/0",
            content=content,
        )
        assert sent.status_code == 204
    completed = await client.post(f"/api/uploads/{upload_id}/complete")
    assert completed.status_code == 202
    return upload_id, completed.json()["job_id"]


async def test_failure_before_commit_rolls_back_database_and_files(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    dataset_id, job_id = await create_dataset_and_job(client, app)
    image = tmp_path / "rollback.jpg"
    label = tmp_path / "rollback.txt"
    make_jpeg(image, (200, 100, 20))
    label.write_text("0 0.5 0.5 0.2 0.2\n", encoding="utf-8")

    def fail() -> None:
        raise RuntimeError("forced pre-commit failure")

    with pytest.raises(RuntimeError, match="forced"):
        await ingest_collected(
            app.state.settings,
            app.state.session_factory,
            job_id,
            [
                item(image, "images/rollback.jpg", "image"),
                item(label, "labels/rollback.txt", "label"),
            ],
            before_commit=fail,
        )

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        job = await session.get(UploadJob, job_id)
        assert dataset is not None
        assert job is not None
        assert (dataset.image_count, dataset.annotation_count) == (0, 0)
        assert dataset.status == "failed"
        assert job.state == "failed"
        assert (
            await session.scalar(
                select(func.count(Image.id)).where(Image.dataset_id == dataset_id)
            )
        ) == 0
        assert (
            await session.scalar(select(func.count(Annotation.id)))
        ) == 0
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ProjectClass)
                .where(ProjectClass.project_id == dataset.project_id)
            )
        ) == 0
        assert (
            await session.scalar(
                select(func.count(ImportIssue.id)).where(
                    ImportIssue.job_id == job_id
                )
            )
        ) == 1
        dataset_path = contained_storage_path(
            app.state.settings.storage_dir,
            dataset.storage_path,
        )
        assert not any(
            path.is_file() for path in (dataset_path / "batches").rglob("*")
        )


async def test_merge_suffixes_existing_stem_and_preserves_edits(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    dataset_id, first_job = await create_dataset_and_job(client, app)
    existing_image = tmp_path / "same.jpg"
    existing_label = tmp_path / "same.txt"
    make_jpeg(existing_image, (255, 0, 0))
    existing_label.write_text("0 0.5 0.5 0.2 0.2\n", encoding="utf-8")
    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        first_job,
        [
            item(existing_image, "images/same.jpg", "image"),
            item(existing_label, "labels/same.txt", "label"),
        ],
    )

    async with app.state.session_factory() as session:
        persisted = await session.scalar(
            select(Image).where(
                Image.dataset_id == dataset_id,
                Image.stem == "same",
            )
        )
        assert persisted is not None
        persisted.is_modified = True
        original_image_id = persisted.id
        original_file_path = persisted.file_path
        await session.commit()

    second_job = await add_job(app, dataset_id)
    duplicate_image = tmp_path / "duplicate.jpg"
    duplicate_label = tmp_path / "duplicate.txt"
    new_image = tmp_path / "new.jpg"
    make_jpeg(duplicate_image, (0, 0, 0))
    make_jpeg(new_image, (0, 255, 0))
    duplicate_label.write_text("0 0.1 0.1 0.1 0.1\n", encoding="utf-8")
    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        second_job,
        [
            item(duplicate_image, "replacement/same.jpg", "image"),
            item(duplicate_label, "replacement/same.txt", "label"),
            item(new_image, "images/new.jpg", "image"),
        ],
    )

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        persisted = await session.get(Image, original_image_id)
        appended = await session.scalar(
            select(Image).where(
                Image.dataset_id == dataset_id,
                Image.stem == "same (1)",
            )
        )
        issues = (
            await session.scalars(
                select(ImportIssue).where(ImportIssue.job_id == second_job)
            )
        ).all()
        assert dataset is not None
        assert persisted is not None
        assert appended is not None
        assert dataset.image_count == 3
        assert persisted.is_modified is True
        assert persisted.file_path == original_file_path
        assert appended.filename == "same (1).jpg"
        assert appended.is_modified is False
        duplicate_issues = [
            issue for issue in issues if issue.kind == "duplicate_skipped"
        ]
        assert len(duplicate_issues) == 1
        assert duplicate_issues[0].path == "replacement/same.jpg"
        assert "stored as replacement/same (1).jpg" in duplicate_issues[0].detail


async def test_completed_zip_job_removes_temporary_upload_files(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    dataset_id, _unused_job = await create_dataset_and_job(client, app)
    image = tmp_path / "zipped.jpg"
    make_jpeg(image, (30, 60, 90))
    archive = tmp_path / "valid.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.write(image, "images/train/zipped.jpg")
        zipped.writestr("labels/train/zipped.txt", "0 0.5 0.5 0.2 0.2\n")

    upload_id, job_id = await upload_zip(client, dataset_id, archive)
    await run_upload_job(
        app.state.settings,
        app.state.session_factory,
        job_id,
        upload_id,
    )

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        job = await session.get(UploadJob, job_id)
        assert dataset is not None
        assert job is not None
        assert (dataset.image_count, dataset.annotation_count) == (1, 1)
        assert job.state == "done"
    assert not (
        Path(app.state.settings.storage_dir) / "uploads" / str(upload_id)
    ).exists()


async def test_rejected_zip_records_issue_and_removes_temporary_files(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    dataset_id, _unused_job = await create_dataset_and_job(client, app)
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("../escape.jpg", b"not-an-image")

    upload_id, job_id = await upload_zip(client, dataset_id, archive)
    await run_upload_job(
        app.state.settings,
        app.state.session_factory,
        job_id,
        upload_id,
    )

    async with app.state.session_factory() as session:
        job = await session.get(UploadJob, job_id)
        issues = (
            await session.scalars(
                select(ImportIssue).where(ImportIssue.job_id == job_id)
            )
        ).all()
        assert job is not None
        assert (job.state, job.failed) == ("done", 1)
        assert [(issue.kind, issue.path) for issue in issues] == [
            ("rejected_file", "../escape.jpg")
        ]
    assert not (
        Path(app.state.settings.storage_dir) / "uploads" / str(upload_id)
    ).exists()
