from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import pytest

from app.services.zipsafe import ZipLimits, ZipSafetyError, safe_extract_zip


def limits(**overrides: int | float) -> ZipLimits:
    values: dict[str, int | float] = {
        "max_extracted_bytes": 10_000,
        "max_file_count": 100,
        "max_compression_ratio": 100,
    }
    values.update(overrides)
    return ZipLimits(**values)


def test_normal_zip_preserves_relative_paths(tmp_path: Path) -> None:
    archive = tmp_path / "normal.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("images/train/a.jpg", b"image")
        zipped.writestr("labels/train/a.txt", b"0 0.5 0.5 0.2 0.2")

    destination = tmp_path / "extracted"
    files = safe_extract_zip(archive, destination, limits())

    assert [path.relative_to(destination).as_posix() for path in files] == [
        "images/train/a.jpg",
        "labels/train/a.txt",
    ]
    assert (destination / "images/train/a.jpg").read_bytes() == b"image"


@pytest.mark.parametrize(
    "member",
    [
        "../escape.txt",
        "../escape/",
        "/absolute.txt",
        "C:/windows-absolute.txt",
        "safe/../../escape.txt",
    ],
)
def test_path_traversal_and_absolute_members_are_rejected_without_residue(
    tmp_path: Path,
    member: str,
) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("safe/first.txt", b"first")
        zipped.writestr(member, b"escape")
    destination = tmp_path / "extracted"
    issues = []

    files = safe_extract_zip(archive, destination, limits(), issues=issues)

    assert [path.relative_to(destination).as_posix() for path in files] == [
        "safe/first.txt"
    ]
    assert [(issue.kind, issue.path) for issue in issues] == [
        ("rejected_file", member)
    ]
    assert not (tmp_path / "escape.txt").exists()


def test_symbolic_link_member_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "symlink.zip"
    link = zipfile.ZipInfo("images/train/link.jpg")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("images/train/valid.jpg", b"image")
        zipped.writestr(link, "../../outside")
    destination = tmp_path / "extracted"
    issues = []

    files = safe_extract_zip(archive, destination, limits(), issues=issues)

    assert [path.relative_to(destination).as_posix() for path in files] == [
        "images/train/valid.jpg"
    ]
    assert [(issue.path, issue.detail) for issue in issues] == [
        ("images/train/link.jpg", "symbolic links are not allowed")
    ]

@pytest.mark.parametrize(
    ("limit_overrides", "entries"),
    [
        ({"max_extracted_bytes": 5}, [("a.txt", b"123456")]),
        ({"max_file_count": 1}, [("a.txt", b"a"), ("b.txt", b"b")]),
        (
            {"max_compression_ratio": 2},
            [("bomb.txt", b"a" * 10_000)],
        ),
    ],
)
def test_limits_abort_without_partial_output(
    tmp_path: Path,
    limit_overrides: dict[str, int | float],
    entries: list[tuple[str, bytes]],
) -> None:
    archive = tmp_path / "limited.zip"
    with zipfile.ZipFile(
        archive,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as zipped:
        for name, content in entries:
            zipped.writestr(name, content)
    destination = tmp_path / "extracted"

    with pytest.raises(ZipSafetyError):
        safe_extract_zip(archive, destination, limits(**limit_overrides))

    assert not destination.exists()


def test_unsupported_compression_cleans_staging_and_reports_member_path(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "unsupported.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("unsupported.bin", b"content")
    content = bytearray(archive.read_bytes())
    local_header = content.index(b"PK\x03\x04")
    central_header = content.index(b"PK\x01\x02")
    content[local_header + 8 : local_header + 10] = (99).to_bytes(2, "little")
    content[central_header + 10 : central_header + 12] = (99).to_bytes(
        2,
        "little",
    )
    archive.write_bytes(content)
    destination = tmp_path / "extracted"

    with pytest.raises(ZipSafetyError) as caught:
        safe_extract_zip(archive, destination, limits())

    assert caught.value.issue.path == "unsupported.bin"
    assert not destination.exists()
    assert list(tmp_path.glob(".extracted.extract-*")) == []
