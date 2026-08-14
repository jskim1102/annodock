from __future__ import annotations

import json
import zipfile
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
    Image,
    ImportIssue,
    UploadJob,
)
from app.services.collect import collect_directory
from app.services.ingest import ingest_collected, run_upload_batch_job


pytestmark = pytest.mark.asyncio
SOURCE_ROOT = Path(__file__).resolve().parents[2] / ".source"
COCO_ARCHIVE = (
    SOURCE_ROOT / "job_775-2026_07_06_03_06_54-coco 1.0.zip"
)
VOC_ARCHIVE = (
    SOURCE_ROOT / "job_775-2026_07_06_03_06_54-pascal voc 1.1.zip"
)
YOLO_ARCHIVE = (
    SOURCE_ROOT / "job_766-2026_07_10_07_49_09-yolo 1.1.zip"
)


async def create_dataset_and_job(
    client: httpx.AsyncClient,
    app,
) -> tuple[int, int]:
    response = await client.post(
        "/api/datasets",
        json={"name": f"test-format-mixed-{uuid4().hex}"},
    )
    dataset_id = response.json()["id"]
    async with app.state.session_factory() as session:
        job = UploadJob(
            dataset_id=dataset_id,
            kind="folder",
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


def make_image(root: Path) -> Path:
    image = root / "images" / "frame.jpg"
    image.parent.mkdir(parents=True)
    PillowImage.new("RGB", (100, 50), (20, 40, 60)).save(image, "JPEG")
    return image


def write_coco(root: Path, bbox: list[float]) -> Path:
    path = root / "annotations" / "instances.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "images": [
                    {
                        "id": 1,
                        "file_name": "frame.jpg",
                        "width": 100,
                        "height": 50,
                    }
                ],
                "categories": [{"id": 1, "name": "person"}],
                "annotations": [
                    {
                        "id": 1,
                        "image_id": 1,
                        "category_id": 1,
                        "bbox": bbox,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def write_voc(root: Path, xmax: float) -> Path:
    path = root / "Annotations" / "frame.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""
        <annotation>
          <filename>frame.jpg</filename>
          <size><width>100</width><height>50</height></size>
          <object><name>person</name><bndbox>
            <xmin>10</xmin><ymin>5</ymin>
            <xmax>{xmax}</xmax><ymax>15</ymax>
          </bndbox></object>
        </annotation>
        """,
        encoding="utf-8",
    )
    return path


async def test_mixed_sources_choose_yolo_then_report_displaced_sources(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    dataset_id, job_id = await create_dataset_and_job(client, app)
    make_image(tmp_path)
    label = tmp_path / "labels" / "frame.txt"
    label.parent.mkdir()
    label.write_text("0 0.5 0.5 0.1 0.1\n", encoding="utf-8")
    (tmp_path / "classes.txt").write_text("person\n", encoding="utf-8")
    write_coco(tmp_path, [10, 5, 20, 10])
    write_voc(tmp_path, 40)

    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        collect_directory(tmp_path, app.state.settings.allowed_image_exts),
    )

    async with app.state.session_factory() as session:
        annotation = await session.scalar(
            select(Annotation)
            .join(Image)
            .where(Image.dataset_id == dataset_id)
        )
        assert annotation is not None
        assert annotation.w == pytest.approx(0.1)
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
        displaced = {
            issue.path: issue.detail
            for issue in issues
            if issue.path.endswith((".json", ".xml"))
        }
        assert set(displaced) == {
            "Annotations/frame.xml",
            "annotations/instances.json",
        }
        assert all("YOLO won" in detail for detail in displaced.values())
        assert all("1 annotation(s)" in detail for detail in displaced.values())


async def test_pure_coco_flows_through_existing_storage_pipeline(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    dataset_id, job_id = await create_dataset_and_job(client, app)
    make_image(tmp_path)
    write_coco(tmp_path, [10, 5, 20, 10])

    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        collect_directory(tmp_path, app.state.settings.allowed_image_exts),
    )

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        annotation = await session.scalar(
            select(Annotation)
            .join(Image)
            .where(Image.dataset_id == dataset_id)
        )
        classes = (
            await session.scalars(
                select(DatasetClass).where(
                    DatasetClass.dataset_id == dataset_id
                )
            )
        ).all()
        assert dataset is not None
        assert (dataset.image_count, dataset.annotation_count) == (1, 1)
        assert annotation is not None
        assert (annotation.cx, annotation.cy, annotation.w, annotation.h) == (
            pytest.approx(0.2),
            pytest.approx(0.2),
            pytest.approx(0.2),
            pytest.approx(0.2),
        )
        assert [(item.class_id, item.name) for item in classes] == [
            (0, "person")
        ]
        assert not await session.scalar(
            select(ImportIssue).where(
                ImportIssue.job_id == job_id,
                ImportIssue.path == "annotations/instances.json",
                ImportIssue.kind == "ignored_file",
            )
        )


async def test_pure_voc_ignores_labelmap_as_metadata_not_classes(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    dataset_id, job_id = await create_dataset_and_job(client, app)
    make_image(tmp_path)
    write_voc(tmp_path, 30)
    (tmp_path / "labelmap.txt").write_text(
        "background:0,0,0::\nperson:255,0,0::\n",
        encoding="utf-8",
    )
    image_set = tmp_path / "ImageSets" / "Main" / "default.txt"
    image_set.parent.mkdir(parents=True)
    image_set.write_text("frame\n", encoding="utf-8")

    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        collect_directory(tmp_path, app.state.settings.allowed_image_exts),
    )

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        classes = (
            await session.scalars(
                select(DatasetClass).where(
                    DatasetClass.dataset_id == dataset_id
                )
            )
        ).all()
        broken_paths = (
            await session.scalars(
                select(ImportIssue.path).where(
                    ImportIssue.job_id == job_id,
                    ImportIssue.kind == "broken_label",
                )
            )
        ).all()
        assert dataset is not None
        assert (dataset.image_count, dataset.annotation_count) == (1, 1)
        assert [(item.class_id, item.name) for item in classes] == [
            (0, "person")
        ]
        assert "labelmap.txt" not in broken_paths
        assert "ImageSets/Main/default.txt" not in broken_paths
        assert not await session.scalar(
            select(ImportIssue).where(
                ImportIssue.job_id == job_id,
                ImportIssue.path == "Annotations/frame.xml",
                ImportIssue.kind == "ignored_file",
            )
        )


async def upload_real_archive(
    client: httpx.AsyncClient,
    app,
    archive: Path,
) -> tuple[int, int]:
    created_dataset = await client.post(
        "/api/datasets",
        json={"name": f"test-format-real-{uuid4().hex}"},
    )
    assert created_dataset.status_code == 201
    dataset_id = created_dataset.json()["id"]
    with zipfile.ZipFile(archive) as zipped:
        expected_extracted_size = sum(
            member.file_size
            for member in zipped.infolist()
            if not member.is_dir()
        )
        file_count = sum(
            not member.is_dir() for member in zipped.infolist()
        )
    chunk_size = 8 * 1024 * 1024
    created_upload = await client.post(
        f"/api/datasets/{dataset_id}/uploads",
        json={
            "filename": archive.name,
            "size": archive.stat().st_size,
            "chunk_size": chunk_size,
            "kind": "zip",
            "file_count": file_count,
            "expected_extracted_size": expected_extracted_size,
        },
    )
    assert created_upload.status_code == 201
    upload_id = created_upload.json()["upload_id"]
    with archive.open("rb") as source:
        chunk_number = 0
        while content := source.read(chunk_size):
            response = await client.put(
                f"/api/uploads/{upload_id}/chunks/{chunk_number}",
                content=content,
            )
            assert response.status_code == 204
            chunk_number += 1
    completed = await client.post(f"/api/uploads/{upload_id}/complete")
    assert completed.status_code == 202
    job_id = completed.json()["job_id"]
    await run_upload_batch_job(
        app.state.settings,
        app.state.session_factory,
        job_id,
        [upload_id],
    )
    return dataset_id, job_id


@pytest.mark.skipif(
    not COCO_ARCHIVE.is_file() or not VOC_ARCHIVE.is_file(),
    reason="real job 775 source archives are unavailable",
)
async def test_real_job_775_coco_and_voc_uploads_cross_validate(
    client: httpx.AsyncClient,
    app,
) -> None:
    coco_dataset_id, coco_job_id = await upload_real_archive(
        client,
        app,
        COCO_ARCHIVE,
    )
    voc_dataset_id, voc_job_id = await upload_real_archive(
        client,
        app,
        VOC_ARCHIVE,
    )

    async with app.state.session_factory() as session:
        results: list[tuple[int, int, set[str]]] = []
        for dataset_id in (coco_dataset_id, voc_dataset_id):
            dataset = await session.get(Dataset, dataset_id)
            assert dataset is not None
            class_names = set(
                await session.scalars(
                    select(DatasetClass.name).where(
                        DatasetClass.dataset_id == dataset_id
                    )
                )
            )
            results.append(
                (
                    dataset.image_count,
                    dataset.annotation_count,
                    class_names,
                )
            )
        assert results[0] == results[1]
        assert results[0] == (120, 1016, {"person", "orklift"})
        assert not await session.scalar(
            select(ImportIssue).where(
                ImportIssue.job_id == coco_job_id,
                ImportIssue.path.endswith(".json"),
                ImportIssue.kind == "ignored_file",
            )
        )
        assert not await session.scalar(
            select(ImportIssue).where(
                ImportIssue.job_id == voc_job_id,
                ImportIssue.path.endswith(".xml"),
                ImportIssue.kind == "ignored_file",
            )
        )

    coco_http = await client.get(
        f"/api/datasets/{coco_dataset_id}/images?offset=0&limit=200"
    )
    voc_http = await client.get(
        f"/api/datasets/{voc_dataset_id}/images?offset=0&limit=200"
    )
    assert coco_http.status_code == voc_http.status_code == 200
    assert coco_http.json()["total"] == voc_http.json()["total"] == 120


@pytest.mark.skipif(
    not YOLO_ARCHIVE.is_file(),
    reason="real CVAT YOLO source archive is unavailable",
)
async def test_real_cvat_yolo_upload_path_is_unchanged(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id, job_id = await upload_real_archive(
        client,
        app,
        YOLO_ARCHIVE,
    )

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        classes = set(
            await session.scalars(
                select(DatasetClass.name).where(
                    DatasetClass.dataset_id == dataset_id
                )
            )
        )
        blocking_issues = (
            await session.scalars(
                select(ImportIssue.kind).where(
                    ImportIssue.job_id == job_id,
                    ImportIssue.kind.in_(
                        {
                            "broken_label",
                            "broken_image",
                            "image_without_label",
                            "label_without_image",
                            "rejected_file",
                        }
                    ),
                )
            )
        ).all()
        assert dataset.image_count == 151
        assert dataset.annotation_count > 0
        assert classes == {"person", "orklift"}
        assert blocking_issues == []

    response = await client.get(
        f"/api/datasets/{dataset_id}/images?offset=0&limit=200"
    )
    assert response.status_code == 200
    assert response.json()["total"] == 151
