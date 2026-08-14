from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.services.collect import (
    SourceFile,
    collect_directory,
    collect_sources,
    collect_zip,
)
from app.services.zipsafe import ZipLimits


ALLOWED = (
    "avif",
    "bmp",
    "dng",
    "heic",
    "heif",
    "jp2",
    "jpeg",
    "jpeg2000",
    "jpg",
    "mpo",
    "png",
    "tif",
    "tiff",
    "webp",
)


def test_explicit_files_normalize_kinds_and_extract_split(
    tmp_path: Path,
) -> None:
    paths = {
        "images/train/frame.jpg": b"image",
        "labels/train/frame.txt": b"0 0.5 0.5 0.2 0.2\n",
        "data.yaml": b"names: [car]",
        "misc/readme.md": b"ignored",
    }
    sources: list[SourceFile] = []
    for relative, content in paths.items():
        absolute = tmp_path / relative
        absolute.parent.mkdir(parents=True, exist_ok=True)
        absolute.write_bytes(content)
        sources.append(SourceFile(rel_path=relative, abs_path=absolute))

    collected = collect_sources(sources, ALLOWED)

    assert [
        (item.rel_path, item.kind, item.split) for item in collected
    ] == [
        ("data.yaml", "classfile", None),
        ("images/train/frame.jpg", "image", "train"),
        ("labels/train/frame.txt", "label", "train"),
        ("misc/readme.md", "other", None),
    ]


def test_folder_and_zip_use_the_same_normalized_shape(tmp_path: Path) -> None:
    folder = tmp_path / "folder"
    (folder / "mixed/val").mkdir(parents=True)
    (folder / "mixed/val/a.png").write_bytes(b"png")
    (folder / "mixed/val/a.txt").write_text("", encoding="utf-8")
    folder_items = collect_directory(folder, ALLOWED)

    archive = tmp_path / "input.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("mixed/val/a.png", b"png")
        zipped.writestr("mixed/val/a.txt", b"")
    zip_items = collect_zip(
        archive,
        tmp_path / "zip-output",
        ALLOWED,
        ZipLimits(
            max_extracted_bytes=10_000,
            max_file_count=10,
            max_compression_ratio=100,
        ),
    )

    assert [
        (item.rel_path, item.kind, item.split) for item in folder_items
    ] == [
        ("mixed/val/a.png", "image", "val"),
        ("mixed/val/a.txt", "label", "val"),
    ]
    assert [
        (item.rel_path, item.kind, item.split) for item in zip_items
    ] == [
        ("mixed/val/a.png", "image", "val"),
        ("mixed/val/a.txt", "label", "val"),
    ]


def test_split_is_null_when_no_known_segment_exists(tmp_path: Path) -> None:
    source = tmp_path / "sample.jpg"
    source.write_bytes(b"image")
    [item] = collect_sources(
        [SourceFile(rel_path="sample.jpg", abs_path=source)],
        ALLOWED,
    )
    assert item.split is None


@pytest.mark.parametrize(
    ("segment", "expected"),
    [
        ("train", "train"),
        ("TRAIN", "train"),
        ("val", "val"),
        ("Valid", "val"),
        ("VALIDATION", "val"),
        ("test", "test"),
    ],
)
def test_split_aliases_are_exact_case_insensitive_path_segments(
    tmp_path: Path,
    segment: str,
    expected: str,
) -> None:
    source = tmp_path / f"{segment}.jpg"
    source.write_bytes(b"image")

    [item] = collect_sources(
        [
            SourceFile(
                rel_path=f"dataset/{segment}/sample.jpg",
                abs_path=source,
            )
        ],
        ALLOWED,
    )

    assert item.split == expected


@pytest.mark.parametrize("segment", ["training", "validate", "contest"])
def test_split_does_not_match_partial_or_unapproved_segments(
    tmp_path: Path,
    segment: str,
) -> None:
    source = tmp_path / f"{segment}.jpg"
    source.write_bytes(b"image")

    [item] = collect_sources(
        [
            SourceFile(
                rel_path=f"dataset/{segment}/sample.jpg",
                abs_path=source,
            )
        ],
        ALLOWED,
    )

    assert item.split is None


def test_null_byte_relative_path_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "sample.jpg"
    source.write_bytes(b"image")

    with pytest.raises(ValueError, match="unsafe relative path"):
        collect_sources(
            [SourceFile(rel_path="sample\x00.jpg", abs_path=source)],
            ALLOWED,
        )


def test_directory_collection_rejects_nested_symbolic_links(
    tmp_path: Path,
) -> None:
    root = tmp_path / "folder"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "image.jpg").write_bytes(b"image")
    (root / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic source"):
        collect_directory(root, ALLOWED)
