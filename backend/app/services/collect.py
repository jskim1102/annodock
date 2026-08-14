"""Normalize file, folder, and ZIP inputs into one ingestion shape."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from app.services.classify import classify_document
from app.services.zipsafe import ZipIssue, ZipLimits, safe_extract_zip


CollectedKind = Literal["image", "label", "classfile", "zip", "other"]
SPLIT_ALIASES = {
    "train": "train",
    "val": "val",
    "valid": "val",
    "validation": "val",
    "test": "test",
}


@dataclass(frozen=True)
class SourceFile:
    rel_path: str
    abs_path: Path


@dataclass(frozen=True)
class CollectedFile:
    rel_path: str
    abs_path: Path
    kind: CollectedKind
    split: str | None


def _normalized_relative(value: str) -> PurePosixPath:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    first = path.parts[0] if path.parts else ""
    if (
        not normalized
        or normalized.startswith("/")
        or path.is_absolute()
        or first.endswith(":")
        or ".." in path.parts
        or "\x00" in normalized
    ):
        raise ValueError(f"unsafe relative path: {value}")
    return path


def _kind(
    path: PurePosixPath,
    absolute: Path,
    allowed: frozenset[str],
    image_stems: frozenset[str],
) -> CollectedKind:
    extension = path.suffix.removeprefix(".").lower()
    if extension in allowed:
        return "image"
    if extension == "zip":
        return "zip"
    return classify_document(
        absolute,
        path.as_posix(),
        matches_image_stem=path.stem in image_stems,
    )


def _split(path: PurePosixPath) -> str | None:
    for part in path.parts:
        split = SPLIT_ALIASES.get(part.lower())
        if split is not None:
            return split
    return None


def collect_sources(
    sources: list[SourceFile],
    allowed_extensions: tuple[str, ...],
) -> list[CollectedFile]:
    allowed = frozenset(item.removeprefix(".").lower() for item in allowed_extensions)
    prepared: list[tuple[PurePosixPath, Path]] = []
    for source in sources:
        relative = _normalized_relative(source.rel_path)
        absolute = source.abs_path.resolve()
        if source.abs_path.is_symlink():
            raise ValueError(f"symbolic source is not allowed: {source.rel_path}")
        if not absolute.is_file():
            raise ValueError(f"source is not a file: {source.rel_path}")
        prepared.append((relative, absolute))

    image_stems = frozenset(
        relative.stem
        for relative, _ in prepared
        if relative.suffix.removeprefix(".").lower() in allowed
    )
    collected: list[CollectedFile] = []
    for relative, absolute in prepared:
        collected.append(
            CollectedFile(
                rel_path=relative.as_posix(),
                abs_path=absolute,
                kind=_kind(relative, absolute, allowed, image_stems),
                split=_split(relative),
            )
        )
    return sorted(collected, key=lambda item: item.rel_path)


def collect_directory(
    root: Path,
    allowed_extensions: tuple[str, ...],
) -> list[CollectedFile]:
    if root.is_symlink():
        raise ValueError(f"symbolic directory is not allowed: {root}")
    root = root.resolve()
    entries = list(root.rglob("*"))
    symbolic = next((path for path in entries if path.is_symlink()), None)
    if symbolic is not None:
        raise ValueError(
            "symbolic source is not allowed: "
            f"{symbolic.relative_to(root).as_posix()}"
        )
    sources = [
        SourceFile(rel_path=path.relative_to(root).as_posix(), abs_path=path)
        for path in entries
        if path.is_file()
    ]
    return collect_sources(sources, allowed_extensions)


def collect_zip(
    archive: Path,
    destination: Path,
    allowed_extensions: tuple[str, ...],
    limits: ZipLimits,
    *,
    issues: list[ZipIssue] | None = None,
) -> list[CollectedFile]:
    safe_extract_zip(archive, destination, limits, issues=issues)
    return collect_directory(destination, allowed_extensions)
