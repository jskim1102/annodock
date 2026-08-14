"""Materialize an isolated Ultralytics work directory for one run."""

from __future__ import annotations

import os
import shutil
from errno import EXDEV
from collections import Counter
from pathlib import Path
from typing import Mapping, Sequence

import yaml

from app.services.split import AnnotationSnapshot, ImageSnapshot
from app.services.quota import path_tree_bytes


def _safe_segment(value: str, *, field: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or Path(value).name != value
    ):
        raise ValueError(f"unsafe {field}: {value!r}")
    return value


def _mkdir(path: Path, *, parents: bool = False) -> None:
    if path.is_symlink():
        raise ValueError(f"symbolic directory is not allowed: {path}")
    path.mkdir(parents=parents, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"real directory is required: {path}")


def _label_text(annotations: Sequence[AnnotationSnapshot]) -> str:
    return "".join(
        (
            f"{annotation.class_id} "
            f"{annotation.cx:.6f} "
            f"{annotation.cy:.6f} "
            f"{annotation.w:.6f} "
            f"{annotation.h:.6f}\n"
        )
        for annotation in annotations
    )


def _target_stems(
    images: Sequence[ImageSnapshot],
    counts: Counter[str],
) -> dict[int, str]:
    targets: dict[int, str] = {}
    used: set[str] = set()
    for image in images:
        stem = _safe_segment(image.stem, field="image stem")
        base = f"{stem}-{image.id}" if counts[image.stem] > 1 else stem
        target = base
        if target in used:
            target = f"{base}-{image.id}"
        used.add(target)
        targets[image.id] = target
    return targets


def _link_or_copy(source: Path, target: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"original image is missing: {source}")
    try:
        os.link(source, target)
    except OSError as error:
        if error.errno != EXDEV:
            raise
        shutil.copy2(source, target)


def build_run_directory(
    out_dir: str | Path,
    assignments: Mapping[str, Sequence[ImageSnapshot]],
    class_names: Mapping[int, str],
    *,
    split_mode: str,
) -> Path:
    """Create hardlinked images, regenerated labels, and data.yaml."""
    root = Path(out_dir)
    workdir = root / "workdir"
    images_root = workdir / "images"
    labels_root = workdir / "labels"
    artifacts = root / "artifacts"
    for directory, parents in (
        (root, True),
        (workdir, False),
        (images_root, False),
        (labels_root, False),
        (artifacts, False),
    ):
        _mkdir(directory, parents=parents)

    stem_counts = Counter(
        image.stem for images in assignments.values() for image in images
    )
    for split, images in assignments.items():
        if not images:
            continue
        split_name = _safe_segment(split, field="split")
        image_dir = images_root / split_name
        label_dir = labels_root / split_name
        _mkdir(image_dir)
        _mkdir(label_dir)
        target_stems = _target_stems(images, stem_counts)
        for image in images:
            filename = _safe_segment(image.filename, field="image filename")
            suffix = Path(filename).suffix
            target_stem = target_stems[image.id]
            _link_or_copy(image.file_path, image_dir / f"{target_stem}{suffix}")
            (label_dir / f"{target_stem}.txt").write_text(
                _label_text(image.annotations),
                encoding="utf-8",
            )

    payload: dict[str, object] = {
        "path": str(workdir.resolve()),
        "train": "images/train",
        "val": "images/valid",
    }
    if split_mode == "3way":
        payload["test"] = "images/test"
    payload["names"] = dict(sorted(class_names.items()))
    data_yaml = workdir / "data.yaml"
    data_yaml.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return data_yaml


def collect_run_artifacts(
    out_dir: str | Path,
    *,
    sources: Mapping[str, Path] | None = None,
) -> int:
    """Publish known trainer outputs and return their exact persisted bytes."""

    root = Path(out_dir)
    if root.is_symlink():
        raise ValueError(f"symbolic run directory is not allowed: {root}")
    artifacts = root / "artifacts"
    _mkdir(artifacts, parents=True)
    train_dir = root / "workdir" / "train"
    conventional = {
        "best.pt": train_dir / "weights" / "best.pt",
        "last.pt": train_dir / "weights" / "last.pt",
        "results.csv": train_dir / "results.csv",
    }
    candidates = {**conventional, **(sources or {})}
    for name, source in candidates.items():
        _safe_segment(name, field="artifact name")
        target = artifacts / name
        if source == target:
            continue
        if source.is_symlink():
            raise ValueError(f"symbolic artifact is not allowed: {source}")
        if source.is_file():
            os.replace(source, target)

    weights_dir = train_dir / "weights"
    if weights_dir.is_dir() and not weights_dir.is_symlink():
        for checkpoint in weights_dir.glob("epoch*.pt"):
            if checkpoint.is_symlink():
                raise ValueError(
                    f"symbolic artifact is not allowed: {checkpoint}"
                )
            if checkpoint.is_file():
                os.replace(checkpoint, artifacts / checkpoint.name)
    return path_tree_bytes(artifacts)
