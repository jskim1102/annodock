from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image as PillowImage

from app.services.derive import ImageDecodeError, prepare_image


pytestmark = pytest.mark.asyncio


def make_image(path: Path, image_format: str) -> None:
    PillowImage.new("RGB", (640, 320), (120, 80, 40)).save(
        path,
        format=image_format,
    )


async def test_web_safe_original_is_lossless_and_skips_display_derivative(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.jpg"
    make_image(source, "JPEG")
    original_bytes = source.read_bytes()
    batch = tmp_path / "batch"

    prepared = await prepare_image(
        source,
        batch,
        "images/train/source.jpg",
        link_original=True,
    )

    assert prepared.width == 640
    assert prepared.height == 320
    assert prepared.display_relative is None
    stored_original = batch / prepared.file_relative
    assert stored_original.read_bytes() == original_bytes
    assert stored_original.samefile(source)
    with PillowImage.open(batch / prepared.thumb_relative) as thumbnail:
        assert max(thumbnail.size) == 256
        assert thumbnail.format == "JPEG"


async def test_non_web_safe_format_gets_browser_jpeg_derivative(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.tiff"
    make_image(source, "TIFF")
    batch = tmp_path / "batch"

    prepared = await prepare_image(
        source,
        batch,
        "images/val/source.tiff",
    )

    assert prepared.display_relative is not None
    with PillowImage.open(batch / prepared.display_relative) as display:
        assert display.format == "JPEG"
        assert display.size == (640, 320)


async def test_decode_failure_removes_partial_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "broken.tif"
    source.write_bytes(b"not-an-image")
    batch = tmp_path / "batch"

    with pytest.raises(ImageDecodeError):
        await prepare_image(source, batch, "images/broken.tif")

    assert not any(path.is_file() for path in batch.rglob("*"))


async def test_exif_orientation_sets_display_dimensions_without_touching_original(
    tmp_path: Path,
) -> None:
    source = tmp_path / "portrait.jpg"
    exif = PillowImage.Exif()
    exif[274] = 6
    PillowImage.new("RGB", (40, 20), (10, 20, 30)).save(
        source,
        "JPEG",
        exif=exif,
    )
    original_bytes = source.read_bytes()
    batch = tmp_path / "batch"

    prepared = await prepare_image(
        source,
        batch,
        "images/portrait.jpg",
    )

    assert (prepared.width, prepared.height) == (20, 40)
    assert (batch / prepared.file_relative).read_bytes() == original_bytes
    with PillowImage.open(batch / prepared.thumb_relative) as thumbnail:
        assert thumbnail.size == (20, 40)
