"""Render one stored run image with a completed run's best checkpoint."""

from __future__ import annotations

import io
import threading
import warnings
from functools import lru_cache
from pathlib import Path

import pillow_avif  # noqa: F401 - registers the Pillow AVIF plugin
import pillow_heif
from PIL import Image as PillowImage
from PIL import ImageOps
from ultralytics import YOLO


pillow_heif.register_heif_opener()


class InferenceImageError(ValueError):
    """The selected stored image is not safely decodable."""


_MODEL_LOCK = threading.Lock()


def _decode_image(image_path: Path) -> PillowImage.Image:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PillowImage.DecompressionBombWarning)
            with PillowImage.open(image_path) as opened:
                corrected = ImageOps.exif_transpose(opened)
                try:
                    corrected.load()
                    return corrected.convert("RGB")
                finally:
                    if corrected is not opened:
                        corrected.close()
    except Exception as error:
        raise InferenceImageError("cannot decode inference image") from error


@lru_cache(maxsize=1)
def _load_model(
    model_path: str,
    modified_ns: int,
    file_size: int,
) -> YOLO:
    # mtime and size invalidate the single-entry cache if the artifact at this
    # exact path is ever replaced.
    del modified_ns, file_size
    return YOLO(model_path)


def clear_model_cache() -> None:
    """Drop the cached checkpoint, primarily for deterministic test teardown."""

    with _MODEL_LOCK:
        _load_model.cache_clear()


def render_prediction_file(
    model_path: Path,
    image_path: Path,
    imgsz: int,
) -> bytes:
    """Run detection and return a PNG with labels and bounding boxes drawn."""

    image = _decode_image(image_path)
    try:
        resolved_model = model_path.resolve(strict=True)
        model_stat = resolved_model.stat()
        with _MODEL_LOCK:
            model = _load_model(
                str(resolved_model),
                model_stat.st_mtime_ns,
                model_stat.st_size,
            )
            results = model.predict(
                source=image,
                imgsz=imgsz,
                device="cpu",
                verbose=False,
            )
            if len(results) != 1:
                raise RuntimeError("inference did not return exactly one result")
            rendered = results[0].plot(pil=True)
    finally:
        image.close()

    if not isinstance(rendered, PillowImage.Image):
        raise RuntimeError("inference renderer did not return a PIL image")
    try:
        output = io.BytesIO()
        rendered.save(output, "PNG", optimize=True)
        return output.getvalue()
    finally:
        rendered.close()
