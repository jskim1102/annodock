"""Content-based roles for label and class metadata files."""

from __future__ import annotations

import math
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml


DocumentRole = Literal["label", "classfile", "other"]
LABEL_SAMPLE_LINES = 20
CLASS_FILE_NAMES = frozenset({"classes.txt"})
CLASS_FILE_SUFFIXES = frozenset({".yaml", ".yml", ".names"})


def is_class_file_candidate(logical_path: str | PurePosixPath) -> bool:
    path = PurePosixPath(str(logical_path).replace("\\", "/"))
    return (
        path.name.lower() in CLASS_FILE_NAMES
        or path.suffix.lower() in CLASS_FILE_SUFFIXES
    )


def looks_like_yolo_label(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            lines = list(islice(source, LABEL_SAMPLE_LINES))
    except (OSError, UnicodeError):
        return False

    has_content = False
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        has_content = True
        tokens = line.split()
        if len(tokens) != 5:
            continue
        try:
            int(tokens[0])
            coordinates = [float(token) for token in tokens[1:]]
        except ValueError:
            continue
        if all(
            math.isfinite(value) and 0.0 <= value <= 1.0
            for value in coordinates
        ):
            return True
    return not has_content


def looks_like_class_names(path: Path) -> bool:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return False
    return bool(lines) and all(
        bool(line.strip())
        and ":" not in line
        and len(line.split()) == 1
        for line in lines
    )


def _yaml_has_names(path: Path) -> bool:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return False
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("names"), (dict, list))
    )


def classify_document(
    path: Path,
    logical_path: str,
    *,
    matches_image_stem: bool = False,
) -> DocumentRole:
    relative = PurePosixPath(logical_path.replace("\\", "/"))
    suffix = relative.suffix.lower()
    if suffix == ".txt":
        if matches_image_stem or looks_like_yolo_label(path):
            return "label"
        if (
            relative.name.lower() in CLASS_FILE_NAMES
            and looks_like_class_names(path)
        ):
            return "classfile"
        return "other"
    if suffix in {".yaml", ".yml"}:
        return "classfile" if _yaml_has_names(path) else "other"
    if suffix == ".names":
        return "classfile" if looks_like_class_names(path) else "other"
    return "other"
