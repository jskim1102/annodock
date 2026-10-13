from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image as PillowImage

from app.services.adapter_coco import adapt_coco
from app.services.collect import CollectedFile, collect_directory


pytestmark = pytest.mark.asyncio


def collected(
    path: Path,
    rel_path: str,
    kind: str,
) -> CollectedFile:
    return CollectedFile(
        rel_path=rel_path,
        abs_path=path,
        kind=kind,  # type: ignore[arg-type]
        split=None,
    )


async def test_coco_adapter_uses_decoded_dimensions_and_reports_skips(
    tmp_path: Path,
) -> None:
    image = tmp_path / "frame.jpg"
    PillowImage.new("RGB", (100, 50), (30, 60, 90)).save(image, "JPEG")
    document = tmp_path / "renamed.json"
    document.write_text(
        json.dumps(
            {
                "images": [
                    {
                        "id": 10,
                        "file_name": "frame.jpg",
                        "width": 200,
                        "height": 100,
                    }
                ],
                "categories": [
                    {"id": 7, "name": "forklift"},
                    {"id": 1, "name": "person"},
                ],
                "annotations": [
                    {
                        "id": 1,
                        "image_id": 10,
                        "category_id": 7,
                        "bbox": [10, 5, 20, 10],
                        "iscrowd": 1,
                    },
                    {
                        "id": 2,
                        "image_id": 10,
                        "category_id": 1,
                        "segmentation": [[1, 1, 2, 2, 3, 3]],
                    },
                    {
                        "id": 3,
                        "image_id": 10,
                        "category_id": 1,
                        "bbox": [95, 5, 20, 10],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    result = await adapt_coco(
        [
            collected(image, "images/frame.jpg", "image"),
            collected(document, "metadata/anything.json", "other"),
        ],
        coco_paths={"metadata/anything.json"},
    )

    assert [(item.class_id, item.name) for item in result.ir.classes] == [
        (0, "person"),
        (1, "forklift"),
    ]
    assert [
        (item.source_id, item.source_name, item.class_id)
        for item in result.ir.class_mappings
    ] == [
        (1, "person", 0),
        (7, "forklift", 1),
    ]
    assert len(result.ir.images) == 1
    adapted = result.ir.images[0]
    assert (adapted.rel_path, adapted.width, adapted.height) == (
        "images/frame.jpg",
        100,
        50,
    )
    assert len(adapted.boxes) == 1
    box = adapted.boxes[0]
    assert box.class_id == 1
    assert (box.cx, box.cy, box.w, box.h) == pytest.approx(
        (0.2, 0.2, 0.2, 0.2)
    )
    assert result.source_by_image == {
        "images/frame.jpg": "metadata/anything.json"
    }
    assert result.documents == ("metadata/anything.json",)
    assert [issue.kind for issue in result.issues] == [
        "broken_label",
        "broken_label",
        "broken_label",
    ]
    assert any("metadata dimensions" in issue.detail for issue in result.issues)
    assert any("segmentation-only" in issue.detail for issue in result.issues)
    assert any("outside 0..1" in issue.detail for issue in result.issues)


async def test_coco_adapter_reports_missing_image_reference(
    tmp_path: Path,
) -> None:
    document = tmp_path / "annotations.json"
    document.write_text(
        json.dumps(
            {
                "images": [
                    {
                        "id": 1,
                        "file_name": "missing.jpg",
                        "width": 20,
                        "height": 10,
                    }
                ],
                "categories": [{"id": 1, "name": "person"}],
                "annotations": [
                    {
                        "id": 1,
                        "image_id": 1,
                        "category_id": 1,
                        "bbox": [1, 1, 2, 2],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = await adapt_coco(
        [collected(document, "annotations.json", "other")],
        coco_paths={"annotations.json"},
    )

    assert result.ir.images == ()
    assert len(result.issues) == 1
    assert result.issues[0].kind == "label_without_image"
    assert "missing.jpg" in result.issues[0].detail


@pytest.mark.parametrize("nested_directories", [False, True])
async def test_coco_adapter_resolves_repeated_filenames_in_each_split(
    tmp_path: Path,
    nested_directories: bool,
) -> None:
    documents: set[str] = set()
    expected_sources: dict[str, str] = {}
    for split, box_width in (("train", 4), ("valid", 8)):
        split_root = tmp_path / split
        image_root = split_root / "images" if nested_directories else split_root
        annotation_root = (
            split_root / "annotations" if nested_directories else split_root
        )
        image_root.mkdir(parents=True, exist_ok=True)
        annotation_root.mkdir(parents=True, exist_ok=True)
        image = image_root / "frame.jpg"
        PillowImage.new("RGB", (20, 20)).save(image, "JPEG")
        document = annotation_root / "_annotations.coco.json"
        document.write_text(
            json.dumps({
                "images": [{
                    "id": 1, "file_name": "frame.jpg", "width": 20, "height": 20,
                }],
                "categories": [{"id": 0, "name": "person"}],
                "annotations": [{
                    "id": 1, "image_id": 1, "category_id": 0,
                    "bbox": [2, 2, box_width, 4],
                }],
            }),
            encoding="utf-8",
        )
        document_path = document.relative_to(tmp_path).as_posix()
        documents.add(document_path)
        expected_sources[image.relative_to(tmp_path).as_posix()] = document_path

    result = await adapt_coco(
        collect_directory(tmp_path, ("jpg",)),
        coco_paths=documents,
    )

    assert result.issues == ()
    assert result.source_by_image == expected_sources
    assert len(result.ir.images) == 2
    assert [len(image.boxes) for image in result.ir.images] == [1, 1]
    assert [image.boxes[0].w for image in result.ir.images] == pytest.approx(
        [0.2, 0.4]
    )
