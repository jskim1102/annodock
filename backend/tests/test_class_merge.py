from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from PIL import Image as PillowImage
from sqlalchemy import select

from app.models import (
    Annotation,
    Dataset,
    DatasetClass,
    ImportIssue,
    UploadJob,
)
from app.services.collect import CollectedFile
from app.services.ingest import ingest_collected


pytestmark = pytest.mark.asyncio


def collected(path: Path, rel_path: str, kind: str) -> CollectedFile:
    return CollectedFile(
        rel_path=rel_path,
        abs_path=path,
        kind=kind,  # type: ignore[arg-type]
        split=None,
    )


async def dataset_with_classes_and_job(
    client: httpx.AsyncClient,
    app,
    classes: dict[int, str],
) -> tuple[int, int]:
    response = await client.post(
        "/api/datasets",
        json={"name": f"test-class-merge-{uuid4().hex}"},
    )
    dataset_id = response.json()["id"]
    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        dataset.status = "ready"
        dataset.class_count = len(classes)
        session.add_all(
            [
                DatasetClass(
                    dataset_id=dataset_id,
                    class_id=class_id,
                    name=name,
                )
                for class_id, name in classes.items()
            ]
        )
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


def make_image_and_label(
    tmp_path: Path,
    stem: str,
    class_id: int,
) -> tuple[Path, Path]:
    image = tmp_path / f"{stem}.jpg"
    PillowImage.new("RGB", (32, 24), (40, 80, 120)).save(image, "JPEG")
    label = tmp_path / f"{stem}.txt"
    label.write_text(
        f"{class_id} 0.5 0.5 0.2 0.2\n",
        encoding="utf-8",
    )
    return image, label


async def test_merge_keeps_conflicting_names_and_adds_new_ids(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    dataset_id, job_id = await dataset_with_classes_and_job(
        client,
        app,
        {0: "cat", 1: "dog"},
    )
    names = tmp_path / "obj.names"
    names.write_text("person\nforklift\nhelmet\n", encoding="utf-8")
    image, label = make_image_and_label(tmp_path, "worker", 2)

    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        [
            collected(names, "obj.names", "classfile"),
            collected(image, "worker.jpg", "image"),
            collected(label, "worker.txt", "label"),
        ],
    )

    async with app.state.session_factory() as session:
        classes = (
            await session.scalars(
                select(DatasetClass)
                .where(DatasetClass.dataset_id == dataset_id)
                .order_by(DatasetClass.class_id)
            )
        ).all()
        issues = (
            await session.scalars(
                select(ImportIssue)
                .where(
                    ImportIssue.job_id == job_id,
                    ImportIssue.kind == "class_conflict",
                )
                .order_by(ImportIssue.id)
            )
        ).all()
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        assert [(row.class_id, row.name) for row in classes] == [
            (0, "cat"),
            (1, "dog"),
            (2, "helmet"),
        ]
        assert dataset.class_count == 3
        assert [issue.path for issue in issues] == [
            "obj.names",
            "obj.names",
            "obj.names",
        ]
        assert "id 0" in issues[0].detail
        assert "cat" in issues[0].detail
        assert "person" in issues[0].detail
        assert "id 1" in issues[1].detail
        assert "dog" in issues[1].detail
        assert "forklift" in issues[1].detail
        assert "id 2" in issues[2].detail
        assert "helmet" in issues[2].detail
        assert "uploaded class count 3 exceeds existing 2" in issues[2].detail
        annotation = await session.scalar(select(Annotation))
        assert annotation is not None
        assert annotation.class_id == 2


async def test_upload_reuses_project_class_id_when_name_matches(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    project = await client.post(
        "/api/projects",
        json={
            "name": f"test-class-remap-{uuid4().hex}",
            "classes": [
                {"name": "cat", "color": "#EF4444"},
                {"name": "person", "color": "#F59E0B"},
            ],
        },
    )
    dataset = await client.post(
        "/api/datasets",
        json={
            "name": f"test-class-remap-dataset-{uuid4().hex}",
            "project_id": project.json()["id"],
        },
    )
    dataset_id = dataset.json()["id"]
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
        job_id = job.id

    names = tmp_path / "classes.txt"
    names.write_text("person\n", encoding="utf-8")
    image, label = make_image_and_label(tmp_path, "worker", 0)

    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        [
            collected(names, "classes.txt", "classfile"),
            collected(image, "worker.jpg", "image"),
            collected(label, "worker.txt", "label"),
        ],
    )

    async with app.state.session_factory() as session:
        classes = (
            await session.scalars(
                select(DatasetClass)
                .where(DatasetClass.dataset_id == dataset_id)
                .order_by(DatasetClass.class_id)
            )
        ).all()
        annotation = await session.scalar(select(Annotation))
        assert [(row.class_id, row.name) for row in classes] == [
            (0, "cat"),
            (1, "person"),
        ]
        assert annotation is not None
        assert annotation.class_id == 1

    detail = await client.get(f"/api/projects/{project.json()['id']}")
    assert detail.json()["classes"] == [
        {"class_id": 0, "name": "cat", "color": "#EF4444"},
        {"class_id": 1, "name": "person", "color": "#F59E0B"},
    ]

    async with app.state.session_factory() as session:
        conflicts = await session.scalars(
            select(ImportIssue).where(
                ImportIssue.job_id == job_id,
                ImportIssue.kind == "class_conflict",
            )
        )
        assert conflicts.all() == []


async def test_upload_reports_unknown_name_when_class_count_matches(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    dataset_id, job_id = await dataset_with_classes_and_job(
        client,
        app,
        {0: "cat", 1: "dog"},
    )
    names = tmp_path / "classes.txt"
    names.write_text("cat\nperson\n", encoding="utf-8")
    image, label = make_image_and_label(tmp_path, "worker", 1)

    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        [
            collected(names, "classes.txt", "classfile"),
            collected(image, "worker.jpg", "image"),
            collected(label, "worker.txt", "label"),
        ],
    )

    async with app.state.session_factory() as session:
        issues = (
            await session.scalars(
                select(ImportIssue).where(
                    ImportIssue.job_id == job_id,
                    ImportIssue.kind == "class_conflict",
                )
            )
        ).all()
        assert len(issues) == 1
        assert issues[0].path == "classes.txt"
        assert "id 1" in issues[0].detail
        assert "person" in issues[0].detail
        assert "exceeds existing" not in issues[0].detail


async def test_upload_reports_extra_class_beyond_project_catalog(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    project = await client.post(
        "/api/projects",
        json={
            "name": f"test-extra-class-{uuid4().hex}",
            "classes": [
                {"name": "cat", "color": "#EF4444"},
                {"name": "dog", "color": "#F59E0B"},
            ],
        },
    )
    dataset = await client.post(
        "/api/datasets",
        json={
            "name": f"test-extra-class-dataset-{uuid4().hex}",
            "project_id": project.json()["id"],
        },
    )
    async with app.state.session_factory() as session:
        job = UploadJob(
            dataset_id=dataset.json()["id"],
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
        job_id = job.id

    names = tmp_path / "classes.txt"
    names.write_text("cat\ndog\nhelmet\n", encoding="utf-8")
    image, label = make_image_and_label(tmp_path, "worker", 2)

    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        [
            collected(names, "classes.txt", "classfile"),
            collected(image, "worker.jpg", "image"),
            collected(label, "worker.txt", "label"),
        ],
    )

    async with app.state.session_factory() as session:
        issues = (
            await session.scalars(
                select(ImportIssue).where(
                    ImportIssue.job_id == job_id,
                    ImportIssue.kind == "class_conflict",
                )
            )
        ).all()
        assert len(issues) == 1
        assert issues[0].path == "classes.txt"
        assert "id 2" in issues[0].detail
        assert "helmet" in issues[0].detail
        assert "uploaded class count 3 exceeds existing 2" in issues[0].detail


async def test_merge_without_class_file_adds_observed_numeric_ids(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    dataset_id, job_id = await dataset_with_classes_and_job(
        client,
        app,
        {0: "person"},
    )
    image, label = make_image_and_label(tmp_path, "unknown", 5)

    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        [
            collected(image, "unknown.jpg", "image"),
            collected(label, "unknown.txt", "label"),
        ],
    )

    async with app.state.session_factory() as session:
        classes = (
            await session.scalars(
                select(DatasetClass)
                .where(DatasetClass.dataset_id == dataset_id)
                .order_by(DatasetClass.class_id)
            )
        ).all()
        assert [(row.class_id, row.name) for row in classes] == [
            (0, "person"),
            (5, "5"),
        ]
        issues = (
            await session.scalars(
                select(ImportIssue).where(
                    ImportIssue.job_id == job_id,
                    ImportIssue.kind == "class_conflict",
                )
            )
        ).all()
        assert len(issues) == 1
        assert "id 5" in issues[0].detail
        assert "uploaded class '5' registered" in issues[0].detail
