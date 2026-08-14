"""COCO JSON to normalized annotation IR adapter."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from app.services.annotations_ir import (
    AnnotationIR,
    IRBox,
    IRImage,
    SourceClass,
    load_decoded_dimensions,
    match_image_reference,
    normalize_source_classes,
    safe_image_reference,
)
from app.services.collect import CollectedFile
from app.services.format_detect import load_json_document
from app.services.labels import IssueData


@dataclass(frozen=True)
class CocoAdapterResult:
    ir: AnnotationIR
    issues: tuple[IssueData, ...]
    source_by_image: dict[str, str]
    documents: tuple[str, ...]


def _issue(kind: str, path: str, detail: str) -> IssueData:
    return IssueData(kind=kind, path=path, detail=detail)


def _as_number(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite number")
    return number


def _normalized_box(
    bbox: Any,
    width: int,
    height: int,
    class_id: int,
) -> IRBox:
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise ValueError("bbox must contain [x, y, width, height]")
    x, y, box_width, box_height = (_as_number(value) for value in bbox)
    if (
        x < 0.0
        or y < 0.0
        or box_width < 0.0
        or box_height < 0.0
        or x + box_width > width
        or y + box_height > height
    ):
        raise ValueError("converted bbox is outside 0..1")
    return IRBox(
        class_id=class_id,
        cx=(x + box_width / 2.0) / width,
        cy=(y + box_height / 2.0) / height,
        w=box_width / width,
        h=box_height / height,
    )


async def adapt_coco(
    items: list[CollectedFile],
    *,
    coco_paths: set[str],
) -> CocoAdapterResult:
    documents = tuple(
        sorted(path for path in coco_paths if any(
            item.rel_path == path for item in items
        ))
    )
    item_by_path = {item.rel_path: item for item in items}
    payloads: list[tuple[str, dict[str, Any]]] = []
    issues: list[IssueData] = []
    for path in documents:
        payload = await load_json_document(item_by_path[path].abs_path)
        if not isinstance(payload, dict):
            issues.append(
                _issue("broken_label", path, "COCO document could not be parsed")
            )
            continue
        payloads.append((path, payload))

    source_classes: list[SourceClass] = []
    for path, payload in payloads:
        for category in payload.get("categories", []):
            if not isinstance(category, dict):
                issues.append(
                    _issue("broken_label", path, "invalid COCO category entry")
                )
                continue
            try:
                source_id = int(category["id"])
                name = str(category["name"]).strip()
            except (KeyError, TypeError, ValueError):
                issues.append(
                    _issue("broken_label", path, "invalid COCO category entry")
                )
                continue
            if not name:
                issues.append(
                    _issue("broken_label", path, "empty COCO category name")
                )
                continue
            source_classes.append(SourceClass(source_id=source_id, name=name))

    normalized = normalize_source_classes(source_classes)
    class_id_by_source = {
        mapping.source_id: mapping.class_id
        for mapping in normalized.mappings
    }
    image_files = [item for item in items if item.kind == "image"]
    metadata_by_source_id: dict[tuple[str, Any], tuple[CollectedFile, dict[str, Any]]] = {}
    source_by_image: dict[str, str] = {}
    matched_images: dict[str, CollectedFile] = {}
    metadata_dimensions: dict[str, tuple[Any, Any]] = {}

    for source_path, payload in payloads:
        for metadata in payload.get("images", []):
            if not isinstance(metadata, dict) or "id" not in metadata:
                issues.append(
                    _issue("broken_label", source_path, "invalid COCO image entry")
                )
                continue
            reference = safe_image_reference(metadata.get("file_name"))
            matched = (
                match_image_reference(reference, image_files)
                if reference is not None
                else None
            )
            if matched is None:
                issues.append(
                    _issue(
                        "label_without_image",
                        source_path,
                        (
                            "COCO file_name did not match exactly one image: "
                            f"{metadata.get('file_name')!r}"
                        ),
                    )
                )
                continue
            if matched.rel_path in source_by_image:
                continue
            source_by_image[matched.rel_path] = source_path
            matched_images[matched.rel_path] = matched
            metadata_by_source_id[(source_path, metadata["id"])] = (
                matched,
                metadata,
            )
            metadata_dimensions[matched.rel_path] = (
                metadata.get("width"),
                metadata.get("height"),
            )

    dimensions, dimension_failures = await load_decoded_dimensions(
        list(matched_images.values())
    )
    for rel_path, detail in dimension_failures.items():
        issues.append(
            _issue(
                "broken_label",
                source_by_image[rel_path],
                f"{rel_path}: actual image dimensions unavailable: {detail}",
            )
        )
    for rel_path, actual in dimensions.items():
        declared = metadata_dimensions.get(rel_path)
        if declared != actual:
            issues.append(
                _issue(
                    "broken_label",
                    source_by_image[rel_path],
                    (
                        f"{rel_path}: metadata dimensions {declared!r} "
                        f"differ from decoded dimensions {actual!r}; "
                        "decoded dimensions used"
                    ),
                )
            )

    boxes_by_image: dict[str, list[IRBox]] = defaultdict(list)
    for source_path, payload in payloads:
        for annotation in payload.get("annotations", []):
            if not isinstance(annotation, dict):
                issues.append(
                    _issue("broken_label", source_path, "invalid COCO annotation")
                )
                continue
            metadata_entry = metadata_by_source_id.get(
                (source_path, annotation.get("image_id"))
            )
            if metadata_entry is None:
                continue
            image_item, _ = metadata_entry
            actual = dimensions.get(image_item.rel_path)
            if actual is None:
                continue
            if "bbox" not in annotation:
                detail = (
                    "segmentation-only annotation skipped because bbox is missing"
                    if annotation.get("segmentation")
                    else "annotation skipped because bbox is missing"
                )
                issues.append(_issue("broken_label", source_path, detail))
                continue
            try:
                source_class_id = int(annotation["category_id"])
                class_id = class_id_by_source[source_class_id]
                box = _normalized_box(
                    annotation["bbox"],
                    actual[0],
                    actual[1],
                    class_id,
                )
            except (KeyError, TypeError, ValueError) as error:
                issues.append(
                    _issue(
                        "broken_label",
                        source_path,
                        (
                            f"annotation {annotation.get('id')!r} skipped: "
                            f"{error}"
                        ),
                    )
                )
                continue
            boxes_by_image[image_item.rel_path].append(box)

    ir_images = tuple(
        IRImage(
            rel_path=rel_path,
            width=dimensions[rel_path][0],
            height=dimensions[rel_path][1],
            boxes=tuple(boxes_by_image.get(rel_path, [])),
        )
        for rel_path in sorted(source_by_image)
        if rel_path in dimensions
    )
    return CocoAdapterResult(
        ir=AnnotationIR(
            images=ir_images,
            classes=normalized.classes,
            class_mappings=normalized.mappings,
        ),
        issues=tuple(issues),
        source_by_image=source_by_image,
        documents=documents,
    )
