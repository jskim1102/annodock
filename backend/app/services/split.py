"""Deterministic class-presence stratified splits for training runs."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Annotation, Image, RunImage
from app.services.storage import contained_storage_path


VALID_ALL_BACKGROUND_WARNING = (
    "valid 분할의 모든 이미지가 background입니다. "
    "검증 지표가 유효하지 않을 수 있으니 분할 비율과 라벨을 확인하세요."
)


@dataclass(frozen=True)
class AnnotationSnapshot:
    class_id: int
    cx: float
    cy: float
    w: float
    h: float


@dataclass(frozen=True)
class ImageSnapshot:
    id: int
    stem: str
    filename: str
    rel_path: str
    file_path: Path
    annotations: tuple[AnnotationSnapshot, ...]
    has_label_source: bool = True

    @property
    def class_ids(self) -> tuple[int, ...]:
        return tuple(sorted({annotation.class_id for annotation in self.annotations}))

    @property
    def is_unlabeled(self) -> bool:
        """Whether the image has neither a label source nor saved boxes."""
        return not self.has_label_source and not self.annotations


@dataclass(frozen=True)
class SplitResult:
    assignments: dict[str, tuple[ImageSnapshot, ...]]
    warnings: tuple[str, ...]


def validate_split_size(image_count: int, ratios: Mapping[str, float]) -> None:
    """Reject only when floor allocation makes train or valid empty."""
    minimum_required_ratio = min(ratios["train"], ratios["valid"])
    if math.floor(image_count * minimum_required_ratio) == 0:
        raise ValueError("train or valid would contain zero images")


def _largest_remainder_counts(
    total: int,
    ratios: Mapping[str, float],
) -> dict[str, int]:
    if not math.isclose(sum(ratios.values()), 1.0, abs_tol=1e-9):
        raise ValueError("split ratios must sum to one")

    names = tuple(ratios)
    exact = {name: total * ratios[name] for name in names}
    counts = {name: math.floor(exact[name]) for name in names}
    remainder = total - sum(counts.values())
    ranked = sorted(
        names,
        key=lambda name: (-(exact[name] - counts[name]), names.index(name)),
    )
    for name in ranked[:remainder]:
        counts[name] += 1
    return counts


def allocate_splits(
    images: Sequence[ImageSnapshot],
    ratios: Mapping[str, float],
    *,
    seed: int,
    test_only_images: Sequence[ImageSnapshot] = (),
) -> SplitResult:
    """Split each class-presence signature bucket by largest remainder."""
    if test_only_images and "test" not in ratios:
        raise ValueError("test-only images require a test split")
    validate_split_size(len(images), ratios)
    buckets: dict[tuple[int, ...], list[ImageSnapshot]] = defaultdict(list)
    for image in images:
        buckets[image.class_ids].append(image)

    assignments: dict[str, list[ImageSnapshot]] = {
        split: [] for split in ratios
    }
    generator = random.Random(seed)
    for signature in sorted(buckets):
        bucket = sorted(buckets[signature], key=lambda image: image.id)
        generator.shuffle(bucket)
        counts = _largest_remainder_counts(len(bucket), ratios)
        cursor = 0
        for split in ratios:
            end = cursor + counts[split]
            assignments[split].extend(bucket[cursor:end])
            cursor = end

    assignments.get("test", []).extend(
        sorted(test_only_images, key=lambda image: image.id)
    )

    warnings: list[str] = []
    valid_images = assignments["valid"]
    if valid_images and all(not image.class_ids for image in valid_images):
        warnings.append(VALID_ALL_BACKGROUND_WARNING)
    return SplitResult(
        assignments={
            split: tuple(rows) for split, rows in assignments.items()
        },
        warnings=tuple(warnings),
    )


async def load_dataset_images(
    session: AsyncSession,
    dataset_id: int,
    storage_dir: Path,
    *,
    exclude_unlabeled_images: bool = False,
) -> list[ImageSnapshot]:
    """Load immutable image and annotation values needed by a run."""
    statement = (
        select(Image, Annotation)
        .outerjoin(Annotation, Annotation.image_id == Image.id)
        .where(Image.dataset_id == dataset_id)
    )
    if exclude_unlabeled_images:
        statement = statement.where(
            or_(
                Image.has_label_source.is_(True),
                Annotation.id.is_not(None),
            )
        )
    rows = (
        await session.execute(
            statement.order_by(Image.id, Annotation.id)
        )
    ).all()
    images: dict[int, Image] = {}
    annotations: dict[int, list[AnnotationSnapshot]] = defaultdict(list)
    for image, annotation in rows:
        images[image.id] = image
        if annotation is not None:
            annotations[image.id].append(
                AnnotationSnapshot(
                    class_id=annotation.class_id,
                    cx=annotation.cx,
                    cy=annotation.cy,
                    w=annotation.w,
                    h=annotation.h,
                )
            )
    return [
        ImageSnapshot(
            id=image.id,
            stem=image.stem,
            filename=image.filename,
            rel_path=image.rel_path,
            file_path=contained_storage_path(storage_dir, image.file_path),
            annotations=tuple(annotations[image.id]),
            has_label_source=image.has_label_source,
        )
        for image in images.values()
    ]


def persist_run_images(
    session: AsyncSession,
    run_id: int,
    assignments: Mapping[str, Sequence[ImageSnapshot]],
) -> None:
    """Persist run-local split membership without touching Image.split."""
    session.add_all(
        RunImage(
            run_id=run_id,
            image_id=image.id,
            split=split,
            stem=image.stem,
            filename=image.filename,
            rel_path=image.rel_path,
        )
        for split, images in assignments.items()
        for image in images
    )
