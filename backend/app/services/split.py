"""Deterministic, source-balanced group splits for training runs."""

from __future__ import annotations

import math
import random
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Annotation, Image, RunImage
from app.services.storage import contained_storage_path


VALID_ALL_BACKGROUND_WARNING = (
    "valid 분할의 모든 이미지가 background입니다. "
    "검증 지표가 유효하지 않을 수 있으니 분할 비율과 라벨을 확인하세요."
)

_DELIMITED_FRAME_SUFFIX = re.compile(
    r"^(?P<prefix>.+?)[_. -](?P<frame>[0-9]{3,})$"
)
_COMPACT_FRAME_SUFFIX = re.compile(
    r"^(?P<prefix>.*\D)(?P<frame>[0-9]{6,})$"
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
    training_source_key: str = ""
    training_group_key: str = ""
    has_label_source: bool = True

    @property
    def class_ids(self) -> tuple[int, ...]:
        return tuple(sorted({annotation.class_id for annotation in self.annotations}))

    @property
    def is_unlabeled(self) -> bool:
        """Whether the image has neither a label source nor saved boxes."""
        return not self.has_label_source and not self.annotations

    @property
    def allocation_source_key(self) -> str:
        return self.training_source_key or "dataset:unknown"

    @property
    def allocation_group_key(self) -> str:
        return self.training_group_key or f"image:{self.id}"


@dataclass(frozen=True)
class SplitResult:
    assignments: dict[str, tuple[ImageSnapshot, ...]]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _AllocationGroup:
    key: str
    images: tuple[ImageSnapshot, ...]
    source_counts: Counter[str]
    signature_counts: Counter[tuple[int, ...]]
    source_signature_counts: Counter[tuple[str, tuple[int, ...]]]

    @property
    def size(self) -> int:
        return len(self.images)


def training_source_key(
    dataset_id: int,
    upload_group_id: UUID | str | None,
) -> str:
    """Return the stable balancing key for a logical uploaded dataset."""

    if upload_group_id is not None:
        return f"upload:{upload_group_id}"
    return f"dataset:{dataset_id}"


def _normalized_path(value: str) -> str:
    return unicodedata.normalize("NFKC", value.replace("\\", "/")).casefold()


def training_group_key(
    source_key: str,
    *,
    filename: str,
    rel_path: str,
    media_object_id: int | None = None,
) -> str:
    """Infer an atomic split group from a frame filename or immutable media.

    Delimited counters with at least three digits (``clip_000123``) and compact
    counters with at least six digits are treated as video frames. Ordinary
    still-image names remain independent. The source path is included for
    sequences so two unrelated folders named ``frame_000001`` do not collapse
    into one oversized group.
    """

    normalized_filename = _normalized_path(Path(filename).stem)
    match = _DELIMITED_FRAME_SUFFIX.match(normalized_filename)
    if match is None:
        match = _COMPACT_FRAME_SUFFIX.match(normalized_filename)
    if match is not None:
        normalized_rel_path = _normalized_path(rel_path)
        parent = PurePosixPath(normalized_rel_path).parent.as_posix()
        location = (
            match.group("prefix")
            if parent == "."
            else f"{parent}/{match.group('prefix')}"
        )
        return f"sequence:{source_key}:{location}"
    if media_object_id is not None:
        return f"media:{media_object_id}"
    return f"image:{source_key}:{_normalized_path(rel_path)}"


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


def _build_allocation_groups(
    images: Sequence[ImageSnapshot],
) -> dict[str, _AllocationGroup]:
    grouped: dict[str, list[ImageSnapshot]] = defaultdict(list)
    for image in images:
        grouped[image.allocation_group_key].append(image)
    result: dict[str, _AllocationGroup] = {}
    for key, rows in grouped.items():
        ordered = tuple(sorted(rows, key=lambda image: image.id))
        source_counts = Counter(
            image.allocation_source_key for image in ordered
        )
        signature_counts = Counter(image.class_ids for image in ordered)
        result[key] = _AllocationGroup(
            key=key,
            images=ordered,
            source_counts=source_counts,
            signature_counts=signature_counts,
            source_signature_counts=Counter(
                (image.allocation_source_key, image.class_ids)
                for image in ordered
            ),
        )
    return result


def _targets_by_dimension(
    totals: Mapping[object, int],
    ratios: Mapping[str, float],
) -> dict[object, dict[str, int]]:
    return {
        key: _largest_remainder_counts(total, ratios)
        for key, total in totals.items()
    }


def _distribution_error(
    counts: Mapping[str, int],
    targets: Mapping[str, int],
    total: int,
) -> float:
    scale = max(total, 1)
    return sum(
        ((counts.get(split, 0) - target) / scale) ** 2
        for split, target in targets.items()
    )


def _candidate_score(
    group: _AllocationGroup,
    split: str,
    *,
    split_names: Sequence[str],
    global_counts: Mapping[str, int],
    global_targets: Mapping[str, int],
    total_images: int,
    source_counts: Mapping[str, Mapping[str, int]],
    source_totals: Mapping[str, int],
    source_targets: Mapping[str, Mapping[str, int]],
    signature_counts: Mapping[tuple[int, ...], Mapping[str, int]],
    signature_totals: Mapping[tuple[int, ...], int],
    signature_targets: Mapping[tuple[int, ...], Mapping[str, int]],
    source_signature_counts: Mapping[
        tuple[str, tuple[int, ...]], Mapping[str, int]
    ],
    source_signature_totals: Mapping[tuple[str, tuple[int, ...]], int],
    source_signature_targets: Mapping[
        tuple[str, tuple[int, ...]], Mapping[str, int]
    ],
) -> tuple[float, int]:
    next_global = dict(global_counts)
    next_global[split] += group.size
    score = 8.0 * _distribution_error(
        next_global,
        global_targets,
        total_images,
    )

    for source, amount in group.source_counts.items():
        next_counts = dict(source_counts[source])
        next_counts[split] += amount
        score += 20.0 * _distribution_error(
            next_counts,
            source_targets[source],
            source_totals[source],
        )
    for signature, amount in group.signature_counts.items():
        next_counts = dict(signature_counts[signature])
        next_counts[split] += amount
        score += 3.0 * _distribution_error(
            next_counts,
            signature_targets[signature],
            signature_totals[signature],
        )
    for source_signature, amount in group.source_signature_counts.items():
        next_counts = dict(source_signature_counts[source_signature])
        next_counts[split] += amount
        score += 4.0 * _distribution_error(
            next_counts,
            source_signature_targets[source_signature],
            source_signature_totals[source_signature],
        )

    overflow = max(0, next_global[split] - global_targets[split])
    return score, overflow * len(split_names) + split_names.index(split)


def _apply_group_counts(
    group: _AllocationGroup,
    split: str,
    *,
    global_counts: dict[str, int],
    source_counts: Mapping[str, dict[str, int]],
    signature_counts: Mapping[tuple[int, ...], dict[str, int]],
    source_signature_counts: Mapping[
        tuple[str, tuple[int, ...]], dict[str, int]
    ],
) -> None:
    global_counts[split] += group.size
    for source, amount in group.source_counts.items():
        source_counts[source][split] += amount
    for signature, amount in group.signature_counts.items():
        signature_counts[signature][split] += amount
    for source_signature, amount in group.source_signature_counts.items():
        source_signature_counts[source_signature][split] += amount


def _ensure_required_groups(
    group_assignments: dict[str, str],
    groups: Mapping[str, _AllocationGroup],
    ratios: Mapping[str, float],
    *,
    fixed_group_keys: frozenset[str] = frozenset(),
) -> None:
    """Keep train and valid non-empty without breaking an atomic group."""

    required = [
        split for split in ("train", "valid") if ratios.get(split, 0) > 0
    ]
    for required_split in required:
        if any(
            assigned == required_split and groups[key].images
            for key, assigned in group_assignments.items()
        ):
            continue
        candidates = []
        for key, assigned in group_assignments.items():
            if assigned == required_split or key in fixed_group_keys:
                continue
            donor_groups = [
                candidate_key
                for candidate_key, candidate_split in group_assignments.items()
                if candidate_split == assigned
            ]
            if len(donor_groups) <= 1:
                continue
            candidates.append((groups[key].size, key))
        if not candidates:
            raise ValueError(
                "group split cannot produce non-empty train and valid"
            )
        _size, selected_key = min(candidates)
        group_assignments[selected_key] = required_split


def _source_balance_score(
    *,
    global_counts: Mapping[str, int],
    global_targets: Mapping[str, int],
    total_images: int,
    source_counts: Mapping[str, int],
    source_targets: Mapping[str, int],
    source_total: int,
) -> float:
    return (
        8.0
        * _distribution_error(
            global_counts,
            global_targets,
            total_images,
        )
        + 20.0
        * _distribution_error(
            source_counts,
            source_targets,
            source_total,
        )
    )


def _refine_source_balance(
    group_assignments: dict[str, str],
    groups: Mapping[str, _AllocationGroup],
    *,
    fixed_group_keys: frozenset[str],
    global_counts: dict[str, int],
    global_targets: Mapping[str, int],
    total_images: int,
    source_counts: Mapping[str, dict[str, int]],
    source_totals: Mapping[str, int],
    source_targets: Mapping[str, Mapping[str, int]],
) -> None:
    """Swap comparable groups when that improves per-source ratio accuracy.

    The greedy pass handles arbitrary multi-source groups. This bounded local
    pass considers only single-source groups with the same class-signature set,
    so it can correct uneven video sizes without undoing stratification. One
    representative per split and size keeps still-image datasets linear.
    """

    buckets: dict[tuple[str, tuple[tuple[int, ...], ...]], list[str]] = (
        defaultdict(list)
    )
    for key, group in groups.items():
        if key in fixed_group_keys or len(group.source_counts) != 1:
            continue
        source = next(iter(group.source_counts))
        signature_profile = tuple(sorted(group.signature_counts))
        buckets[(source, signature_profile)].append(key)

    for (source, _signature_profile), keys in sorted(buckets.items()):
        for _iteration in range(64):
            representatives: dict[tuple[str, int], str] = {}
            for key in keys:
                marker = (group_assignments[key], groups[key].size)
                current = representatives.get(marker)
                if current is None or key < current:
                    representatives[marker] = key
            candidate_keys = sorted(representatives.values())
            current_score = _source_balance_score(
                global_counts=global_counts,
                global_targets=global_targets,
                total_images=total_images,
                source_counts=source_counts[source],
                source_targets=source_targets[source],
                source_total=source_totals[source],
            )
            best: tuple[float, str, str] | None = None
            for left_index, left_key in enumerate(candidate_keys):
                left_split = group_assignments[left_key]
                left_size = groups[left_key].size
                for right_key in candidate_keys[left_index + 1 :]:
                    right_split = group_assignments[right_key]
                    right_size = groups[right_key].size
                    if left_split == right_split or left_size == right_size:
                        continue
                    next_global = dict(global_counts)
                    next_source = dict(source_counts[source])
                    next_global[left_split] += right_size - left_size
                    next_global[right_split] += left_size - right_size
                    next_source[left_split] += right_size - left_size
                    next_source[right_split] += left_size - right_size
                    score = _source_balance_score(
                        global_counts=next_global,
                        global_targets=global_targets,
                        total_images=total_images,
                        source_counts=next_source,
                        source_targets=source_targets[source],
                        source_total=source_totals[source],
                    )
                    candidate = (score, left_key, right_key)
                    if score + 1e-12 < current_score and (
                        best is None or candidate < best
                    ):
                        best = candidate
            if best is None:
                break
            _score, left_key, right_key = best
            left_split = group_assignments[left_key]
            right_split = group_assignments[right_key]
            left_size = groups[left_key].size
            right_size = groups[right_key].size
            global_counts[left_split] += right_size - left_size
            global_counts[right_split] += left_size - right_size
            source_counts[source][left_split] += right_size - left_size
            source_counts[source][right_split] += left_size - right_size
            group_assignments[left_key] = right_split
            group_assignments[right_key] = left_split


def allocate_splits(
    images: Sequence[ImageSnapshot],
    ratios: Mapping[str, float],
    *,
    seed: int,
    test_only_images: Sequence[ImageSnapshot] = (),
) -> SplitResult:
    """Allocate source-balanced atomic groups without temporal leakage."""
    if test_only_images and "test" not in ratios:
        raise ValueError("test-only images require a test split")
    validate_split_size(len(images), ratios)
    if not math.isclose(sum(ratios.values()), 1.0, abs_tol=1e-9):
        raise ValueError("split ratios must sum to one")

    groups = _build_allocation_groups(images)
    split_names = tuple(ratios)
    eligible_splits = tuple(
        split for split in split_names if ratios[split] > 0
    )
    if not eligible_splits:
        raise ValueError("at least one split ratio must be positive")

    source_totals = Counter(
        image.allocation_source_key for image in images
    )
    signature_totals = Counter(image.class_ids for image in images)
    source_signature_totals = Counter(
        (image.allocation_source_key, image.class_ids) for image in images
    )
    global_targets = _largest_remainder_counts(len(images), ratios)
    source_targets = _targets_by_dimension(source_totals, ratios)
    signature_targets = _targets_by_dimension(signature_totals, ratios)
    source_signature_targets = _targets_by_dimension(
        source_signature_totals,
        ratios,
    )
    global_counts = {split: 0 for split in split_names}
    source_counts = {
        source: {split: 0 for split in split_names}
        for source in source_totals
    }
    signature_counts = {
        signature: {split: 0 for split in split_names}
        for signature in signature_totals
    }
    source_signature_counts = {
        source_signature: {split: 0 for split in split_names}
        for source_signature in source_signature_totals
    }

    forced_test_keys = {
        image.allocation_group_key for image in test_only_images
    }
    group_assignments: dict[str, str] = {}
    for key in sorted(forced_test_keys & groups.keys()):
        group = groups[key]
        group_assignments[key] = "test"
        _apply_group_counts(
            group,
            "test",
            global_counts=global_counts,
            source_counts=source_counts,
            signature_counts=signature_counts,
            source_signature_counts=source_signature_counts,
        )

    generator = random.Random(seed)
    ranked_groups = [
        (generator.random(), group)
        for key, group in sorted(groups.items())
        if key not in group_assignments
    ]
    ranked_groups.sort(key=lambda item: (-item[1].size, item[0], item[1].key))
    for _tie_breaker, group in ranked_groups:
        selected_split = min(
            eligible_splits,
            key=lambda split: _candidate_score(
                group,
                split,
                split_names=split_names,
                global_counts=global_counts,
                global_targets=global_targets,
                total_images=len(images),
                source_counts=source_counts,
                source_totals=source_totals,
                source_targets=source_targets,
                signature_counts=signature_counts,
                signature_totals=signature_totals,
                signature_targets=signature_targets,
                source_signature_counts=source_signature_counts,
                source_signature_totals=source_signature_totals,
                source_signature_targets=source_signature_targets,
            ),
        )
        group_assignments[group.key] = selected_split
        _apply_group_counts(
            group,
            selected_split,
            global_counts=global_counts,
            source_counts=source_counts,
            signature_counts=signature_counts,
            source_signature_counts=source_signature_counts,
        )

    _refine_source_balance(
        group_assignments,
        groups,
        fixed_group_keys=frozenset(forced_test_keys),
        global_counts=global_counts,
        global_targets=global_targets,
        total_images=len(images),
        source_counts=source_counts,
        source_totals=source_totals,
        source_targets=source_targets,
    )
    _ensure_required_groups(
        group_assignments,
        groups,
        ratios,
        fixed_group_keys=frozenset(forced_test_keys),
    )
    assignments: dict[str, list[ImageSnapshot]] = {
        split: [] for split in split_names
    }
    for key, split in group_assignments.items():
        assignments[split].extend(groups[key].images)
    if test_only_images:
        assignments["test"].extend(test_only_images)
    for rows in assignments.values():
        rows.sort(key=lambda image: image.id)

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
            training_source_key=image.training_source_key,
            training_group_key=image.training_group_key,
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
