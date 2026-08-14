"""Trusted Ultralytics preset model names exposed by the trainer."""

from __future__ import annotations


PRESET_MODELS: tuple[str, ...] = (
    "yolo26n.pt",
    "yolo26s.pt",
    "yolo26m.pt",
    "yolo26l.pt",
    "yolo26x.pt",
)


def is_preset(name: str) -> bool:
    """Return whether *name* is one of the trusted official presets."""
    return name in PRESET_MODELS


def list_all_models() -> list[dict[str, str | float | None]]:
    """Return the preset-only model list used by API and UI callers."""
    return [
        {"name": name, "type": "preset", "size_mb": None}
        for name in PRESET_MODELS
    ]
