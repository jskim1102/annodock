"""ZIP extraction that validates every member before writing any output."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class ZipLimits:
    max_extracted_bytes: int
    max_file_count: int
    max_compression_ratio: float


@dataclass(frozen=True)
class ZipIssue:
    kind: str
    path: str
    detail: str


class ZipSafetyError(ValueError):
    def __init__(self, path: str, detail: str) -> None:
        super().__init__(detail)
        self.issue = ZipIssue(
            kind="rejected_file",
            path=path,
            detail=detail,
        )


def _relative_member(info: zipfile.ZipInfo) -> PurePosixPath:
    normalized = info.filename.replace("\\", "/")
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
        raise ZipSafetyError(info.filename, "unsafe archive path")
    return path


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return stat.S_IFMT(mode) == stat.S_IFLNK


def _validated_members(
    zipped: zipfile.ZipFile,
    limits: ZipLimits,
    issues: list[ZipIssue],
) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    validated: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    total_size = 0
    total_compressed = 0
    file_count = 0

    for info in zipped.infolist():
        if not info.is_dir():
            file_count += 1
            total_size += info.file_size
            total_compressed += max(info.compress_size, 1)
            member_ratio = info.file_size / max(info.compress_size, 1)
            total_ratio = total_size / max(total_compressed, 1)
            if file_count > limits.max_file_count:
                raise ZipSafetyError(
                    info.filename,
                    "archive file-count limit exceeded",
                )
            if total_size > limits.max_extracted_bytes:
                raise ZipSafetyError(
                    info.filename,
                    "archive extracted-size limit exceeded",
                )
            if (
                member_ratio > limits.max_compression_ratio
                or total_ratio > limits.max_compression_ratio
            ):
                raise ZipSafetyError(
                    info.filename,
                    "archive compression ratio exceeded",
                )

        try:
            relative = _relative_member(info)
        except ZipSafetyError as error:
            issues.append(error.issue)
            continue
        if _is_symlink(info):
            issues.append(
                ZipIssue(
                    kind="rejected_file",
                    path=info.filename,
                    detail="symbolic links are not allowed",
                )
            )
            continue
        if info.is_dir():
            continue
        if info.flag_bits & 0x1:
            raise ZipSafetyError(info.filename, "encrypted files are not allowed")
        validated.append((info, relative))
    return validated


def safe_extract_zip(
    archive: Path,
    destination: Path,
    limits: ZipLimits,
    *,
    issues: list[ZipIssue] | None = None,
) -> list[Path]:
    """Extract into a sibling staging directory, then publish atomically."""

    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ZipSafetyError(str(destination), "extraction destination exists")

    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.extract-",
            dir=destination.parent,
        )
    )
    written_relative: list[PurePosixPath] = []
    extracted_size = 0
    rejected = issues if issues is not None else []
    current_path = str(archive)
    try:
        with zipfile.ZipFile(archive) as zipped:
            members = _validated_members(zipped, limits, rejected)
            for info, relative in members:
                current_path = info.filename
                target = staging.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(info) as source, target.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        extracted_size += len(chunk)
                        if extracted_size > limits.max_extracted_bytes:
                            raise ZipSafetyError(
                                info.filename,
                                "archive extracted-size limit exceeded",
                            )
                        output.write(chunk)
                written_relative.append(relative)
        os.replace(staging, destination)
    except Exception as error:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(error, ZipSafetyError):
            raise
        raise ZipSafetyError(
            current_path,
            f"invalid zip archive: {error}",
        ) from error

    return [destination.joinpath(*relative.parts) for relative in written_relative]
