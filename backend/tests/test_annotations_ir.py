from __future__ import annotations

import pytest

from app.services.annotations_ir import (
    IRBox,
    IRImage,
    SourceClass,
    normalize_source_classes,
)


def test_coco_ids_are_sorted_and_remapped_contiguously() -> None:
    normalized = normalize_source_classes(
        [
            SourceClass(source_id=7, name="forklift"),
            SourceClass(source_id=1, name="person"),
        ]
    )

    assert [(item.class_id, item.name) for item in normalized.classes] == [
        (0, "person"),
        (1, "forklift"),
    ]
    assert [
        (item.source_id, item.source_name, item.class_id)
        for item in normalized.mappings
    ] == [
        (1, "person", 0),
        (7, "forklift", 1),
    ]


def test_voc_names_are_sorted_and_remapped_contiguously() -> None:
    normalized = normalize_source_classes(
        [
            SourceClass(source_id=None, name="person"),
            SourceClass(source_id=None, name="orklift"),
            SourceClass(source_id=None, name="person"),
        ]
    )

    assert [(item.class_id, item.name) for item in normalized.classes] == [
        (0, "orklift"),
        (1, "person"),
    ]
    assert [
        (item.source_id, item.source_name, item.class_id)
        for item in normalized.mappings
    ] == [
        (None, "orklift", 0),
        (None, "person", 1),
    ]


def test_ir_uses_normalized_cxcywh_and_rejects_out_of_range_values() -> None:
    box = IRBox(class_id=0, cx=0.5, cy=0.5, w=0.25, h=0.5)
    image = IRImage(
        rel_path="images/frame.jpg",
        width=640,
        height=320,
        boxes=(box,),
    )

    assert image.boxes == (box,)
    with pytest.raises(ValueError, match="normalized"):
        IRBox(class_id=0, cx=1.1, cy=0.5, w=0.25, h=0.5)
    with pytest.raises(ValueError, match="positive"):
        IRImage(
            rel_path="images/frame.jpg",
            width=0,
            height=320,
            boxes=(),
        )
