from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from PIL import Image as PillowImage
from sqlalchemy import select, text

from app.models import Annotation, Dataset, Image, ImportIssue, UploadJob
from app.services.collect import CollectedFile
from app.services.ingest import _index_pairable_items, ingest_collected


pytestmark = pytest.mark.asyncio


def collected(
    path: Path,
    rel_path: str,
    kind: str,
    split: str | None,
) -> CollectedFile:
    return CollectedFile(
        rel_path=rel_path,
        abs_path=path,
        kind=kind,  # type: ignore[arg-type]
        split=split,
    )


def make_jpeg(path: Path, color: tuple[int, int, int]) -> None:
    PillowImage.new("RGB", (48, 32), color).save(path, "JPEG")


async def create_dataset_and_job(
    client: httpx.AsyncClient,
    app,
) -> tuple[int, int]:
    response = await client.post(
        "/api/datasets",
        json={"name": f"test-pairing-{uuid4().hex}"},
    )
    assert response.status_code == 201
    dataset_id = response.json()["id"]
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


async def test_pairing_index_uses_only_split_and_stem(tmp_path: Path) -> None:
    paths = {
        "train_image": tmp_path / "train.jpg",
        "train_label": tmp_path / "train.txt",
        "val_image": tmp_path / "val.jpg",
        "val_label": tmp_path / "val.txt",
        "flat_image": tmp_path / "flat.jpg",
        "flat_label": tmp_path / "flat.txt",
    }
    items = [
        collected(paths["val_label"], "labels/val/a.txt", "label", "val"),
        collected(paths["flat_image"], "obj_train_data/b.jpg", "image", None),
        collected(paths["train_image"], "images/train/a.jpg", "image", "train"),
        collected(paths["flat_label"], "obj_train_data/b.txt", "label", None),
        collected(paths["val_image"], "images/val/a.jpg", "image", "val"),
        collected(paths["train_label"], "labels/train/a.txt", "label", "train"),
    ]

    images, labels, issues = _index_pairable_items(items)

    assert set(images) == {("train", "a"), ("val", "a"), (None, "b")}
    assert set(labels) == {("train", "a"), ("val", "a"), (None, "b")}
    assert issues == []


async def test_duplicate_image_stems_keep_each_image_and_matching_label(
    tmp_path: Path,
) -> None:
    first_image = tmp_path / "first.jpg"
    second_image = tmp_path / "second.png"
    first_label = tmp_path / "first.txt"
    second_label = tmp_path / "second.txt"
    items = [
        collected(
            second_label,
            "camera_b/train/0001.txt",
            "label",
            "train",
        ),
        collected(
            first_image,
            "camera_a/train/0001.jpg",
            "image",
            "train",
        ),
        collected(
            second_image,
            "camera_b/train/0001.png",
            "image",
            "train",
        ),
        collected(
            first_label,
            "camera_a/train/0001.txt",
            "label",
            "train",
        ),
    ]

    images, labels, issues = _index_pairable_items(items)

    assert list(images) == [("train", "0001"), ("train", "0001 (1)")]
    assert list(labels) == [("train", "0001"), ("train", "0001 (1)")]
    assert images[("train", "0001")].rel_path == "camera_a/train/0001.jpg"
    assert images[("train", "0001 (1)")].rel_path == (
        "camera_b/train/0001.png"
    )
    assert labels[("train", "0001")].rel_path == "camera_a/train/0001.txt"
    assert labels[("train", "0001 (1)")].rel_path == (
        "camera_b/train/0001.txt"
    )
    assert issues == []


async def test_duplicate_image_suffix_does_not_claim_an_incoming_name(
    tmp_path: Path,
) -> None:
    first = collected(
        tmp_path / "first.jpg",
        "a/train/0001.jpg",
        "image",
        "train",
    )
    duplicate = collected(
        tmp_path / "duplicate.jpg",
        "b/train/0001.jpg",
        "image",
        "train",
    )
    already_suffixed = collected(
        tmp_path / "suffixed.jpg",
        "c/train/0001 (1).jpg",
        "image",
        "train",
    )

    images, _labels, _issues = _index_pairable_items(
        [first, duplicate, already_suffixed]
    )

    assert set(images) == {
        ("train", "0001"),
        ("train", "0001 (1)"),
        ("train", "0001 (2)"),
    }
    assert images[("train", "0001 (1)")] is already_suffixed
    assert images[("train", "0001 (2)")] is duplicate


async def test_duplicate_image_stems_are_renamed_stored_and_counted(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    dataset_id, job_id = await create_dataset_and_job(client, app)
    first_image = tmp_path / "first.jpg"
    second_image = tmp_path / "second.png"
    first_label = tmp_path / "first.txt"
    second_label = tmp_path / "second.txt"
    make_jpeg(first_image, (20, 40, 60))
    PillowImage.new("RGB", (48, 32), (80, 100, 120)).save(
        second_image,
        "PNG",
    )
    first_label.write_text("0 0.2 0.5 0.2 0.2\n", encoding="utf-8")
    second_label.write_text("1 0.8 0.5 0.2 0.2\n", encoding="utf-8")

    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        [
            collected(
                second_label,
                "camera_b/train/0001.txt",
                "label",
                "train",
            ),
            collected(
                first_image,
                "camera_a/train/0001.jpg",
                "image",
                "train",
            ),
            collected(
                second_image,
                "camera_b/train/0001.png",
                "image",
                "train",
            ),
            collected(
                first_label,
                "camera_a/train/0001.txt",
                "label",
                "train",
            ),
        ],
    )

    async with app.state.session_factory() as session:
        images = (
            await session.scalars(
                select(Image)
                .where(Image.dataset_id == dataset_id)
                .order_by(Image.stem)
            )
        ).all()
        annotations = (
            await session.execute(
                select(Image.stem, Annotation.class_id, Annotation.cx)
                .join(Annotation, Annotation.image_id == Image.id)
                .where(Image.dataset_id == dataset_id)
                .order_by(Image.stem)
            )
        ).all()
        collision_issues = (
            await session.scalars(
                select(ImportIssue).where(
                    ImportIssue.job_id == job_id,
                    ImportIssue.kind == "duplicate_skipped",
                )
            )
        ).all()

    assert [(image.stem, image.filename, image.rel_path) for image in images] == [
        ("0001", "0001.jpg", "camera_a/train/0001.jpg"),
        ("0001 (1)", "0001 (1).png", "camera_b/train/0001 (1).png"),
    ]
    assert annotations == [
        ("0001", 0, 0.2),
        ("0001 (1)", 1, 0.8),
    ]
    assert len(collision_issues) == 1
    assert collision_issues[0].path == "camera_b/train/0001.png"
    assert "stored as camera_b/train/0001 (1).png" in collision_issues[0].detail


async def test_flat_cvat_layout_still_pairs_image_and_label(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    dataset_id, job_id = await create_dataset_and_job(client, app)
    image = tmp_path / "flat.jpg"
    label = tmp_path / "flat.txt"
    make_jpeg(image, (20, 40, 60))
    label.write_text("0 0.5 0.5 0.2 0.2\n", encoding="utf-8")

    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        [
            collected(image, "obj_train_data/flat.jpg", "image", None),
            collected(label, "obj_train_data/flat.txt", "label", None),
        ],
    )

    async with app.state.session_factory() as session:
        stored = await session.scalar(
            select(Image).where(Image.dataset_id == dataset_id)
        )
        assert stored is not None
        assert (stored.split, stored.stem, stored.box_count) == (None, "flat", 1)
        issues = (
            await session.scalars(
                select(ImportIssue).where(ImportIssue.job_id == job_id)
            )
        ).all()
        assert not {
            issue.kind
            for issue in issues
            if issue.kind in {"image_without_label", "label_without_image"}
        }


async def test_duplicate_labels_keep_lexicographic_first_and_report_rest(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    dataset_id, job_id = await create_dataset_and_job(client, app)
    image = tmp_path / "duplicate.jpg"
    first_label = tmp_path / "first.txt"
    later_label = tmp_path / "later.txt"
    make_jpeg(image, (60, 40, 20))
    first_label.write_text("0 0.5 0.5 0.2 0.2\n", encoding="utf-8")
    later_label.write_text("1 0.5 0.5 0.4 0.4\n", encoding="utf-8")

    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        [
            collected(later_label, "labels/train/a.txt", "label", "train"),
            collected(image, "images/train/a.jpg", "image", "train"),
            collected(
                first_label,
                "annotations/train/a.txt",
                "label",
                "train",
            ),
        ],
    )

    async with app.state.session_factory() as session:
        annotation = await session.scalar(
            select(Annotation)
            .join(Image)
            .where(Image.dataset_id == dataset_id)
        )
        assert annotation is not None
        assert annotation.class_id == 0
        ignored = (
            await session.scalars(
                select(ImportIssue).where(
                    ImportIssue.job_id == job_id,
                    ImportIssue.kind == "ignored_file",
                )
            )
        ).all()
        assert len(ignored) == 1
        assert ignored[0].path == "labels/train/a.txt"
        assert "annotations/train/a.txt" in ignored[0].detail


async def test_rejected_image_makes_its_label_unmatched(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    dataset_id, job_id = await create_dataset_and_job(client, app)
    image = tmp_path / "bad.jpg"
    label = tmp_path / "bad.txt"
    image.write_bytes(b"not a jpeg")
    label.write_text("0 0.5 0.5 0.2 0.2\n", encoding="utf-8")

    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        [
            collected(image, "images/train/bad.jpg", "image", "train"),
            collected(label, "labels/train/bad.txt", "label", "train"),
        ],
    )

    async with app.state.session_factory() as session:
        issues = (
            await session.scalars(
                select(ImportIssue).where(ImportIssue.job_id == job_id)
            )
        ).all()
        assert {issue.kind for issue in issues} >= {
            "broken_image",
            "label_without_image",
        }

    response = await client.get(f"/api/datasets/{dataset_id}/issues")
    assert response.status_code == 200
    assert {item["kind"] for item in response.json()["items"]} >= {
        "broken_image",
        "label_without_image",
    }


async def test_same_stem_in_different_splits_is_stored_after_migration(
    client: httpx.AsyncClient,
    app,
    tmp_path: Path,
) -> None:
    async with app.state.session_factory() as session:
        split_constraint_exists = await session.scalar(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE conname = 'uq_images_dataset_split_stem'
                )
                """
            )
        )
    if not split_constraint_exists:
        pytest.skip("live split/stem migration is intentionally not applied yet")

    dataset_id, job_id = await create_dataset_and_job(client, app)
    train_image = tmp_path / "train.jpg"
    val_image = tmp_path / "val.jpg"
    train_label = tmp_path / "train.txt"
    val_label = tmp_path / "val.txt"
    make_jpeg(train_image, (255, 0, 0))
    make_jpeg(val_image, (0, 255, 0))
    train_label.write_text("0 0.5 0.5 0.2 0.2\n", encoding="utf-8")
    val_label.write_text("1 0.5 0.5 0.2 0.2\n", encoding="utf-8")

    await ingest_collected(
        app.state.settings,
        app.state.session_factory,
        job_id,
        [
            collected(train_image, "images/train/0001.jpg", "image", "train"),
            collected(train_label, "labels/train/0001.txt", "label", "train"),
            collected(val_image, "images/val/0001.jpg", "image", "val"),
            collected(val_label, "labels/val/0001.txt", "label", "val"),
        ],
    )

    async with app.state.session_factory() as session:
        images = (
            await session.scalars(
                select(Image)
                .where(Image.dataset_id == dataset_id)
                .order_by(Image.split)
            )
        ).all()
        assert [(image.split, image.stem) for image in images] == [
            ("train", "0001"),
            ("val", "0001"),
        ]
        issues = (
            await session.scalars(
                select(ImportIssue).where(ImportIssue.job_id == job_id)
            )
        ).all()
        assert not any(
            issue.kind == "duplicate_skipped" for issue in issues
        )
