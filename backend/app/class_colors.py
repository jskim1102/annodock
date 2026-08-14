"""Deterministic display colors for project-owned class catalogs."""

CLASS_COLOR_PRESETS = (
    "#EF4444",
    "#F59E0B",
    "#22C55E",
    "#3B82F6",
    "#8B5CF6",
    "#EC4899",
    "#06B6D4",
    "#84CC16",
)


def class_color(class_id: int) -> str:
    return CLASS_COLOR_PRESETS[class_id % len(CLASS_COLOR_PRESETS)]
