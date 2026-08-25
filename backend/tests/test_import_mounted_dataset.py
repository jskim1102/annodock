from __future__ import annotations

from pathlib import Path

from scripts.import_mounted_dataset import build_mounted_items


def test_build_mounted_items_keeps_pairs_and_external_class_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "yolo_dataset"
    image = source / "images" / "train" / "frame.jpg"
    label = source / "labels" / "train" / "frame.txt"
    cache = source / "labels" / "train.cache"
    metadata = tmp_path / "forklift.yaml"
    image.parent.mkdir(parents=True)
    label.parent.mkdir(parents=True)
    image.write_bytes(b"\xff\xd8\xfftest")
    label.write_text("0 0.5 0.5 0.2 0.2\n", encoding="utf-8")
    cache.write_bytes(b"cache")
    metadata.write_text("names:\n  0: forklift\n", encoding="utf-8")

    items = build_mounted_items(
        source,
        metadata,
        ("jpg", "png"),
    )

    assert [(item.rel_path, item.kind) for item in items] == [
        ("forklift.yaml", "classfile"),
        ("images/train/frame.jpg", "image"),
        ("labels/train/frame.txt", "label"),
    ]


def test_build_mounted_items_rejects_metadata_inside_dataset_tree(
    tmp_path: Path,
) -> None:
    source = tmp_path / "yolo_dataset"
    source.mkdir()
    metadata = source / "data.yaml"
    metadata.write_text("names: [forklift]\n", encoding="utf-8")

    items = build_mounted_items(source, metadata, ("jpg",))

    assert [(item.rel_path, item.kind) for item in items] == [
        ("data.yaml", "classfile"),
    ]
