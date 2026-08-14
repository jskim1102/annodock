"""Common normalized annotation representation used by import adapters."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from app.services.collect import CollectedFile
from app.services.derive import DERIVE_EXECUTOR, _decoded_rgb


@dataclass(frozen=True)
class IRBox:
    class_id: int
    cx: float
    cy: float
    w: float
    h: float

    def __post_init__(self) -> None:
        if self.class_id < 0:
            raise ValueError("class_id must be non-negative")
        coordinates = (self.cx, self.cy, self.w, self.h)
        if not all(
            math.isfinite(value) and 0.0 <= value <= 1.0
            for value in coordinates
        ):
            raise ValueError("box coordinates must be normalized to 0..1")


@dataclass(frozen=True)
class IRImage:
    rel_path: str
    width: int
    height: int
    boxes: tuple[IRBox, ...]

    def __post_init__(self) -> None:
        if not self.rel_path:
            raise ValueError("rel_path must not be empty")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("image dimensions must be positive")


@dataclass(frozen=True)
class IRClass:
    class_id: int
    name: str


@dataclass(frozen=True)
class SourceClass:
    source_id: int | None
    name: str


@dataclass(frozen=True)
class ClassMapping:
    source_id: int | None
    source_name: str
    class_id: int


@dataclass(frozen=True)
class NormalizedClasses:
    classes: tuple[IRClass, ...]
    mappings: tuple[ClassMapping, ...]


@dataclass(frozen=True)
class AnnotationIR:
    images: tuple[IRImage, ...]
    classes: tuple[IRClass, ...]
    class_mappings: tuple[ClassMapping, ...]


def normalize_source_classes(
    source_classes: list[SourceClass],
) -> NormalizedClasses:
    if not source_classes:
        return NormalizedClasses(classes=(), mappings=())
    if any(not item.name.strip() for item in source_classes):
        raise ValueError("class names must not be empty")

    has_ids = [item.source_id is not None for item in source_classes]
    if any(has_ids) and not all(has_ids):
        raise ValueError("source classes cannot mix ids with name-only entries")

    if all(has_ids):
        by_id: dict[int, str] = {}
        for item in source_classes:
            assert item.source_id is not None
            if item.source_id < 0:
                raise ValueError("source class ids must be non-negative")
            previous = by_id.setdefault(item.source_id, item.name.strip())
            if previous != item.name.strip():
                raise ValueError(
                    f"source class id {item.source_id} has conflicting names"
                )
        ordered = [
            SourceClass(source_id=source_id, name=name)
            for source_id, name in sorted(by_id.items())
        ]
    else:
        ordered = [
            SourceClass(source_id=None, name=name)
            for name in sorted({item.name.strip() for item in source_classes})
        ]

    classes = tuple(
        IRClass(class_id=class_id, name=item.name)
        for class_id, item in enumerate(ordered)
    )
    mappings = tuple(
        ClassMapping(
            source_id=item.source_id,
            source_name=item.name,
            class_id=class_id,
        )
        for class_id, item in enumerate(ordered)
    )
    return NormalizedClasses(classes=classes, mappings=mappings)


def safe_image_reference(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or path.is_absolute()
        or ".." in path.parts
        or "\x00" in normalized
    ):
        return None
    return path.as_posix()


def match_image_reference(
    reference: str,
    images: list[CollectedFile],
) -> CollectedFile | None:
    exact = [
        item
        for item in images
        if item.rel_path == reference
        or item.rel_path.endswith(f"/{reference}")
    ]
    if len(exact) == 1:
        return exact[0]
    basename = PurePosixPath(reference).name
    by_name = [
        item
        for item in images
        if PurePosixPath(item.rel_path).name == basename
    ]
    return by_name[0] if len(by_name) == 1 else None


def _decoded_dimensions(item: CollectedFile) -> tuple[int, int]:
    extension = Path(item.rel_path).suffix.removeprefix(".").lower()
    decoded = _decoded_rgb(item.abs_path, extension)
    try:
        return decoded.size
    finally:
        decoded.close()


async def load_decoded_dimensions(
    images: list[CollectedFile],
) -> tuple[dict[str, tuple[int, int]], dict[str, str]]:
    loop = asyncio.get_running_loop()
    dimensions: dict[str, tuple[int, int]] = {}
    failures: dict[str, str] = {}

    async def load(item: CollectedFile) -> None:
        try:
            dimensions[item.rel_path] = await loop.run_in_executor(
                DERIVE_EXECUTOR,
                _decoded_dimensions,
                item,
            )
        except Exception as error:
            failures[item.rel_path] = str(error)

    await asyncio.gather(*(load(item) for item in images))
    return dimensions, failures
