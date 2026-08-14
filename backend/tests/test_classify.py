from __future__ import annotations

from pathlib import Path

import pytest

from app.services.classify import classify_document, looks_like_yolo_label
from app.services.collect import SourceFile, collect_sources


ALLOWED = ("jpg", "png")


@pytest.mark.parametrize(
    "content",
    [
        "",
        "0 0.5 0.5 0.2 0.2\n",
        "0 0 1 1 0.1\n5 0.25 0.75 0.5 0.4\n",
    ],
)
def test_yolo_shape_accepts_valid_and_empty_labels(
    tmp_path: Path,
    content: str,
) -> None:
    path = tmp_path / "arbitrary.txt"
    path.write_text(content, encoding="utf-8")

    assert looks_like_yolo_label(path)
    assert classify_document(path, "any-name.txt") == "label"


@pytest.mark.parametrize(
    "content",
    [
        "data/obj_train_data/frame_001.jpg\n",
        "background:0,0,0::\nperson:128,0,0::\n",
        "frame_001\nframe_002\n",
        "0 0.5 0.5 0.2\n",
        "zero 0.5 0.5 0.2 0.2\n",
        "0 1.1 0.5 0.2 0.2\n",
    ],
)
def test_non_yolo_text_shapes_are_not_labels(
    tmp_path: Path,
    content: str,
) -> None:
    path = tmp_path / "input.txt"
    path.write_text(content, encoding="utf-8")

    assert not looks_like_yolo_label(path)
    assert classify_document(path, "arbitrary.txt") == "other"


def test_real_export_noise_shapes_are_rejected_with_bounded_sampling(
    tmp_path: Path,
) -> None:
    train = tmp_path / "train.txt"
    train.write_text(
        "".join(
            f"data/obj_train_data/frame_{index:03}.jpg\n"
            for index in range(151)
        ),
        encoding="utf-8",
    )
    labelmap = tmp_path / "labelmap.txt"
    labelmap.write_text(
        "background:0,0,0::\nperson:128,0,0::\n",
        encoding="utf-8",
    )
    default = tmp_path / "default.txt"
    default.write_text(
        "".join(f"frame_{index:03}\n" for index in range(120)),
        encoding="utf-8",
    )

    assert not looks_like_yolo_label(train)
    assert not looks_like_yolo_label(labelmap)
    assert not looks_like_yolo_label(default)


def test_only_first_twenty_lines_are_sampled(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"
    path.write_text(
        ("0 0.5 0.5 0.2 0.2\n" * 20) + "not sampled\n",
        encoding="utf-8",
    )

    assert looks_like_yolo_label(path)


def test_yolo_shape_accepts_a_file_with_any_valid_sampled_line(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mixed.txt"
    path.write_text(
        "not a label\n"
        "0 0.5 0.5 0.2 0.2\n"
        "0 1.1 0.5 0.2 0.2\n",
        encoding="utf-8",
    )

    assert looks_like_yolo_label(path)
    assert classify_document(path, "mixed.txt") == "label"


def test_class_candidates_are_filename_bounded_and_shape_checked(
    tmp_path: Path,
) -> None:
    classes = tmp_path / "classes.txt"
    classes.write_text("person\nforklift\n", encoding="utf-8")
    names = tmp_path / "obj.names"
    names.write_text("person\nforklift\n", encoding="utf-8")
    yaml = tmp_path / "data.yaml"
    yaml.write_text("names: [person, forklift]\n", encoding="utf-8")
    labelmap = tmp_path / "labelmap.txt"
    labelmap.write_text("background:0,0,0::\n", encoding="utf-8")
    random_names = tmp_path / "random.txt"
    random_names.write_text("person\nforklift\n", encoding="utf-8")

    assert classify_document(classes, "classes.txt") == "classfile"
    assert classify_document(names, "obj.names") == "classfile"
    assert classify_document(yaml, "data.yaml") == "classfile"
    assert classify_document(labelmap, "labelmap.txt") == "other"
    assert classify_document(random_names, "random.txt") == "other"


def test_collection_uses_content_roles_for_txt_and_names(
    tmp_path: Path,
) -> None:
    paths = {
        "obj_train_data/frame.jpg": b"image",
        "obj_train_data/frame.txt": b"0 0.5 0.5 0.2 0.2\n",
        "obj.names": b"person\nforklift\n",
        "train.txt": b"data/obj_train_data/frame.jpg\n",
    }
    sources: list[SourceFile] = []
    for relative, content in paths.items():
        absolute = tmp_path / relative
        absolute.parent.mkdir(parents=True, exist_ok=True)
        absolute.write_bytes(content)
        sources.append(SourceFile(rel_path=relative, abs_path=absolute))

    collected = collect_sources(sources, ALLOWED)

    assert [(item.rel_path, item.kind) for item in collected] == [
        ("obj.names", "classfile"),
        ("obj_train_data/frame.jpg", "image"),
        ("obj_train_data/frame.txt", "label"),
        ("train.txt", "other"),
    ]


def test_collection_uses_input_image_stems_for_broken_labels(
    tmp_path: Path,
) -> None:
    paths = {
        "images/train/frame.jpg": b"not-a-valid-image",
        "labels/train/frame.txt": b"broken label\n0 1.1 0.5 0.2 0.2\n",
        "train.txt": b"data/images/train/frame.jpg\n",
        "labelmap.txt": b"background:0,0,0::\nperson:128,0,0::\n",
        "ImageSets/Main/default.txt": b"frame\n",
    }
    sources: list[SourceFile] = []
    for relative, content in paths.items():
        absolute = tmp_path / relative
        absolute.parent.mkdir(parents=True, exist_ok=True)
        absolute.write_bytes(content)
        sources.append(SourceFile(rel_path=relative, abs_path=absolute))

    collected = collect_sources(sources, ALLOWED)

    assert [(item.rel_path, item.kind) for item in collected] == [
        ("ImageSets/Main/default.txt", "other"),
        ("images/train/frame.jpg", "image"),
        ("labelmap.txt", "other"),
        ("labels/train/frame.txt", "label"),
        ("train.txt", "other"),
    ]
