from __future__ import annotations

import asyncio
import io
import zipfile
from collections import Counter
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from PIL import Image as PillowImage
from sqlalchemy import func, select, text

from app.models import (
    Annotation,
    Dataset,
    DatasetClass,
    Image,
    ImportIssue,
    MediaObject,
    ProjectClass,
    UploadJob,
    UploadSession,
    UserStorage,
)
from app.services.collect import CollectedFile
from app.services.ingest import ingest_collected, run_upload_batch_job
from app.services.storage import contained_storage_path


pytestmark = pytest.mark.asyncio


def collected(
    path: Path,
    rel_path: str,
    kind: str,
    split: str | None = None,
) -> CollectedFile:
    return CollectedFile(
        rel_path=rel_path,
        abs_path=path,
        kind=kind,  # type: ignore[arg-type]
        split=split,
    )


async def create_dataset_and_job(
    client: httpx.AsyncClient,
    app,
    *,
    status: str = "pending",
) -> tuple[int, int]:
    response = await client.post(
        "/api/datasets",
        json={"name": f"test-ingest-{uuid4().hex}"},
    )
    dataset_id = response.json()["id"]
    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        dataset.status = status
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


def make_jpeg(path: Path, color: tuple[int, int, int]) -> None:
    PillowImage.new("RGB", (80, 40), color).save(path, "JPEG")


async def test_ingest_matches_stems_reports_issues_and_updates_progress(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    dataset_id, job_id = await create_dataset_and_job(client, app)
    classes = tmp_path / "classes.txt"
    classes.write_text("person\n", encoding="utf-8")
    image_a = tmp_path / "a.jpg"
    image_b = tmp_path / "b.jpg"
    image_bad = tmp_path / "bad.jpg"
    make_jpeg(image_a, (255, 0, 0))
    make_jpeg(image_b, (0, 255, 0))
    image_bad.write_bytes(b"not-jpeg")
    label_a = tmp_path / "a.txt"
    label_a_original = (
        "0 0.5 0.5 0.2 0.4\n"
        "0 1.2 0.5 0.2 0.2\n"
        "9 0.5 0.5 0.2 0.2\n"
    )
    label_a.write_text(label_a_original, encoding="utf-8")
    label_c = tmp_path / "c.txt"
    label_c.write_text("0 0.5 0.5 0.1 0.1\n", encoding="utf-8")
    items = [
        collected(classes, "classes.txt", "classfile"),
        collected(image_a, "images/train/a.jpg", "image", "train"),
        collected(label_a, "labels/train/a.txt", "label", "train"),
        collected(image_b, "images/val/b.jpg", "image", "val"),
        collected(label_c, "labels/val/c.txt", "label", "val"),
        collected(image_bad, "images/train/bad.jpg", "image", "train"),
    ]
    phases: list[str] = []

    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        items,
        phase_observer=phases.append,
    )

    assert phases == [
        "uploading",
        "collecting",
        "parsing",
        "storing",
        "deriving",
        "thumbnailing",
        "done",
    ]
    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        job = await session.get(UploadJob, job_id)
        assert dataset is not None
        assert job is not None
        assert dataset.status == "ready"
        assert (
            dataset.image_count,
            dataset.annotation_count,
            dataset.class_count,
        ) == (2, 1, 1)
        assert (job.state, job.phase, job.total, job.processed, job.failed) == (
            "done",
            "done",
            6,
            6,
            1,
        )
        assert (job.image_processed, job.image_total) == (3, 3)
        stored_images = list(
            (
                await session.scalars(
                    select(Image).where(Image.dataset_id == dataset_id)
                )
            ).all()
        )
        assert len(stored_images) == 2
        assert all(image.media_object_id is not None for image in stored_images)
        assert len({image.media_object_id for image in stored_images}) == 2
        media_objects = list(
            (
                await session.scalars(
                    select(MediaObject).where(
                        MediaObject.created_by_dataset_id == dataset_id
                    )
                )
            ).all()
        )
        assert len(media_objects) == 2
        usage = await session.get(UserStorage, dataset.owner_id)
        assert usage is not None
        assert usage.bytes_used == sum(
            item.original_bytes + item.display_bytes + item.thumb_bytes
            for item in media_objects
        )
        assert len((await session.scalars(select(Annotation))).all()) == 1
        assert len((await session.scalars(select(DatasetClass).where(DatasetClass.dataset_id == dataset_id))).all()) == 1
        project_class = await session.get(
            ProjectClass,
            (dataset.project_id, 0),
        )
        assert project_class is not None
        assert (project_class.name, project_class.color) == (
            "person",
            "#EF4444",
        )
        project_id = dataset.project_id
        issues = (
            await session.scalars(
                select(ImportIssue).where(ImportIssue.job_id == job_id)
            )
        ).all()
        assert Counter(issue.kind for issue in issues) == {
            "broken_label": 2,
            "image_without_label": 1,
            "label_without_image": 1,
            "broken_image": 1,
        }
        for image in await session.scalars(
            select(Image).where(Image.dataset_id == dataset_id)
        ):
            assert not Path(image.file_path).is_absolute()
            assert not Path(image.thumb_path).is_absolute()
            assert contained_storage_path(
                app.state.settings.storage_dir,
                image.file_path,
            ).is_file()
            assert contained_storage_path(
                app.state.settings.storage_dir,
                image.thumb_path,
            ).is_file()

    sibling = await client.post(
        "/api/datasets",
        json={
            "name": f"test-ingest-sibling-{uuid4().hex}",
            "project_id": project_id,
        },
    )
    assert sibling.status_code == 201
    sibling_classes = await client.get(
        f"/api/datasets/{sibling.json()['id']}/classes"
    )
    assert sibling_classes.json() == {
        "classes": [{"class_id": 0, "name": "person"}]
    }

    assert label_a.read_text(encoding="utf-8") == label_a_original
    job_response = await client.get(f"/api/jobs/{job_id}")
    assert job_response.status_code == 200
    assert job_response.json()["state"] == "done"
    assert job_response.json()["image_processed"] == 3
    assert job_response.json()["image_total"] == 3
    issue_response = await client.get(
        f"/api/datasets/{dataset_id}/issues?offset=0&limit=100"
    )
    assert issue_response.status_code == 200
    assert issue_response.json()["total"] == 5


async def test_empty_yolo_label_is_separate_from_missing_label(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    dataset_id, job_id = await create_dataset_and_job(client, app)
    classes = tmp_path / "classes.txt"
    classes.write_text("person\n", encoding="utf-8")
    missing_image = tmp_path / "missing.jpg"
    empty_image = tmp_path / "empty.jpg"
    empty_label = tmp_path / "empty.txt"
    make_jpeg(missing_image, (255, 0, 0))
    make_jpeg(empty_image, (0, 255, 0))
    empty_label.write_text("\n  \n", encoding="utf-8")

    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        [
            collected(classes, "classes.txt", "classfile"),
            collected(missing_image, "images/missing.jpg", "image"),
            collected(empty_image, "images/empty.jpg", "image"),
            collected(empty_label, "labels/empty.txt", "label"),
        ],
    )

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        issues = (
            await session.scalars(
                select(ImportIssue)
                .where(ImportIssue.job_id == job_id)
                .order_by(ImportIssue.id)
            )
        ).all()
        images = (
            await session.scalars(
                select(Image)
                .where(Image.dataset_id == dataset_id)
                .order_by(Image.filename)
            )
        ).all()
        assert dataset is not None
        assert (dataset.image_count, dataset.annotation_count) == (2, 0)
        assert {
            image.filename: image.has_label_source for image in images
        } == {"empty.jpg": True, "missing.jpg": False}
        assert Counter(issue.kind for issue in issues) == {
            "image_without_label": 1,
            "empty_label": 1,
        }
        assert {
            (issue.kind, issue.path)
            for issue in issues
        } == {
            ("image_without_label", "images/missing.jpg"),
            ("empty_label", "labels/empty.txt"),
        }

    response = await client.get(
        f"/api/datasets/{dataset_id}/issues?offset=0&limit=100"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert {
        (item["kind"], item["path"], item["detail"])
        for item in payload["items"]
    } == {
        (
            "image_without_label",
            "images/missing.jpg",
            "matching annotation source was not found",
        ),
        (
            "empty_label",
            "labels/empty.txt",
            "matching label file contained no annotations",
        ),
    }


async def test_ready_dataset_stays_ready_during_merge(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    dataset_id, job_id = await create_dataset_and_job(
        client,
        app,
        status="ready",
    )
    image = tmp_path / "merge.jpg"
    make_jpeg(image, (0, 0, 255))

    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        [collected(image, "images/merge.jpg", "image")],
    )

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        assert dataset.status == "ready"


async def test_http_batch_upload_matches_extensionless_file_sessions(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    response = await client.post(
        "/api/datasets",
        json={"name": f"test-ingest-batch-{uuid4().hex}"},
    )
    dataset_id = response.json()["id"]
    files: list[tuple[str, bytes]] = []
    for stem, color in [("a", (40, 80, 120)), ("b", (120, 80, 40))]:
        image_path = tmp_path / f"{stem}.jpg"
        make_jpeg(image_path, color)
        files.extend(
            [
                (f"images/train/{stem}.jpg", image_path.read_bytes()),
                (f"labels/train/{stem}.txt", b"0 0.5 0.5 0.2 0.2\n"),
            ]
        )

    upload_ids: list[int] = []
    for filename, content in files:
        created = await client.post(
            f"/api/datasets/{dataset_id}/uploads",
            json={
                "filename": filename,
                "size": len(content),
                "chunk_size": len(content),
                "kind": "file",
                "file_count": len(files),
                "expected_extracted_size": sum(
                    len(value) for _, value in files
                ),
            },
        )
        assert created.status_code == 201
        upload_id = created.json()["upload_id"]
        upload_ids.append(upload_id)
        sent = await client.put(
            f"/api/uploads/{upload_id}/chunks/0",
            content=content,
        )
        assert sent.status_code == 204

    completed = await client.post(
        f"/api/datasets/{dataset_id}/upload-batches/complete",
        json={"upload_ids": upload_ids},
    )
    assert completed.status_code == 202
    job_id = completed.json()["job_id"]
    await run_upload_batch_job(
        app.state.settings,
        app.state.session_factory,
        job_id,
        upload_ids,
    )

    async with app.state.session_factory() as session:
        sql_counts = (
            await session.execute(
                text(
                    """
                    SELECT
                      (SELECT count(*) FROM images
                       WHERE dataset_id = :dataset_id) AS image_count,
                      (SELECT count(*) FROM annotations AS annotations
                       JOIN images AS images
                         ON images.id = annotations.image_id
                       WHERE images.dataset_id = :dataset_id)
                        AS annotation_count,
                      (SELECT count(*) FROM upload_sessions
                       WHERE dataset_id = :dataset_id) AS session_count,
                      (SELECT count(*) FROM upload_jobs
                       WHERE id = :job_id) AS job_count
                    """
                ),
                {"dataset_id": dataset_id, "job_id": job_id},
            )
        ).mappings().one()
        dataset = await session.get(Dataset, dataset_id)
        job = await session.get(UploadJob, job_id)
        sessions = (
            await session.scalars(
                select(UploadSession).where(UploadSession.id.in_(upload_ids))
            )
        ).all()
        assert dataset is not None
        assert job is not None
        assert (dataset.image_count, dataset.annotation_count) == (2, 2)
        assert dict(sql_counts) == {
            "image_count": 2,
            "annotation_count": 2,
            "session_count": 4,
            "job_count": 1,
        }
        assert (job.state, job.total, job.processed, job.failed) == (
            "done",
            4,
            4,
            0,
        )
        assert len(sessions) == 4
        assert all(upload.state == "complete" for upload in sessions)
        assert (
            await session.scalar(
                select(func.count(ImportIssue.id)).where(
                    ImportIssue.job_id == job_id,
                    ImportIssue.kind == "rejected_file",
                )
            )
        ) == 0
    assert all(
        not (
            Path(app.state.settings.storage_dir) / "uploads" / str(upload_id)
        ).exists()
        for upload_id in upload_ids
    )


async def test_batch_zip_upload_suffixes_duplicate_image_and_label_stems(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    response = await client.post(
        "/api/datasets",
        json={"name": f"test-ingest-zip-collisions-{uuid4().hex}"},
    )
    dataset_id = response.json()["id"]
    upload_ids: list[int] = []

    for index, center_x in enumerate((0.2, 0.4, 0.6), start=1):
        image_path = tmp_path / f"duplicate-{index}.jpg"
        make_jpeg(image_path, (index * 40, index * 30, index * 20))
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, "w") as archive:
            archive.writestr(
                "images/train/repeated.jpg",
                image_path.read_bytes(),
            )
            archive.writestr(
                "labels/train/repeated.txt",
                f"0 {center_x} 0.5 0.2 0.2\n",
            )
        content = archive_buffer.getvalue()
        created = await client.post(
            f"/api/datasets/{dataset_id}/uploads",
            json={
                "filename": f"part-{index}.zip",
                "size": len(content),
                "chunk_size": len(content),
                "kind": "zip",
                "file_count": 2,
                "expected_extracted_size": len(content),
            },
        )
        assert created.status_code == 201
        upload_id = created.json()["upload_id"]
        upload_ids.append(upload_id)
        sent = await client.put(
            f"/api/uploads/{upload_id}/chunks/0",
            content=content,
        )
        assert sent.status_code == 204

    completed = await client.post(
        f"/api/datasets/{dataset_id}/upload-batches/complete",
        json={"upload_ids": upload_ids},
    )
    assert completed.status_code == 202
    job_id = completed.json()["job_id"]
    await run_upload_batch_job(
        app.state.settings,
        app.state.session_factory,
        job_id,
        upload_ids,
    )

    async with app.state.session_factory() as session:
        images = (
            await session.scalars(
                select(Image)
                .where(Image.dataset_id == dataset_id)
                .order_by(Image.id)
            )
        ).all()
        centers = (
            await session.execute(
                select(Image.stem, Annotation.cx)
                .join(Annotation, Annotation.image_id == Image.id)
                .where(Image.dataset_id == dataset_id)
                .order_by(Image.id)
            )
        ).all()
        duplicate_issues = (
            await session.scalars(
                select(ImportIssue).where(
                    ImportIssue.job_id == job_id,
                    ImportIssue.kind == "duplicate_skipped",
                )
            )
        ).all()

    images_by_stem = {image.stem: image for image in images}
    assert set(images_by_stem) == {
        "repeated",
        "repeated (1)",
        "repeated (2)",
    }
    assert {
        stem: image.filename for stem, image in images_by_stem.items()
    } == {
        "repeated": "repeated.jpg",
        "repeated (1)": "repeated (1).jpg",
        "repeated (2)": "repeated (2).jpg",
    }
    assert {
        stem: image.rel_path for stem, image in images_by_stem.items()
    } == {
        "repeated": "images/train/repeated.jpg",
        "repeated (1)": "images/train/repeated (1).jpg",
        "repeated (2)": "images/train/repeated (2).jpg",
    }
    assert dict(centers) == {
        "repeated": 0.2,
        "repeated (1)": 0.4,
        "repeated (2)": 0.6,
    }
    assert len(duplicate_issues) == 2
    assert [issue.path for issue in duplicate_issues] == [
        "images/train/repeated.jpg",
        "images/train/repeated.jpg",
    ]
    assert [issue.detail.rsplit("stored as ", 1)[-1] for issue in duplicate_issues] == [
        "images/train/repeated (1).jpg",
        "images/train/repeated (2).jpg",
    ]


async def test_http_batch_upload_keeps_valid_lines_in_mixed_label(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    response = await client.post(
        "/api/datasets",
        json={"name": f"test-ingest-mixed-label-{uuid4().hex}"},
    )
    dataset_id = response.json()["id"]
    image_path = tmp_path / "mixed.jpg"
    make_jpeg(image_path, (30, 60, 90))
    label_content = (
        "0 0.5 0.5 0.2 0.2\n"
        "0 0.25 0.25 0.1 0.1\n"
        "0 0.75 0.75 0.1 0.1\n"
        "broken\n"
        "0 0.5 0.5 0.2\n"
        "zero 0.5 0.5 0.2 0.2\n"
        "-1 0.5 0.5 0.2 0.2\n"
        "0 nan 0.5 0.2 0.2\n"
        "0 1.1 0.5 0.2 0.2\n"
        "0 0.5 0.5 nope 0.2\n"
    ).encode()
    files = [
        ("images/train/mixed.jpg", image_path.read_bytes()),
        ("labels/train/mixed.txt", label_content),
        ("images/train/rejected.jpg", b"not-a-valid-image"),
        (
            "labels/train/rejected.txt",
            b"broken label\n0 1.1 0.5 0.2 0.2\n",
        ),
    ]

    upload_ids: list[int] = []
    for filename, content in files:
        created = await client.post(
            f"/api/datasets/{dataset_id}/uploads",
            json={
                "filename": filename,
                "size": len(content),
                "chunk_size": len(content),
                "kind": "file",
                "file_count": len(files),
                "expected_extracted_size": sum(
                    len(value) for _, value in files
                ),
            },
        )
        assert created.status_code == 201
        upload_id = created.json()["upload_id"]
        upload_ids.append(upload_id)
        sent = await client.put(
            f"/api/uploads/{upload_id}/chunks/0",
            content=content,
        )
        assert sent.status_code == 204

    completed = await client.post(
        f"/api/datasets/{dataset_id}/upload-batches/complete",
        json={"upload_ids": upload_ids},
    )
    assert completed.status_code == 202
    job_id = completed.json()["job_id"]
    await run_upload_batch_job(
        app.state.settings,
        app.state.session_factory,
        job_id,
        upload_ids,
    )

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        assert (dataset.image_count, dataset.annotation_count) == (1, 3)
        issues = (
            await session.scalars(
                select(ImportIssue).where(ImportIssue.job_id == job_id)
            )
        ).all()
        assert Counter(
            (issue.kind, issue.path) for issue in issues
        ) == {
            ("broken_label", "labels/train/mixed.txt"): 7,
            ("broken_label", "labels/train/rejected.txt"): 2,
            ("broken_image", "images/train/rejected.jpg"): 1,
            ("label_without_image", "labels/train/rejected.txt"): 1,
        }


async def test_decompression_bomb_rejects_one_image_without_rolling_back_batch(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_id, job_id = await create_dataset_and_job(client, app)
    monkeypatch.setattr(PillowImage, "MAX_IMAGE_PIXELS", 100)
    normal_a = tmp_path / "normal-a.jpg"
    normal_b = tmp_path / "normal-b.jpg"
    bomb = tmp_path / "bomb.jpg"
    PillowImage.new("RGB", (8, 8), (255, 0, 0)).save(normal_a, "JPEG")
    PillowImage.new("RGB", (8, 8), (0, 255, 0)).save(normal_b, "JPEG")
    PillowImage.new("RGB", (20, 20), (0, 0, 255)).save(bomb, "JPEG")
    bomb_label = tmp_path / "bomb.txt"
    bomb_label.write_text("0 0.5 0.5 0.2 0.2\n", encoding="utf-8")

    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        [
            collected(normal_a, "images/normal-a.jpg", "image"),
            collected(normal_b, "images/normal-b.jpg", "image"),
            collected(bomb, "images/bomb.jpg", "image"),
            collected(bomb_label, "labels/bomb.txt", "label"),
        ],
    )

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        job = await session.get(UploadJob, job_id)
        issues = (
            await session.scalars(
                select(ImportIssue).where(ImportIssue.job_id == job_id)
            )
        ).all()
        assert dataset is not None
        assert job is not None
        assert dataset.image_count == 2
        assert (job.state, job.failed) == ("done", 1)
        assert Counter(issue.kind for issue in issues) == {
            "image_without_label": 2,
            "broken_image": 1,
            "label_without_image": 1,
        }


async def test_zip_rejects_unsafe_member_but_keeps_valid_file_and_issue_path(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    response = await client.post(
        "/api/datasets",
        json={"name": f"test-ingest-zip-{uuid4().hex}"},
    )
    dataset_id = response.json()["id"]
    image_path = tmp_path / "valid.jpg"
    make_jpeg(image_path, (20, 40, 60))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("images/valid.jpg", image_path.read_bytes())
        zipped.writestr("../escape.jpg", b"not-an-image")
    content = archive.getvalue()
    created = await client.post(
        f"/api/datasets/{dataset_id}/uploads",
        json={
            "filename": "dataset.zip",
            "size": len(content),
            "chunk_size": len(content),
            "kind": "zip",
            "expected_extracted_size": len(content) * 4,
        },
    )
    upload_id = created.json()["upload_id"]
    assert (
        await client.put(
            f"/api/uploads/{upload_id}/chunks/0",
            content=content,
        )
    ).status_code == 204
    completed = await client.post(f"/api/uploads/{upload_id}/complete")
    job_id = completed.json()["job_id"]

    await run_upload_batch_job(
        app.state.settings,
        app.state.session_factory,
        job_id,
        [upload_id],
    )

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        job = await session.get(UploadJob, job_id)
        upload = await session.get(UploadSession, upload_id)
        issues = (
            await session.scalars(
                select(ImportIssue).where(ImportIssue.job_id == job_id)
            )
        ).all()
        assert dataset is not None
        assert job is not None
        assert upload is not None
        assert dataset.image_count == 1
        assert (job.state, job.failed) == ("done", 1)
        assert upload.state == "complete"
        assert any(
            issue.kind == "rejected_file"
            and issue.path == "../escape.jpg"
            for issue in issues
        )
    assert not (
        Path(app.state.settings.storage_dir) / "uploads" / str(upload_id)
    ).exists()


async def test_zip_limit_failure_persists_actual_member_path(
    client: httpx.AsyncClient,
    app,
) -> None:
    response = await client.post(
        "/api/datasets",
        json={"name": f"test-ingest-zip-limit-{uuid4().hex}"},
    )
    dataset_id = response.json()["id"]
    archive = io.BytesIO()
    with zipfile.ZipFile(
        archive,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as zipped:
        zipped.writestr("bomb.txt", b"a" * 100_000)
    content = archive.getvalue()
    created = await client.post(
        f"/api/datasets/{dataset_id}/uploads",
        json={
            "filename": "bomb.zip",
            "size": len(content),
            "chunk_size": len(content),
            "kind": "zip",
            "expected_extracted_size": 100_000,
        },
    )
    upload_id = created.json()["upload_id"]
    assert (
        await client.put(
            f"/api/uploads/{upload_id}/chunks/0",
            content=content,
        )
    ).status_code == 204
    completed = await client.post(f"/api/uploads/{upload_id}/complete")
    job_id = completed.json()["job_id"]

    await run_upload_batch_job(
        app.state.settings,
        app.state.session_factory,
        job_id,
        [upload_id],
    )

    async with app.state.session_factory() as session:
        job = await session.get(UploadJob, job_id)
        issue = await session.scalar(
            select(ImportIssue).where(ImportIssue.job_id == job_id)
        )
        assert job is not None
        assert issue is not None
        assert job.state == "failed"
        assert issue.path == "bomb.txt"
    assert not (
        Path(app.state.settings.storage_dir) / "uploads" / str(upload_id)
    ).exists()


async def test_unconsumed_files_are_reported_with_specific_reasons(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    dataset_id, job_id = await create_dataset_and_job(client, app)
    image = tmp_path / "kept.jpg"
    make_jpeg(image, (40, 60, 80))
    label = tmp_path / "kept.txt"
    label.write_text("0 0.5 0.5 0.2 0.2\n", encoding="utf-8")
    classes = tmp_path / "classes.txt"
    classes.write_text("person\n", encoding="utf-8")
    coco = tmp_path / "instances_default.json"
    coco.write_text('{"annotations": []}', encoding="utf-8")
    voc = tmp_path / "frame.xml"
    voc.write_text("<annotation />", encoding="utf-8")
    nested = tmp_path / "nested.zip"
    nested.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        [
            collected(image, "images/kept.jpg", "image"),
            collected(label, "labels/kept.txt", "label"),
            collected(classes, "classes.txt", "classfile"),
            collected(
                coco,
                "annotations/instances_default.json",
                "other",
            ),
            collected(voc, "Annotations/frame.xml", "other"),
            collected(nested, "archives/nested.zip", "zip"),
        ],
    )

    async with app.state.session_factory() as session:
        issues = (
            await session.scalars(
                select(ImportIssue)
                .where(
                    ImportIssue.job_id == job_id,
                    ImportIssue.kind == "ignored_file",
                )
                .order_by(ImportIssue.path)
            )
        ).all()
        assert [issue.path for issue in issues] == [
            "Annotations/frame.xml",
            "annotations/instances_default.json",
            "archives/nested.zip",
        ]
        details = {issue.path: issue.detail for issue in issues}
        assert "VOC/XML" in details["Annotations/frame.xml"]
        assert "COCO/JSON" in details[
            "annotations/instances_default.json"
        ]
        assert "nested ZIP" in details["archives/nested.zip"]
        assert not any(
            issue.path in {
                "images/kept.jpg",
                "labels/kept.txt",
                "classes.txt",
            }
            for issue in issues
        )
    response = await client.get(
        f"/api/datasets/{dataset_id}/issues?offset=0&limit=100"
    )
    assert response.status_code == 200
    assert sum(
        item["kind"] == "ignored_file"
        for item in response.json()["items"]
    ) == 3


async def test_job_processed_advances_monotonically_before_completion(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_id, job_id = await create_dataset_and_job(client, app)
    images: list[CollectedFile] = []
    for index in range(4):
        path = tmp_path / f"progress-{index}.jpg"
        make_jpeg(path, (index * 20, 40, 80))
        images.append(collected(path, f"images/{path.name}", "image"))

    from app.services import ingest as ingest_service

    original_prepare = ingest_service.prepare_image
    prepare_order = 0

    async def slow_prepare(*args, **kwargs):
        nonlocal prepare_order
        prepare_order += 1
        # Four workers start together now. Stagger their finishes so the poller
        # deterministically observes at least one committed progress update.
        await asyncio.sleep(0.03 * prepare_order)
        return await original_prepare(*args, **kwargs)

    monkeypatch.setattr(ingest_service, "prepare_image", slow_prepare)
    task = asyncio.create_task(
        ingest_collected(
            app.state.settings,
            app.state.session_factory,
            job_id,
            images,
        )
    )
    observed: list[int] = []
    while not task.done():
        async with app.state.session_factory() as session:
            job = await session.get(UploadJob, job_id)
            assert job is not None
            observed.append(job.image_processed)
        await asyncio.sleep(0.01)
    await task
    async with app.state.session_factory() as session:
        job = await session.get(UploadJob, job_id)
        assert job is not None
        observed.append(job.image_processed)

    assert observed == sorted(observed)
    assert any(0 < value < len(images) for value in observed)
    assert observed[-1] == len(images)
    assert job.image_total == len(images)


async def test_image_derivation_runs_four_at_a_time(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dataset_id, job_id = await create_dataset_and_job(client, app)
    images: list[CollectedFile] = []
    for index in range(8):
        path = tmp_path / f"parallel-{index}.jpg"
        make_jpeg(path, (index * 20, 40, 80))
        images.append(collected(path, f"images/{path.name}", "image"))

    from app.services import ingest as ingest_service

    original_prepare = ingest_service.prepare_image
    active = 0
    max_active = 0

    async def observed_prepare(*args, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.02)
            return await original_prepare(*args, **kwargs)
        finally:
            active -= 1

    monkeypatch.setattr(ingest_service, "prepare_image", observed_prepare)
    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        images,
    )

    assert max_active == 4


async def test_ingest_commits_checkpoints_and_resumes_without_duplicates(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    dataset_id, job_id = await create_dataset_and_job(client, app)
    images: list[CollectedFile] = []
    for index in range(5):
        path = tmp_path / f"checkpoint-{index}.jpg"
        make_jpeg(path, (index * 20, 60, 100))
        images.append(collected(path, f"images/{path.name}", "image"))
    settings = app.state.settings.model_copy(
        update={"ingest_batch_size": 2},
    )
    commit_attempt = 0

    def cancel_during_second_checkpoint() -> None:
        nonlocal commit_attempt
        commit_attempt += 1
        if commit_attempt == 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await ingest_collected(
            settings,
            app.state.session_factory,
            job_id,
            images,
            before_commit=cancel_during_second_checkpoint,
        )

    async with app.state.session_factory() as session:
        partial_job = await session.get(UploadJob, job_id)
        partial_images = (
            await session.scalars(
                select(Image)
                .where(Image.dataset_id == dataset_id)
                .order_by(Image.id)
            )
        ).all()
        assert partial_job is not None
        assert partial_job.ingest_cursor == 2
        assert partial_job.image_total == len(images)
        assert partial_job.image_processed >= partial_job.ingest_cursor
        assert len(partial_images) == 2

    await ingest_collected(
        settings,
        app.state.session_factory,
        job_id,
        images,
    )

    async with app.state.session_factory() as session:
        finished_job = await session.get(UploadJob, job_id)
        stored_images = (
            await session.scalars(
                select(Image)
                .where(Image.dataset_id == dataset_id)
                .order_by(Image.id)
            )
        ).all()
        assert finished_job is not None
        assert finished_job.ingest_cursor == len(images)
        assert finished_job.image_processed == len(images)
        assert finished_job.image_total == len(images)
        assert finished_job.state == "done"
        assert len(stored_images) == len(images)
        assert len({image.rel_path for image in stored_images}) == len(images)


async def test_checkpoint_resume_keeps_collision_suffixes_stable(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    dataset_id, first_job_id = await create_dataset_and_job(client, app)
    original = tmp_path / "original-same.jpg"
    make_jpeg(original, (10, 20, 30))
    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        first_job_id,
        [collected(original, "images/same.jpg", "image")],
    )
    async with app.state.session_factory() as session:
        second_job = UploadJob(
            dataset_id=dataset_id,
            kind="folder",
            state="queued",
            phase="uploading",
            total=0,
            processed=0,
            failed=0,
        )
        session.add(second_job)
        await session.commit()
        second_job_id = second_job.id

    replacement = tmp_path / "replacement-same.jpg"
    later = tmp_path / "z-later.jpg"
    make_jpeg(replacement, (40, 50, 60))
    make_jpeg(later, (70, 80, 90))
    incoming = [
        collected(replacement, "images/same.jpg", "image"),
        collected(later, "images/z-later.jpg", "image"),
    ]
    settings = app.state.settings.model_copy(update={"ingest_batch_size": 1})
    commit_attempt = 0

    def cancel_second_checkpoint() -> None:
        nonlocal commit_attempt
        commit_attempt += 1
        if commit_attempt == 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await ingest_collected(
            settings,
            app.state.session_factory,
            second_job_id,
            incoming,
            before_commit=cancel_second_checkpoint,
        )
    await ingest_collected(
        settings,
        app.state.session_factory,
        second_job_id,
        incoming,
    )

    async with app.state.session_factory() as session:
        stems = list(
            (
                await session.scalars(
                    select(Image.stem)
                    .where(Image.dataset_id == dataset_id)
                    .order_by(Image.stem)
                )
            ).all()
        )
    assert stems == ["same", "same (1)", "z-later"]
