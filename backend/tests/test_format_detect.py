from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from app.services.collect import CollectedFile
from app.services.format_detect import (
    detect_file_format,
    detect_formats,
)


pytestmark = pytest.mark.asyncio


def item(path: Path, rel_path: str) -> CollectedFile:
    return CollectedFile(
        rel_path=rel_path,
        abs_path=path,
        kind="other",
        split=None,
    )


async def test_detects_annotation_formats_from_content_not_directory(
    tmp_path: Path,
) -> None:
    coco = tmp_path / "foo.json"
    coco.write_text(
        '{"images":[],"annotations":[],"categories":[]}',
        encoding="utf-8",
    )
    voc = tmp_path / "foo.xml"
    voc.write_text(
        """
        <annotation>
          <object><name>person</name><bndbox>
            <xmin>1</xmin><ymin>2</ymin><xmax>3</xmax><ymax>4</ymax>
          </bndbox></object>
        </annotation>
        """,
        encoding="utf-8",
    )
    yolo = tmp_path / "anything.txt"
    yolo.write_text("0 0.5 0.5 0.2 0.3\n", encoding="utf-8")

    assert await detect_file_format(item(coco, "renamed/foo.json")) == "coco"
    assert await detect_file_format(item(voc, "random/foo.xml")) == "voc"
    assert await detect_file_format(item(yolo, "misc/anything.txt")) == "yolo"


async def test_non_annotation_metadata_is_not_misclassified(
    tmp_path: Path,
) -> None:
    contents = {
        "train.txt": "obj_train_data/frame-1.jpg\n",
        "labelmap.txt": "background:0,0,0::\nperson:255,0,0::\n",
        "obj.data": "classes = 2\ntrain = train.txt\nnames = obj.names\n",
    }

    for filename, content in contents.items():
        path = tmp_path / filename
        path.write_text(content, encoding="utf-8")
        assert await detect_file_format(item(path, filename)) == "unknown"


async def test_upload_detection_records_counts_and_priority(
    tmp_path: Path,
) -> None:
    coco = tmp_path / "annotations.data"
    coco.write_text(
        '{"images":[],"annotations":[],"categories":[]}',
        encoding="utf-8",
    )
    voc = tmp_path / "annotation.data"
    voc.write_text(
        "<annotation><object><bndbox /></object></annotation>",
        encoding="utf-8",
    )
    result = await detect_formats(
        [
            item(voc, "annotation.data"),
            item(coco, "annotations.data"),
        ]
    )

    assert result.primary == "coco"
    assert result.counts == {"coco": 1, "voc": 1, "yolo": 0}
    assert result.by_path == {
        "annotation.data": "voc",
        "annotations.data": "coco",
    }


async def test_json_parsing_runs_outside_event_loop_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import format_detect

    document = tmp_path / "annotations.json"
    document.write_text(
        '{"images":[],"annotations":[],"categories":[]}',
        encoding="utf-8",
    )
    caller_thread = threading.get_ident()
    parser_threads: list[int] = []
    original = format_detect._parse_json_document

    def observed_parser(path: Path):
        parser_threads.append(threading.get_ident())
        return original(path)

    monkeypatch.setattr(
        format_detect,
        "_parse_json_document",
        observed_parser,
    )

    assert (
        await detect_file_format(item(document, "annotations.json"))
        == "coco"
    )
    await asyncio.sleep(0)
    assert parser_threads
    assert all(thread_id != caller_thread for thread_id in parser_threads)
