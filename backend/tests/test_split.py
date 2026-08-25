from __future__ import annotations

from collections import Counter
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import Dataset, Image, Project, RunImage, TrainingRun
from app.services.split import (
    AnnotationSnapshot,
    ImageSnapshot,
    allocate_splits,
    load_dataset_images,
    persist_run_images,
    validate_split_size,
)
from app.services.storage import storage_relative_path
from tests.factories import image_with_media


def _snapshot(image_id: int, classes: tuple[int, ...]) -> ImageSnapshot:
    annotations = tuple(
        AnnotationSnapshot(
            class_id=class_id,
            cx=0.5,
            cy=0.5,
            w=0.25,
            h=0.25,
        )
        for class_id in classes
    )
    return ImageSnapshot(
        id=image_id,
        stem=f"image-{image_id}",
        filename=f"image-{image_id}.jpg",
        rel_path=f"incoming/image-{image_id}.jpg",
        file_path=Path(f"/source/image-{image_id}.jpg"),
        annotations=annotations,
    )


def _signature_counts(assignments) -> dict[str, Counter[tuple[int, ...]]]:
    return {
        split: Counter(image.class_ids for image in images)
        for split, images in assignments.items()
    }


def test_split_two_way_uses_signature_buckets_and_largest_remainder() -> None:
    images = [
        *(_snapshot(index, ()) for index in range(1, 38)),
        *(_snapshot(index, (0, 1)) for index in range(38, 121)),
    ]

    result = allocate_splits(
        images,
        {"train": 0.8, "valid": 0.2},
        seed=42,
    )

    assert {split: len(rows) for split, rows in result.assignments.items()} == {
        "train": 96,
        "valid": 24,
    }
    assert _signature_counts(result.assignments) == {
        "train": Counter({(0, 1): 66, (): 30}),
        "valid": Counter({(0, 1): 17, (): 7}),
    }
    assert result.warnings == ()


def test_split_three_way_uses_largest_remainder_per_signature() -> None:
    images = [
        *(_snapshot(index, ()) for index in range(1, 38)),
        *(_snapshot(index, (0, 1)) for index in range(38, 121)),
    ]

    result = allocate_splits(
        images,
        {"train": 0.7, "valid": 0.2, "test": 0.1},
        seed=42,
    )

    assert {split: len(rows) for split, rows in result.assignments.items()} == {
        "train": 84,
        "valid": 24,
        "test": 12,
    }
    assert _signature_counts(result.assignments) == {
        "train": Counter({(0, 1): 58, (): 26}),
        "valid": Counter({(0, 1): 17, (): 7}),
        "test": Counter({(0, 1): 8, (): 4}),
    }


def test_split_floor_guard_only_considers_train_and_valid() -> None:
    with pytest.raises(ValueError):
        validate_split_size(4, {"train": 0.8, "valid": 0.2})

    validate_split_size(
        5,
        {"train": 0.8, "valid": 0.2, "test": 0.0},
    )


def test_split_valid_all_background_is_warning_not_block() -> None:
    result = allocate_splits(
        [_snapshot(index, ()) for index in range(1, 11)],
        {"train": 0.8, "valid": 0.2},
        seed=7,
    )

    assert len(result.assignments["valid"]) == 2
    assert len(result.warnings) == 1
    assert "valid" in result.warnings[0]
    assert "background" in result.warnings[0]


@pytest.mark.asyncio
async def test_split_persists_run_images_without_modifying_image_split(app) -> None:
    name = f"test-split-persist-{uuid4().hex}"
    dataset_root = app.state.settings.storage_dir / "datasets" / uuid4().hex
    dataset_root.mkdir(parents=True)
    async with app.state.session_factory() as session:
        project = Project(
            owner_id=1,
            name=f"test-split-project-{uuid4().hex}",
        )
        session.add(project)
        await session.flush()
        dataset = Dataset(
            owner_id=1,
            project_id=project.id,
            name=name,
            status="ready",
            storage_path=storage_relative_path(
                app.state.settings.storage_dir,
                dataset_root,
            ),
            image_count=5,
            annotation_count=0,
            class_count=0,
        )
        session.add(dataset)
        await session.flush()
        for index in range(5):
            source = dataset_root / f"persist-{index}.jpg"
            source.write_bytes(b"image")
            session.add(
                image_with_media(
                    owner_id=dataset.owner_id,
                    dataset_id=dataset.id,
                    stem=f"persist-{index}",
                    filename=f"persist-{index}.jpg",
                    rel_path=f"incoming/persist-{index}.jpg",
                    split="train" if index == 0 else None,
                    width=10,
                    height=10,
                    file_path=storage_relative_path(
                        app.state.settings.storage_dir,
                        source,
                    ),
                    display_path=None,
                    thumb_path=storage_relative_path(
                        app.state.settings.storage_dir,
                        dataset_root / f"thumb-{index}.jpg",
                    ),
                    box_count=0,
                )
            )
        run = TrainingRun(
            owner_id=1,
            dataset_id=dataset.id,
            dataset_name=name,
            weights="yolo26n.pt",
            epochs=1,
            imgsz=640,
            batch=-1,
            split_mode="2way",
            ratios={"train": 0.8, "valid": 0.2},
            seed=1,
            state="done",
            out_dir="/tmp/test-split-run",
        )
        session.add(run)
        await session.flush()
        snapshots = await load_dataset_images(
            session,
            dataset.id,
            app.state.settings.storage_dir,
        )
        result = allocate_splits(
            snapshots,
            {"train": 0.8, "valid": 0.2},
            seed=1,
        )
        persist_run_images(session, run.id, result.assignments)
        await session.commit()

        rows = (
            await session.scalars(
                select(RunImage)
                .where(RunImage.run_id == run.id)
                .order_by(RunImage.id)
            )
        ).all()
        original_splits = (
            await session.scalars(
                select(Image.split)
                .where(Image.dataset_id == dataset.id)
                .order_by(Image.id)
            )
        ).all()

    assert len(rows) == 5
    assert Counter(row.split for row in rows) == Counter(train=4, valid=1)
    assert original_splits == ["train", None, None, None, None]
