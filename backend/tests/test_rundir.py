from __future__ import annotations

import os
from errno import EPERM, EXDEV
from pathlib import Path

import pytest
import yaml

from app.services.rundir import build_run_directory
from app.services.split import AnnotationSnapshot, ImageSnapshot


def _image(
    source: Path,
    *,
    image_id: int,
    stem: str,
    class_id: int | None = 0,
) -> ImageSnapshot:
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(f"source-{image_id}".encode())
    annotations = (
        (
            AnnotationSnapshot(
                class_id=class_id,
                cx=0.5,
                cy=0.4,
                w=0.3,
                h=0.2,
            ),
        )
        if class_id is not None
        else ()
    )
    return ImageSnapshot(
        id=image_id,
        stem=stem,
        filename=source.name,
        rel_path=f"incoming/{source.name}",
        file_path=source,
        annotations=annotations,
    )


def test_rundir_builds_hardlinks_labels_and_three_way_yaml(tmp_path: Path) -> None:
    source_root = tmp_path / "source images with spaces"
    first = _image(source_root / "first.jpg", image_id=11, stem="same")
    second = _image(source_root / "second.png", image_id=12, stem="same")
    background = _image(
        source_root / "background image.jpg",
        image_id=13,
        stem="background image",
        class_id=None,
    )
    out_dir = tmp_path / "run output with spaces"

    data_yaml = build_run_directory(
        out_dir,
        {"train": (first, second), "valid": (background,), "test": ()},
        {0: "forklift"},
        split_mode="3way",
    )

    workdir = out_dir / "workdir"
    assert data_yaml == workdir / "data.yaml"
    assert (out_dir / "artifacts").is_dir()
    assert not (workdir / "images" / "train").is_symlink()
    assert not (workdir / "labels" / "valid").is_symlink()
    assert not (workdir / "images" / "test").exists()
    assert not (workdir / "labels" / "test").exists()

    linked_first = workdir / "images" / "train" / "same-11.jpg"
    linked_second = workdir / "images" / "train" / "same-12.png"
    assert linked_first.stat().st_ino == first.file_path.stat().st_ino
    assert linked_second.stat().st_ino == second.file_path.stat().st_ino
    assert (
        workdir / "labels" / "train" / "same-11.txt"
    ).read_text() == "0 0.500000 0.400000 0.300000 0.200000\n"
    assert (
        workdir / "labels" / "valid" / "background image.txt"
    ).read_text() == ""

    payload = yaml.safe_load(data_yaml.read_text())
    assert payload == {
        "path": str(workdir.resolve()),
        "train": "images/train",
        "val": "images/valid",
        "test": "images/test",
        "names": {0: "forklift"},
    }


def test_rundir_falls_back_to_copy2_for_cross_device_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _image(
        tmp_path / "source" / "copy me.jpg",
        image_id=21,
        stem="copy me",
    )

    def fail_link(_source: Path, _target: Path) -> None:
        raise OSError(EXDEV, "simulated cross-device link failure")

    monkeypatch.setattr(os, "link", fail_link)
    out_dir = tmp_path / "copy fallback"
    data_yaml = build_run_directory(
        out_dir,
        {"train": (source,), "valid": ()},
        {0: "person"},
        split_mode="2way",
    )

    copied = out_dir / "workdir" / "images" / "train" / "copy me.jpg"
    assert copied.read_bytes() == source.file_path.read_bytes()
    assert copied.stat().st_ino != source.file_path.stat().st_ino
    assert "test:" not in data_yaml.read_text()


def test_rundir_does_not_hide_non_cross_device_link_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _image(tmp_path / "source" / "blocked.jpg", image_id=22, stem="blocked")

    def fail_link(_source: Path, _target: Path) -> None:
        raise OSError(EPERM, "simulated permission failure")

    monkeypatch.setattr(os, "link", fail_link)

    with pytest.raises(OSError, match="permission failure"):
        build_run_directory(
            tmp_path / "permission failure",
            {"train": (source,), "valid": ()},
            {0: "person"},
            split_mode="2way",
        )
