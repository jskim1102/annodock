from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image as PillowImage

from app.services.adapter_voc import adapt_voc
from app.services.collect import CollectedFile


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


async def test_voc_adapter_uses_xml_names_and_decoded_dimensions(
    tmp_path: Path,
) -> None:
    image = tmp_path / "frame.jpg"
    PillowImage.new("RGB", (100, 50), (90, 60, 30)).save(image, "JPEG")
    annotation = tmp_path / "frame.xml"
    annotation.write_text(
        """
        <annotation>
          <filename>frame.jpg</filename>
          <size><width>200</width><height>100</height></size>
          <object>
            <name>person</name><difficult>1</difficult><truncated>1</truncated>
            <bndbox>
              <xmin>10</xmin><ymin>5</ymin>
              <xmax>30</xmax><ymax>15</ymax>
            </bndbox>
          </object>
          <object>
            <name>forklift</name>
            <bndbox>
              <xmin>95</xmin><ymin>5</ymin>
              <xmax>110</xmax><ymax>15</ymax>
            </bndbox>
          </object>
        </annotation>
        """,
        encoding="utf-8",
    )
    labelmap = tmp_path / "labelmap.txt"
    labelmap.write_text(
        "background:0,0,0::\nperson:255,0,0::\n",
        encoding="utf-8",
    )

    result = await adapt_voc(
        [
            collected(image, "JPEGImages/frame.jpg", "image"),
            collected(annotation, "Annotations/frame.xml", "other"),
            collected(labelmap, "labelmap.txt", "other"),
        ],
        voc_paths={"Annotations/frame.xml"},
    )

    assert [(item.class_id, item.name) for item in result.ir.classes] == [
        (0, "forklift"),
        (1, "person"),
    ]
    assert all(item.name != "background" for item in result.ir.classes)
    assert [
        (item.source_id, item.source_name, item.class_id)
        for item in result.ir.class_mappings
    ] == [
        (None, "forklift", 0),
        (None, "person", 1),
    ]
    assert len(result.ir.images) == 1
    adapted = result.ir.images[0]
    assert (adapted.rel_path, adapted.width, adapted.height) == (
        "JPEGImages/frame.jpg",
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
        "JPEGImages/frame.jpg": "Annotations/frame.xml"
    }
    assert result.documents == ("Annotations/frame.xml",)
    assert any("metadata dimensions" in issue.detail for issue in result.issues)
    assert any("outside 0..1" in issue.detail for issue in result.issues)


async def test_voc_adapter_reports_missing_filename_image(
    tmp_path: Path,
) -> None:
    annotation = tmp_path / "missing.xml"
    annotation.write_text(
        """
        <annotation>
          <filename>missing.jpg</filename>
          <size><width>20</width><height>10</height></size>
          <object><name>person</name><bndbox>
            <xmin>1</xmin><ymin>1</ymin>
            <xmax>3</xmax><ymax>3</ymax>
          </bndbox></object>
        </annotation>
        """,
        encoding="utf-8",
    )

    result = await adapt_voc(
        [collected(annotation, "Annotations/missing.xml", "other")],
        voc_paths={"Annotations/missing.xml"},
    )

    assert result.ir.images == ()
    assert len(result.issues) == 1
    assert result.issues[0].kind == "label_without_image"
    assert "missing.jpg" in result.issues[0].detail
