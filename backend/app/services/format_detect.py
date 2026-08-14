"""Content-based annotation format detection."""

from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from xml.etree import ElementTree

from app.services.classify import looks_like_yolo_label
from app.services.collect import CollectedFile


AnnotationFormat = Literal["coco", "voc", "yolo", "unknown"]
KNOWN_FORMATS: tuple[AnnotationFormat, ...] = ("coco", "voc", "yolo")
FORMAT_PRIORITY: tuple[AnnotationFormat, ...] = ("yolo", "coco", "voc")
FORMAT_EXECUTOR = ThreadPoolExecutor(
    max_workers=min(4, os.cpu_count() or 1),
    thread_name_prefix="dataset-format",
)


@dataclass(frozen=True)
class FormatDetection:
    primary: AnnotationFormat
    counts: dict[str, int]
    by_path: dict[str, AnnotationFormat]


def _parse_json_document(path: Path) -> Any | None:
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            return json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _is_coco_document(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and all(key in payload for key in ("images", "annotations", "categories"))
        and isinstance(payload["images"], list)
        and isinstance(payload["annotations"], list)
        and isinstance(payload["categories"], list)
    )


def _is_voc_document(root: ElementTree.Element) -> bool:
    return (
        root.tag == "annotation"
        and root.find(".//object") is not None
        and root.find(".//object/bndbox") is not None
    )


def _parse_xml_document(path: Path) -> ElementTree.Element | None:
    try:
        return ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError):
        return None


def _leading_non_whitespace(path: Path) -> bytes:
    try:
        with path.open("rb") as source:
            prefix = source.read(4096)
    except OSError:
        return b""
    return prefix.lstrip()[:1]


def _detect_file_format_sync(item: CollectedFile) -> AnnotationFormat:
    if item.kind == "label":
        return "yolo"
    if item.kind in {"image", "classfile", "zip"}:
        return "unknown"
    leading = _leading_non_whitespace(item.abs_path)
    if leading in {b"{", b"["}:
        if _is_coco_document(_parse_json_document(item.abs_path)):
            return "coco"
    elif leading == b"<":
        root = _parse_xml_document(item.abs_path)
        if root is not None and _is_voc_document(root):
            return "voc"

    if (
        Path(item.rel_path).suffix.lower() == ".txt"
        and looks_like_yolo_label(item.abs_path)
    ):
        return "yolo"
    return "unknown"


async def detect_file_format(item: CollectedFile) -> AnnotationFormat:
    if item.kind == "label":
        return "yolo"
    if item.kind in {"image", "classfile", "zip"}:
        return "unknown"
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        FORMAT_EXECUTOR,
        _detect_file_format_sync,
        item,
    )


async def load_json_document(path: Path) -> Any | None:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        FORMAT_EXECUTOR,
        _parse_json_document,
        path,
    )


async def load_xml_document(path: Path) -> ElementTree.Element | None:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        FORMAT_EXECUTOR,
        _parse_xml_document,
        path,
    )


async def find_voc_annotation_documents(
    items: list[CollectedFile],
) -> set[str]:
    candidates = [
        item
        for item in items
        if item.kind == "other"
    ]
    roots = await asyncio.gather(
        *(load_xml_document(item.abs_path) for item in candidates)
    )
    return {
        item.rel_path
        for item, root in zip(candidates, roots, strict=True)
        if root is not None and root.tag == "annotation"
    }


async def detect_formats(items: list[CollectedFile]) -> FormatDetection:
    by_path: dict[str, AnnotationFormat] = {
        item.rel_path: "yolo"
        for item in items
        if item.kind == "label"
    }
    candidates = [
        item
        for item in items
        if item.kind == "other"
    ]
    detected = await asyncio.gather(
        *(detect_file_format(item) for item in candidates)
    )
    by_path.update(
        {
            item.rel_path: annotation_format
            for item, annotation_format in zip(
                candidates,
                detected,
                strict=True,
            )
            if annotation_format != "unknown"
        }
    )
    counted = Counter(by_path.values())
    counts = {
        annotation_format: counted.get(annotation_format, 0)
        for annotation_format in KNOWN_FORMATS
    }
    primary = next(
        (
            annotation_format
            for annotation_format in FORMAT_PRIORITY
            if counts[annotation_format] > 0
        ),
        "unknown",
    )
    return FormatDetection(
        primary=primary,
        counts=counts,
        by_path=by_path,
    )
