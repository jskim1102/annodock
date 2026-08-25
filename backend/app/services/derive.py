"""Original copy, browser derivative, and thumbnail generation."""

from __future__ import annotations

import asyncio
import os
import shutil
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pillow_avif  # noqa: F401 - registers the Pillow AVIF plugin
import pillow_heif
import rawpy
from PIL import Image as PillowImage
from PIL import ImageOps


pillow_heif.register_heif_opener()

WEB_SAFE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp"})
DERIVE_EXECUTOR = ThreadPoolExecutor(
    max_workers=min(4, os.cpu_count() or 1),
    thread_name_prefix="dataset-image",
)


class ImageDecodeError(ValueError):
    pass


@dataclass(frozen=True)
class PreparedImage:
    width: int
    height: int
    file_relative: Path
    display_relative: Path | None
    thumb_relative: Path
    original_bytes: int
    display_bytes: int
    thumb_bytes: int


def _safe_relative(rel_path: str) -> PurePosixPath:
    normalized = rel_path.replace("\\", "/")
    relative = PurePosixPath(normalized)
    if (
        not normalized
        or relative.is_absolute()
        or normalized.startswith("/")
        or ".." in relative.parts
    ):
        raise ImageDecodeError("unsafe image relative path")
    return relative


def _decoded_rgb(source: Path, extension: str) -> PillowImage.Image:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter(
                "error",
                PillowImage.DecompressionBombWarning,
            )
            if extension == "dng":
                with rawpy.imread(str(source)) as raw:
                    return PillowImage.fromarray(raw.postprocess()).convert(
                        "RGB"
                    )
            with PillowImage.open(source) as opened:
                corrected = ImageOps.exif_transpose(opened)
                try:
                    corrected.load()
                    return corrected.convert("RGB")
                finally:
                    if corrected is not opened:
                        corrected.close()
    except Exception as error:
        raise ImageDecodeError(f"cannot decode image: {error}") from error


def _link_or_copy(source: Path, target: Path) -> None:
    """Reuse immutable upload bytes, with a cross-filesystem fallback."""

    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _prepare_image_sync(
    source: Path,
    batch_root: Path,
    rel_path: str,
    link_original: bool,
) -> PreparedImage:
    relative = _safe_relative(rel_path)
    extension = relative.suffix.removeprefix(".").lower()
    file_relative = Path("original").joinpath(*relative.parts)
    display_relative = (
        None
        if extension in WEB_SAFE_EXTENSIONS
        else Path("display").joinpath(*relative.with_suffix(".jpg").parts)
    )
    thumb_relative = Path("thumbs").joinpath(
        *relative.with_suffix(".jpg").parts
    )
    original_target = batch_root / file_relative
    display_target = (
        batch_root / display_relative if display_relative is not None else None
    )
    thumb_target = batch_root / thumb_relative
    created: list[Path] = []
    decoded: PillowImage.Image | None = None
    thumbnail: PillowImage.Image | None = None

    try:
        original_target.parent.mkdir(parents=True, exist_ok=True)
        if link_original:
            _link_or_copy(source, original_target)
        else:
            shutil.copy2(source, original_target)
        created.append(original_target)

        decoded = _decoded_rgb(source, extension)
        width, height = decoded.size
        if display_target is not None:
            display_target.parent.mkdir(parents=True, exist_ok=True)
            decoded.save(display_target, "JPEG", quality=92, optimize=True)
            created.append(display_target)

        thumbnail = decoded.copy()
        thumbnail.thumbnail((256, 256), PillowImage.Resampling.LANCZOS)
        thumb_target.parent.mkdir(parents=True, exist_ok=True)
        thumbnail.save(thumb_target, "JPEG", quality=85, optimize=True)
        created.append(thumb_target)
        return PreparedImage(
            width=width,
            height=height,
            file_relative=file_relative,
            display_relative=display_relative,
            thumb_relative=thumb_relative,
            original_bytes=original_target.stat().st_size,
            display_bytes=(
                display_target.stat().st_size
                if display_target is not None
                else 0
            ),
            thumb_bytes=thumb_target.stat().st_size,
        )
    except Exception:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise
    finally:
        if decoded is not None:
            decoded.close()
        if thumbnail is not None:
            thumbnail.close()


async def prepare_image(
    source: Path,
    batch_root: Path,
    rel_path: str,
    *,
    link_original: bool = False,
) -> PreparedImage:
    """Run CPU-bound decoding outside the event loop."""

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        DERIVE_EXECUTOR,
        _prepare_image_sync,
        source,
        batch_root,
        rel_path,
        link_original,
    )
