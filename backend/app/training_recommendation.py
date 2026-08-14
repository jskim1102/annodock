"""Deterministic RTX 3090 recommendations for YOLO26 detection training."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass


POLICY_VERSION = "rtx3090-detect-v1"
_MODEL_SIZE = re.compile(r"^yolo26([nsmxl])\.pt$")
_BATCH_AT_640 = {"n": 64, "s": 32, "m": 16, "l": 8, "x": 4}
_COMMON_BATCHES = {
    512: {"n": 96, "s": 48, "m": 24, "l": 12, "x": 6},
    640: _BATCH_AT_640,
    768: {"n": 44, "s": 22, "m": 10, "l": 5, "x": 2},
    960: {"n": 28, "s": 14, "m": 6, "l": 3, "x": 1},
    1280: {"n": 16, "s": 8, "m": 4, "l": 2, "x": 1},
}


@dataclass(frozen=True)
class TrainingDatasetProfile:
    image_count: int
    annotation_count: int
    small_object_ratio: float


@dataclass(frozen=True)
class TrainingRecommendation:
    policy_version: str
    train_images: int
    total_instances: int
    instances_per_image: float
    small_object_ratio: float
    epochs: int
    imgsz: int
    batch: int
    optimizer: str
    lr0: float
    warmup_epochs: float
    patience: int
    mosaic: float
    mixup: float
    scale: float
    amp: bool
    close_mosaic: int
    copy_paste: float
    compile: bool
    effective_max_imgsz: int
    reasons: tuple[str, ...]


def _model_size(weights: str) -> str:
    match = _MODEL_SIZE.fullmatch(weights)
    if match is None:
        raise ValueError("unsupported YOLO26 preset")
    return match.group(1)


def _stride_ceil(value: float) -> int:
    return max(32, int(math.ceil(value / 32.0) * 32))


def effective_max_imgsz(imgsz: int, multi_scale: float) -> int:
    """Return the largest stride-aligned shape multi-scale may generate."""
    if imgsz < 1:
        raise ValueError("imgsz must be positive")
    if not 0 <= multi_scale <= 1:
        raise ValueError("multi_scale must be between zero and one")
    return _stride_ceil(imgsz * (1.0 + multi_scale))


def _recommended_batch(model_size: str, max_imgsz: int) -> int:
    common = _COMMON_BATCHES.get(max_imgsz)
    if common is not None:
        return common[model_size]
    raw = _BATCH_AT_640[model_size] * (640.0 / max_imgsz) ** 2
    if raw >= 8:
        # Even batches are easier to compare across repeated runs while keeping
        # a little headroom for augmentation peaks on a display-attached 3090.
        return max(2, int(raw) // 2 * 2)
    return max(1, int(raw))


def _schedule(train_images: int) -> tuple[int, int, float]:
    if train_images < 1_000:
        return 250, 50, 0.0
    if train_images < 5_000:
        return 200, 45, 0.0
    if train_images < 20_000:
        return 150, 40, 0.05
    if train_images < 50_000:
        return 120, 35, 0.1
    return 100, 30, 0.1


def recommend_training(
    *,
    profile: TrainingDatasetProfile,
    weights: str,
    requested_imgsz: int,
    multi_scale: float,
    train_ratio: float,
) -> TrainingRecommendation:
    """Build an editable starting recipe, not a claim of optimal accuracy."""
    if not 0 < train_ratio <= 1:
        raise ValueError("train_ratio must be greater than zero and at most one")
    if not 0 <= profile.small_object_ratio <= 1:
        raise ValueError("small_object_ratio must be between zero and one")

    model_size = _model_size(weights)
    train_images = max(1, int(round(profile.image_count * train_ratio)))
    epochs, patience, mixup = _schedule(train_images)
    recommended_imgsz = requested_imgsz
    reasons = [
        f"train 이미지 {train_images:,}장 기준 epochs와 patience를 조정했습니다.",
    ]
    if profile.small_object_ratio >= 0.3 and requested_imgsz < 960:
        recommended_imgsz = 960
        reasons.append(
            "면적 1% 이하 작은 객체가 30% 이상이라 imgsz를 960으로 높였습니다."
        )

    max_imgsz = effective_max_imgsz(recommended_imgsz, multi_scale)
    batch = _recommended_batch(model_size, max_imgsz)
    reasons.append(
        f"RTX 3090 24GB와 최대 해상도 {max_imgsz} 기준 batch {batch}를 권장합니다."
    )
    if multi_scale > 0:
        reasons.append("Multi-scale 동적 shape 재컴파일을 피하도록 Compile을 껐습니다.")

    images = max(1, profile.image_count)
    return TrainingRecommendation(
        policy_version=POLICY_VERSION,
        train_images=train_images,
        total_instances=profile.annotation_count,
        instances_per_image=round(profile.annotation_count / images, 4),
        small_object_ratio=round(profile.small_object_ratio, 4),
        epochs=epochs,
        imgsz=recommended_imgsz,
        batch=batch,
        optimizer="auto",
        lr0=0.01,
        warmup_epochs=3.0,
        patience=patience,
        mosaic=1.0,
        mixup=mixup,
        scale=0.5,
        amp=True,
        close_mosaic=10,
        copy_paste=0.0,
        compile=multi_scale == 0,
        effective_max_imgsz=max_imgsz,
        reasons=tuple(reasons),
    )
