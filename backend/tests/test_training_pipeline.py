"""CPU regressions against the installed Ultralytics data pipeline."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from PIL import Image as PillowImage
from ultralytics.cfg import get_cfg
from ultralytics.data.augment import Albumentations
from ultralytics.data.build import build_yolo_dataset

from app.models import Annotation, Image
from app.services.derive import _prepare_image_sync
from app.services.rundir import build_run_directory
from app.services.split import load_dataset_images, persist_run_images
from app.training_params import (
    RTX_3090_TRAINING_ARGS,
    normalize_training_args,
    ultralytics_training_args,
)
from app.worker import train_worker


def _pixels() -> np.ndarray:
    y, x = np.indices((64, 64))
    return np.stack((x * 4, y * 4, (x + y) * 2), axis=-1).astype(np.uint8)


def _dataset(tmp_path: Path, arguments: dict[str, object]):
    images = tmp_path / "images"
    labels = tmp_path / "labels"
    images.mkdir()
    labels.mkdir()
    PillowImage.fromarray(_pixels()).save(images / "sample.png")
    (labels / "sample.txt").write_text("0 0.25 0.5 0.25 0.5\n")
    return build_yolo_dataset(
        get_cfg(overrides={**arguments, "imgsz": 64, "cache": False}),
        str(images),
        1,
        {"names": {0: "object"}, "nc": 1},
        mode="train",
    )


def test_disabled_augmentation_preserves_actual_training_pixels_and_boxes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Exercise the optional-installed-library path too. An explicit empty list
    # must bypass Albumentations' default Blur/CLAHE construction entirely.
    class Compose:
        def __init__(self, transforms):
            self.transforms = transforms

        def __call__(self, **labels):
            return labels

    monkeypatch.setitem(
        sys.modules,
        "albumentations",
        SimpleNamespace(__version__="1.0.3", Compose=Compose),
    )
    persisted = {**RTX_3090_TRAINING_ARGS, "augment": False, "mixup": 1.0}
    dataset = _dataset(tmp_path, ultralytics_training_args(persisted))
    optional = next(
        transform for transform in dataset.transforms.transforms
        if isinstance(transform, Albumentations)
    )
    assert optional.transform is not None
    assert optional.transform.transforms == []
    for _ in range(8):
        sample = dataset[0]
        np.testing.assert_array_equal(sample["img"].numpy(), _pixels().transpose(2, 0, 1))
        np.testing.assert_allclose(sample["bboxes"].numpy(), [[0.25, 0.5, 0.25, 0.5]])
    # Runtime translation must preserve the user's saved recipe.
    assert persisted["mosaic"] == 1.0
    assert persisted["mixup"] == 1.0


def test_enabled_augmentation_applies_configured_horizontal_flip(tmp_path: Path) -> None:
    persisted = {
        **RTX_3090_TRAINING_ARGS,
        "augment": True,
        "mosaic": 0.0,
        "scale": 0.0,
        "translate": 0.0,
        "hsv_h": 0.0,
        "hsv_s": 0.0,
        "hsv_v": 0.0,
        "fliplr": 1.0,
    }
    dataset = _dataset(tmp_path, ultralytics_training_args(persisted))
    sample = dataset[0]
    np.testing.assert_array_equal(sample["img"].numpy(), _pixels()[:, ::-1].transpose(2, 0, 1))
    np.testing.assert_allclose(sample["bboxes"].numpy(), [[0.75, 0.5, 0.25, 0.5]])


@pytest.mark.parametrize("persisted", [None, {}, {"cache": "none"}])
def test_worker_preserves_legacy_augmentation_defaults(
    persisted,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = MagicMock()
    connection.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchone.return_value = {
        "id": 1, "owner_id": 1, "weights": "yolo26n.pt", "epochs": 1,
        "imgsz": 64, "batch": 1, "training_args": persisted,
        "out_dir": "training-runs/1",
    }
    monkeypatch.setattr(train_worker.psycopg, "connect", lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(train_worker, "get_settings", lambda: SimpleNamespace(storage_dir=tmp_path))
    config = train_worker.load_run_config(1, 1, "unused")
    arguments = ultralytics_training_args(config.training_args)
    assert arguments["mosaic"] == 1.0
    assert arguments["fliplr"] == 0.5
    assert arguments["scale"] == 0.5
    assert "augmentations" not in arguments
    reported = normalize_training_args(persisted)
    assert reported["augment"] is True
    assert ultralytics_training_args(reported) == arguments


@pytest.mark.parametrize("extension, image_format", [("heif", "HEIF"), ("png", "PNG")])
def test_uploaded_images_load_in_real_training_pipeline(
    tmp_path: Path,
    extension: str,
    image_format: str,
) -> None:
    filename = f"photo.{extension}"
    source = tmp_path / filename
    PillowImage.fromarray(_pixels()).save(source, format=image_format)
    original_bytes = source.read_bytes()
    prepared = _prepare_image_sync(source, tmp_path / "stored", filename, False)
    original = tmp_path / "stored" / prepared.file_relative
    display = (
        tmp_path / "stored" / prepared.display_relative
        if prepared.display_relative is not None else None
    )
    image = Image(
        id=1, dataset_id=1, stem="photo", filename=filename, rel_path=filename,
        file_path=str(original), display_path=str(display) if display else None,
        has_label_source=True, training_source_key="source", training_group_key="image:1",
    )
    annotation = Annotation(id=1, image_id=1, class_id=0, cx=0.25, cy=0.5, w=0.25, h=0.5)
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(all=lambda: [(image, annotation)])
    snapshots = asyncio.run(load_dataset_images(session, 1, tmp_path))
    assignments = {"train": tuple(snapshots), "valid": ()}
    build_run_directory(tmp_path / "run", assignments, {0: "object"}, split_mode="2way")
    run_images = MagicMock()
    persist_run_images(run_images, 1, assignments)
    persisted_image = list(run_images.add_all.call_args.args[0])[0]
    assert persisted_image.filename == filename
    assert persisted_image.rel_path == filename
    expected = display or original
    linked = tmp_path / "run/workdir/images/train" / f"photo{expected.suffix}"
    assert linked.stat().st_ino == expected.stat().st_ino
    arguments = ultralytics_training_args({**RTX_3090_TRAINING_ARGS, "augment": False})
    dataset = build_yolo_dataset(
        get_cfg(overrides={**arguments, "imgsz": 64, "cache": False}),
        str(linked.parent), 1, {"names": {0: "object"}, "nc": 1}, mode="train",
    )
    loaded, original_shape, _resized_shape = dataset.load_image(0)
    assert loaded.shape == (64, 64, 3)
    assert original_shape == (64, 64)
    assert dataset[0]["img"].shape == (3, 64, 64)
    assert original.read_bytes() == original_bytes
