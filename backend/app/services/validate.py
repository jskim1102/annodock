"""Upload limits and image signature validation."""

from __future__ import annotations

import math
import shutil
from pathlib import Path

from app.config import Settings
from app.services.storage import storage_root


class RejectedFile(ValueError):
    """An input file failed the extension or magic-byte contract."""


class UploadLimitExceeded(ValueError):
    """An upload exceeds a configured byte or file-count ceiling."""


class InsufficientStorage(ValueError):
    def __init__(self, required_bytes: int, available_bytes: int) -> None:
        super().__init__("insufficient storage")
        self.required_bytes = required_bytes
        self.available_bytes = available_bytes


def validate_upload_capacity(
    settings: Settings,
    *,
    size: int,
    file_count: int,
    expected_extracted_size: int,
) -> None:
    if size > settings.max_zip_bytes:
        raise UploadLimitExceeded("upload exceeds MAX_ZIP_BYTES")
    if file_count > settings.max_file_count:
        raise UploadLimitExceeded("upload exceeds MAX_FILE_COUNT")
    if expected_extracted_size > settings.max_extracted_bytes:
        raise UploadLimitExceeded("upload exceeds MAX_EXTRACTED_BYTES")

    root = storage_root(settings.storage_dir)
    available = shutil.disk_usage(root).free
    required = math.ceil(
        expected_extracted_size * settings.disk_headroom_factor
    )
    if available < required:
        raise InsufficientStorage(required, available)


def _matches_signature(extension: str, header: bytes) -> bool:
    if extension in {"jpg", "jpeg", "mpo"}:
        return header.startswith(b"\xff\xd8\xff")
    if extension == "png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if extension == "webp":
        return (
            len(header) >= 12
            and header.startswith(b"RIFF")
            and header[8:12] == b"WEBP"
        )
    if extension == "bmp":
        return header.startswith(b"BM")
    if extension in {"tif", "tiff", "dng"}:
        return header.startswith((b"II*\x00", b"MM\x00*"))
    if extension in {"jp2", "jpeg2000"}:
        return header.startswith(
            (b"\x00\x00\x00\x0cjP  \r\n\x87\n", b"\xffO\xffQ")
        )
    if extension in {"avif", "heic", "heif"}:
        if len(header) < 12 or header[4:8] != b"ftyp":
            return False
        brand = header[8:12]
        if extension == "avif":
            return brand in {b"avif", b"avis"}
        return brand in {
            b"heic",
            b"heix",
            b"hevc",
            b"hevx",
            b"heim",
            b"heis",
            b"mif1",
            b"msf1",
        }
    return False


def validate_image_file(
    content_path: Path,
    logical_path: str | Path,
    allowed_extensions: tuple[str, ...],
) -> str:
    extension = Path(logical_path).suffix.removeprefix(".").lower()
    allowed = frozenset(item.lower() for item in allowed_extensions)
    if extension not in allowed:
        raise RejectedFile(f"extension is not allowed: {extension or '(none)'}")

    with content_path.open("rb") as source:
        header = source.read(32)
    if not _matches_signature(extension, header):
        raise RejectedFile(f"signature does not match .{extension}")
    return extension
