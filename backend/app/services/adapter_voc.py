"""Pascal VOC XML to normalized annotation IR adapter."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree

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
from app.services.format_detect import load_xml_document
from app.services.labels import IssueData


@dataclass(frozen=True)
class VocAdapterResult:
    ir: AnnotationIR
    issues: tuple[IssueData, ...]
    source_by_image: dict[str, str]
    documents: tuple[str, ...]


def _issue(kind: str, path: str, detail: str) -> IssueData:
    return IssueData(kind=kind, path=path, detail=detail)


def _number(node: ElementTree.Element, name: str) -> float:
    text = node.findtext(name)
    if text is None:
        raise ValueError(f"missing {name}")
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(f"non-finite {name}")
    return number


def _declared_dimensions(
    root: ElementTree.Element,
) -> tuple[int, int] | None:
    size = root.find("size")
    if size is None:
        return None
    try:
        width = int(_number(size, "width"))
        height = int(_number(size, "height"))
    except ValueError:
        return None
    return (width, height)


def _normalized_box(
    node: ElementTree.Element,
    width: int,
    height: int,
    class_id: int,
) -> IRBox:
    xmin = _number(node, "xmin")
    ymin = _number(node, "ymin")
    xmax = _number(node, "xmax")
    ymax = _number(node, "ymax")
    box_width = xmax - xmin
    box_height = ymax - ymin
    if (
        xmin < 0.0
        or ymin < 0.0
        or box_width < 0.0
        or box_height < 0.0
        or xmax > width
        or ymax > height
    ):
        raise ValueError("converted bbox is outside 0..1")
    return IRBox(
        class_id=class_id,
        cx=(xmin + xmax) / 2.0 / width,
        cy=(ymin + ymax) / 2.0 / height,
        w=box_width / width,
        h=box_height / height,
    )


async def adapt_voc(
    items: list[CollectedFile],
    *,
    voc_paths: set[str],
) -> VocAdapterResult:
    documents = tuple(
        sorted(path for path in voc_paths if any(
            item.rel_path == path for item in items
        ))
    )
    item_by_path = {item.rel_path: item for item in items}
    roots: list[tuple[str, ElementTree.Element]] = []
    issues: list[IssueData] = []
    for path in documents:
        root = await load_xml_document(item_by_path[path].abs_path)
        if root is None or root.tag != "annotation":
            issues.append(
                _issue("broken_label", path, "VOC document could not be parsed")
            )
            continue
        roots.append((path, root))

    source_classes = [
        SourceClass(source_id=None, name=name.strip())
        for _, root in roots
        for obj in root.findall("object")
        if (name := obj.findtext("name")) is not None
        if name.strip()
    ]
    normalized = normalize_source_classes(source_classes)
    class_id_by_name = {
        mapping.source_name: mapping.class_id
        for mapping in normalized.mappings
    }
    image_files = [item for item in items if item.kind == "image"]
    root_by_image: dict[str, tuple[str, ElementTree.Element]] = {}
    source_by_image: dict[str, str] = {}
    matched_images: dict[str, CollectedFile] = {}
    metadata_dimensions: dict[str, tuple[int, int] | None] = {}

    for source_path, root in roots:
        reference = safe_image_reference(root.findtext("filename"))
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
                        "VOC filename did not match exactly one image: "
                        f"{root.findtext('filename')!r}"
                    ),
                )
            )
            continue
        if matched.rel_path in source_by_image:
            continue
        source_by_image[matched.rel_path] = source_path
        matched_images[matched.rel_path] = matched
        root_by_image[matched.rel_path] = (source_path, root)
        metadata_dimensions[matched.rel_path] = _declared_dimensions(root)

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
    for rel_path, (source_path, root) in root_by_image.items():
        actual = dimensions.get(rel_path)
        if actual is None:
            continue
        for object_index, obj in enumerate(root.findall("object"), start=1):
            name = (obj.findtext("name") or "").strip()
            bndbox = obj.find("bndbox")
            if not name or bndbox is None:
                issues.append(
                    _issue(
                        "broken_label",
                        source_path,
                        f"object {object_index} skipped: missing name or bndbox",
                    )
                )
                continue
            try:
                box = _normalized_box(
                    bndbox,
                    actual[0],
                    actual[1],
                    class_id_by_name[name],
                )
            except (KeyError, TypeError, ValueError) as error:
                issues.append(
                    _issue(
                        "broken_label",
                        source_path,
                        f"object {object_index} skipped: {error}",
                    )
                )
                continue
            boxes_by_image[rel_path].append(box)

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
    return VocAdapterResult(
        ir=AnnotationIR(
            images=ir_images,
            classes=normalized.classes,
            class_mappings=normalized.mappings,
        ),
        issues=tuple(issues),
        source_by_image=source_by_image,
        documents=documents,
    )

