"""Deterministic image-count partitioning for uploaded datasets."""

from __future__ import annotations

from math import ceil


MAX_DATASET_NAME_LENGTH = 255


def balanced_image_partition_sizes(
    image_count: int,
    max_images: int,
) -> tuple[int, ...]:
    """Return near-equal part sizes which never exceed ``max_images``."""
    if image_count < 0:
        raise ValueError("image_count must be nonnegative")
    if max_images <= 0:
        raise ValueError("max_images must be positive")
    if image_count == 0:
        return ()

    part_count = ceil(image_count / max_images)
    base_size, larger_part_count = divmod(image_count, part_count)
    return tuple(
        base_size + (index < larger_part_count)
        for index in range(part_count)
    )


def dataset_partition_name(base_name: str, part_index: int) -> str:
    """Append the public ``_(n)`` suffix without exceeding 255 characters."""
    if part_index < 1:
        raise ValueError("part_index must be positive")
    suffix = f"_({part_index})"
    normalized = base_name.strip() or "dataset"
    return f"{normalized[: MAX_DATASET_NAME_LENGTH - len(suffix)]}{suffix}"
