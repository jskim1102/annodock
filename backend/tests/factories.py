from __future__ import annotations

from typing import Any

from app.models import Image, MediaObject
from app.services.split import training_group_key, training_source_key


def image_with_media(*, workspace_id: int, **image_values: Any) -> Image:
    """Build an image row with the physical-media invariant used in production."""

    dataset_id = int(image_values["dataset_id"])
    source_key = image_values.setdefault(
        "training_source_key",
        training_source_key(dataset_id, None),
    )
    image_values.setdefault(
        "training_group_key",
        training_group_key(
            source_key,
            filename=str(image_values["filename"]),
            rel_path=str(image_values["rel_path"]),
        ),
    )
    if "media_object" not in image_values and "media_object_id" not in image_values:
        image_values["media_object"] = MediaObject(
            workspace_id=workspace_id,
            created_by_dataset_id=dataset_id,
            original_bytes=int(image_values.get("original_bytes", 0) or 0),
            display_bytes=int(image_values.get("display_bytes", 0) or 0),
            thumb_bytes=int(image_values.get("thumb_bytes", 0) or 0),
        )
    return Image(**image_values)
