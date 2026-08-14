from __future__ import annotations

from pathlib import Path

from app.services.collect import CollectedFile
from app.services.labels import (
    classes_for_ids,
    load_classes,
    parse_yolo_label,
)


def class_file(path: Path, relative: str) -> CollectedFile:
    return CollectedFile(
        rel_path=relative,
        abs_path=path,
        kind="classfile",
        split=None,
    )


def test_valid_and_empty_yolo_labels(tmp_path: Path) -> None:
    valid = tmp_path / "valid.txt"
    valid.write_text(
        "0 0.5 0.5 0.25 0.4\n1 0 1 1 0.1\n",
        encoding="utf-8",
    )
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")

    parsed = parse_yolo_label(valid, {0: "person", 1: "car"}, "valid.txt")
    parsed_empty = parse_yolo_label(empty, {0: "person"}, "empty.txt")

    assert [
        (box.class_id, box.cx, box.cy, box.w, box.h) for box in parsed.boxes
    ] == [
        (0, 0.5, 0.5, 0.25, 0.4),
        (1, 0.0, 1.0, 1.0, 0.1),
    ]
    assert parsed.issues == []
    assert parsed_empty.boxes == []
    assert parsed_empty.issues == []


def test_broken_lines_are_skipped_not_clamped_and_original_is_unchanged(
    tmp_path: Path,
) -> None:
    label = tmp_path / "broken.txt"
    original = (
        "0 0.5 0.5 0.2 0.2\n"
        "0 1.2 0.5 0.2 0.2\n"
        "not-a-label\n"
        "9 0.5 0.5 0.2 0.2\n"
    )
    label.write_text(original, encoding="utf-8")

    parsed = parse_yolo_label(label, {0: "person"}, "labels/broken.txt")

    assert len(parsed.boxes) == 1
    assert len(parsed.issues) == 3
    assert all(issue.kind == "broken_label" for issue in parsed.issues)
    assert [issue.path for issue in parsed.issues] == [
        "labels/broken.txt",
        "labels/broken.txt",
        "labels/broken.txt",
    ]
    assert "line 2" in parsed.issues[0].detail
    assert "line 3" in parsed.issues[1].detail
    assert "line 4" in parsed.issues[2].detail
    assert label.read_text(encoding="utf-8") == original


def test_yaml_has_priority_over_classes_txt(tmp_path: Path) -> None:
    text = tmp_path / "classes.txt"
    text.write_text("person\ntruck\nhelmet\n", encoding="utf-8")
    yaml = tmp_path / "data.yaml"
    yaml.write_text("names: [yaml-person, yaml-truck]\n", encoding="utf-8")

    classes = load_classes(
        [
            class_file(yaml, "data.yaml"),
            class_file(text, "classes.txt"),
        ]
    )
    assert classes == {0: "yaml-person", 1: "yaml-truck"}


def test_classes_txt_has_priority_over_names_and_uses_line_order(
    tmp_path: Path,
) -> None:
    text = tmp_path / "classes.txt"
    text.write_text("person\ntruck\nhelmet\n", encoding="utf-8")
    names = tmp_path / "obj.names"
    names.write_text("wrong\n", encoding="utf-8")

    classes = load_classes(
        [
            class_file(names, "obj.names"),
            class_file(text, "classes.txt"),
        ]
    )

    assert classes == {0: "person", 1: "truck", 2: "helmet"}


def test_data_yaml_accepts_list_and_mapping_names(tmp_path: Path) -> None:
    list_yaml = tmp_path / "list.yaml"
    list_yaml.write_text("names: [person, car]\n", encoding="utf-8")
    mapping_yaml = tmp_path / "mapping.yaml"
    mapping_yaml.write_text(
        "names:\n  0: bicycle\n  3: bus\n",
        encoding="utf-8",
    )

    assert load_classes([class_file(list_yaml, "data.yaml")]) == {
        0: "person",
        1: "car",
    }
    assert load_classes([class_file(mapping_yaml, "data.yaml")]) == {
        0: "bicycle",
        3: "bus",
    }


def test_numeric_class_names_are_created_when_no_class_file_exists() -> None:
    assert classes_for_ids({5, 1, 5}) == {1: "1", 5: "5"}


def test_names_is_used_when_higher_priority_candidates_are_invalid(
    tmp_path: Path,
) -> None:
    yaml = tmp_path / "data.yaml"
    yaml.write_text("train: images/train\n", encoding="utf-8")
    text = tmp_path / "classes.txt"
    text.write_text("background:0,0,0::\nperson:128,0,0::\n", encoding="utf-8")
    names = tmp_path / "obj.names"
    names.write_text("person\nforklift\n", encoding="utf-8")

    classes = load_classes(
        [
            class_file(names, "obj.names"),
            class_file(text, "classes.txt"),
            class_file(yaml, "data.yaml"),
        ]
    )

    assert classes == {0: "person", 1: "forklift"}


def test_arbitrary_text_and_colon_names_are_not_class_sources(
    tmp_path: Path,
) -> None:
    arbitrary = tmp_path / "readme.txt"
    arbitrary.write_text("person\nforklift\n", encoding="utf-8")
    names = tmp_path / "obj.names"
    names.write_text("background:0,0,0::\n", encoding="utf-8")

    assert (
        load_classes(
            [
                class_file(arbitrary, "readme.txt"),
                class_file(names, "obj.names"),
            ]
        )
        == {}
    )
