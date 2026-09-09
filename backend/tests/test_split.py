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
    training_group_key,
    training_source_key,
    validate_split_size,
)
from app.services.storage import storage_relative_path
from tests.factories import image_with_media


def _snapshot(
    image_id: int,
    classes: tuple[int, ...],
    *,
    source_key: str = "dataset:test",
    group_key: str | None = None,
    filename: str | None = None,
) -> ImageSnapshot:
    resolved_filename = filename or f"image-{image_id}.jpg"
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
        filename=resolved_filename,
        rel_path=f"incoming/image-{image_id}.jpg",
        file_path=Path(f"/source/image-{image_id}.jpg"),
        annotations=annotations,
        training_source_key=source_key,
        training_group_key=group_key or f"image:{source_key}:{image_id}",
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


def test_training_keys_share_upload_parts_and_detect_frame_sequences() -> None:
    upload_group_id = uuid4()
    source = training_source_key(12, upload_group_id)

    assert source == f"upload:{upload_group_id}"
    assert training_source_key(12, None) == "dataset:12"
    first = training_group_key(
        source,
        filename="2026-02-11 11_13_19 PT-C27_000001.jpg",
        rel_path="images/2026-02-11 11_13_19 PT-C27_000001.jpg",
    )
    second = training_group_key(
        source,
        filename="2026-02-11 11_13_19 PT-C27_000002.jpg",
        rel_path="images/2026-02-11 11_13_19 PT-C27_000002.jpg",
    )
    other_camera = training_group_key(
        source,
        filename="2026-02-11 11_13_19 PT-C28_000001.jpg",
        rel_path="images/2026-02-11 11_13_19 PT-C28_000001.jpg",
    )

    assert first == second
    assert first != other_camera


def test_grouped_split_keeps_sequences_atomic_and_balances_each_source() -> None:
    images: list[ImageSnapshot] = []
    image_id = 1
    source_group_sizes = {
        "upload:source-a": [10] * 10,
        "dataset:source-b": [4] * 10,
    }
    for source_key, group_sizes in source_group_sizes.items():
        for group_index, group_size in enumerate(group_sizes):
            group_key = f"sequence:{source_key}:camera-{group_index}"
            for _ in range(group_size):
                images.append(
                    _snapshot(
                        image_id,
                        (0,),
                        source_key=source_key,
                        group_key=group_key,
                    )
                )
                image_id += 1

    result = allocate_splits(
        images,
        {"train": 0.7, "valid": 0.2, "test": 0.1},
        seed=1985425369,
    )

    split_by_group: dict[str, set[str]] = {}
    source_counts: dict[str, Counter[str]] = {
        source: Counter() for source in source_group_sizes
    }
    for split, rows in result.assignments.items():
        for image in rows:
            split_by_group.setdefault(image.training_group_key, set()).add(split)
            source_counts[image.training_source_key][split] += 1

    assert all(len(splits) == 1 for splits in split_by_group.values())
    assert source_counts == {
        "upload:source-a": Counter(train=70, valid=20, test=10),
        "dataset:source-b": Counter(train=28, valid=8, test=4),
    }


def test_grouped_split_is_seeded_and_reproducible() -> None:
    images = [
        _snapshot(
            image_id,
            (0,),
            group_key=f"sequence:dataset:test:clip-{image_id // 2}",
        )
        for image_id in range(24)
    ]

    first = allocate_splits(
        images,
        {"train": 0.5, "valid": 0.5},
        seed=7,
    )
    repeated = allocate_splits(
        images,
        {"train": 0.5, "valid": 0.5},
        seed=7,
    )
    reordered = allocate_splits(
        list(reversed(images)),
        {"train": 0.5, "valid": 0.5},
        seed=7,
    )
    changed = allocate_splits(
        images,
        {"train": 0.5, "valid": 0.5},
        seed=8,
    )

    memberships = lambda result: {
        split: tuple(image.id for image in rows)
        for split, rows in result.assignments.items()
    }
    assert memberships(first) == memberships(repeated)
    assert memberships(first) == memberships(reordered)
    assert memberships(first) != memberships(changed)


def test_grouped_split_rejects_a_single_sequence_for_train_and_valid() -> None:
    images = [
        _snapshot(
            image_id,
            (0,),
            group_key="sequence:dataset:test:only-clip",
        )
        for image_id in range(20)
    ]

    with pytest.raises(ValueError, match="group"):
        allocate_splits(
            images,
            {"train": 0.8, "valid": 0.2},
            seed=1,
        )


def test_test_only_image_forces_its_whole_sequence_into_test() -> None:
    shared_group = "sequence:dataset:test:partially-unlabeled"
    labeled = [
        _snapshot(
            image_id,
            (0,),
            group_key=shared_group if image_id <= 3 else f"image:test:{image_id}",
        )
        for image_id in range(1, 13)
    ]
    unlabeled = _snapshot(
        99,
        (),
        group_key=shared_group,
    )

    result = allocate_splits(
        labeled,
        {"train": 0.6, "valid": 0.2, "test": 0.2},
        seed=5,
        test_only_images=[unlabeled],
    )

    test_ids = {image.id for image in result.assignments["test"]}
    assert {1, 2, 3, 99} <= test_ids


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
