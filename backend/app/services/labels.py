"""Lossless YOLO label parsing with line-scoped issue reporting."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.services.classify import (
    CLASS_FILE_NAMES,
    looks_like_class_names,
)
from app.services.collect import CollectedFile


@dataclass(frozen=True)
class ParsedBox:
    class_id: int
    cx: float
    cy: float
    w: float
    h: float


@dataclass(frozen=True)
class IssueData:
    kind: str
    path: str
    detail: str


@dataclass(frozen=True)
class ParsedLabel:
    boxes: list[ParsedBox]
    issues: list[IssueData]


@dataclass(frozen=True)
class LoadedClasses:
    classes: dict[int, str]
    source: CollectedFile | None


class ClassFileError(ValueError):
    pass


def _broken(path: str, line_number: int, reason: str) -> IssueData:
    return IssueData(
        kind="broken_label",
        path=path,
        detail=f"line {line_number}: {reason}",
    )


def parse_yolo_label(
    path: Path,
    known_classes: dict[int, str] | None,
    rel_path: str | None = None,
) -> ParsedLabel:
    boxes: list[ParsedBox] = []
    issues: list[IssueData] = []
    issue_path = rel_path or path.name
    content = path.read_text(encoding="utf-8-sig")

    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        tokens = line.split()
        if len(tokens) != 5:
            issues.append(_broken(issue_path, line_number, "expected 5 tokens"))
            continue
        try:
            class_id = int(tokens[0])
            values = tuple(float(token) for token in tokens[1:])
        except ValueError:
            issues.append(_broken(issue_path, line_number, "invalid number"))
            continue
        if class_id < 0:
            issues.append(_broken(issue_path, line_number, "negative class id"))
            continue
        if not all(math.isfinite(value) for value in values):
            issues.append(_broken(issue_path, line_number, "non-finite coordinate"))
            continue
        if not all(0.0 <= value <= 1.0 for value in values):
            issues.append(
                _broken(issue_path, line_number, "coordinate outside 0..1")
            )
            continue
        if known_classes is not None and class_id not in known_classes:
            issues.append(_broken(issue_path, line_number, "unknown class id"))
            continue
        boxes.append(ParsedBox(class_id, *values))

    return ParsedLabel(boxes=boxes, issues=issues)


def _classes_from_text(path: Path) -> dict[int, str]:
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
    ]
    return {
        class_id: name
        for class_id, name in enumerate(lines)
    }


def _classes_from_yaml(path: Path) -> dict[int, str]:
    try:
        payload: Any = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ClassFileError(f"cannot parse {path.name}: {error}") from error
    if not isinstance(payload, dict) or "names" not in payload:
        raise ClassFileError(f"{path.name} has no names entry")
    names = payload["names"]
    if isinstance(names, list):
        return {index: str(name) for index, name in enumerate(names)}
    if isinstance(names, dict):
        try:
            classes = {int(key): str(value) for key, value in names.items()}
        except (TypeError, ValueError) as error:
            raise ClassFileError(f"{path.name} has invalid class ids") from error
        if any(class_id < 0 for class_id in classes):
            raise ClassFileError(f"{path.name} has negative class ids")
        return dict(sorted(classes.items()))
    raise ClassFileError(f"{path.name} names must be a list or mapping")


def _class_file_priority(item: CollectedFile) -> int | None:
    path = Path(item.rel_path)
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        return 0
    if path.name.lower() in CLASS_FILE_NAMES:
        return 1
    if suffix == ".names":
        return 2
    return None


def load_classes_with_source(
    class_files: list[CollectedFile],
) -> LoadedClasses:
    candidates = sorted(
        (
            (priority, item)
            for item in class_files
            if item.kind == "classfile"
            if (priority := _class_file_priority(item)) is not None
        ),
        key=lambda candidate: (candidate[0], candidate[1].rel_path),
    )
    for _, selected in candidates:
        suffix = Path(selected.rel_path).suffix.lower()
        try:
            if suffix in {".yaml", ".yml"}:
                classes = _classes_from_yaml(selected.abs_path)
            else:
                if not looks_like_class_names(selected.abs_path):
                    continue
                classes = _classes_from_text(selected.abs_path)
        except ClassFileError:
            continue
        return LoadedClasses(classes=classes, source=selected)
    return LoadedClasses(classes={}, source=None)


def load_classes(class_files: list[CollectedFile]) -> dict[int, str]:
    return load_classes_with_source(class_files).classes


def classes_for_ids(class_ids: set[int]) -> dict[int, str]:
    return {class_id: str(class_id) for class_id in sorted(class_ids)}
