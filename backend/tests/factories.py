from __future__ import annotations

from typing import Any

from app.models import Image, MediaObject


def image_with_media(*, owner_id: int, **image_values: Any) -> Image:
    """Build an image row with the physical-media invariant used in production."""

    if "media_object" not in image_values and "media_object_id" not in image_values:
        dataset_id = int(image_values["dataset_id"])
        image_values["media_object"] = MediaObject(
            owner_id=owner_id,
            created_by_dataset_id=dataset_id,
            original_bytes=int(image_values.get("original_bytes", 0) or 0),
            display_bytes=int(image_values.get("display_bytes", 0) or 0),
            thumb_bytes=int(image_values.get("thumb_bytes", 0) or 0),
        )
    return Image(**image_values)
