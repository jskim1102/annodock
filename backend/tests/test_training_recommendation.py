from __future__ import annotations

import pytest

from app.training_recommendation import (
    TrainingDatasetProfile,
    effective_max_imgsz,
    recommend_training,
)


def _profile(
    *,
    images: int = 1_252,
    annotations: int = 5_986,
    small_ratio: float = 0.1,
) -> TrainingDatasetProfile:
    return TrainingDatasetProfile(
        image_count=images,
        annotation_count=annotations,
        small_object_ratio=small_ratio,
    )


def test_rtx3090_recommendation_uses_model_resolution_and_train_image_count() -> None:
    result = recommend_training(
        profile=_profile(),
        weights="yolo26s.pt",
        requested_imgsz=640,
        multi_scale=0.0,
        train_ratio=0.7,
    )

    assert result.train_images == 876
    assert result.epochs == 250
    assert result.patience == 50
    assert result.batch == 32
    assert result.optimizer == "auto"
    assert result.mixup == 0.0
    assert result.copy_paste == 0.0
    assert result.amp is True
    assert result.close_mosaic == 10
    assert result.compile is True


def test_small_objects_raise_resolution_and_recalculate_batch() -> None:
    result = recommend_training(
        profile=_profile(small_ratio=0.45),
        weights="yolo26m.pt",
        requested_imgsz=640,
        multi_scale=0.0,
        train_ratio=0.8,
    )

    assert result.imgsz == 960
    assert result.effective_max_imgsz == 960
    assert result.batch == 6
    assert any("작은 객체" in reason for reason in result.reasons)


def test_multi_scale_uses_largest_shape_and_disables_compile() -> None:
    result = recommend_training(
        profile=_profile(images=8_000),
        weights="yolo26m.pt",
        requested_imgsz=640,
        multi_scale=0.5,
        train_ratio=0.7,
    )

    assert effective_max_imgsz(640, 0.5) == 960
    assert result.effective_max_imgsz == 960
    assert result.batch == 6
    assert result.compile is False
    assert result.epochs == 150
    assert result.mixup == pytest.approx(0.05)


@pytest.mark.parametrize(
    ("weights", "imgsz", "expected"),
    [
        ("yolo26n.pt", 512, 96),
        ("yolo26s.pt", 640, 32),
        ("yolo26m.pt", 768, 10),
        ("yolo26l.pt", 960, 3),
        ("yolo26x.pt", 1280, 1),
    ],
)
def test_documented_rtx3090_batch_table(
    weights: str,
    imgsz: int,
    expected: int,
) -> None:
    result = recommend_training(
        profile=_profile(),
        weights=weights,
        requested_imgsz=imgsz,
        multi_scale=0.0,
        train_ratio=0.7,
    )

    assert result.batch == expected
