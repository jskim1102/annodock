from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0002_storage_relpath.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "deeplabel_storage_relpath_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_allowlists_only_physical_storage_columns() -> None:
    migration = _migration()

    assert migration._COLUMNS == (
        ("datasets", "storage_path", False),
        ("images", "file_path", False),
        ("images", "display_path", True),
        ("images", "thumb_path", False),
        ("training_runs", "out_dir", False),
    )


def test_migration_normalizes_absolute_and_mixed_relative_values(
    tmp_path: Path,
) -> None:
    migration = _migration()
    root = (tmp_path / "storage").resolve()
    root.mkdir()

    absolute = root / "datasets" / "12" / "image.jpg"
    relative = "training-runs/4"

    assert migration._safe_relative(str(absolute), root) == (
        "datasets/12/image.jpg"
    )
    assert migration._safe_relative(relative, root) == relative
    assert migration._absolute_value(relative, root) == str(root / relative)


def test_migration_rejects_paths_outside_storage_root(tmp_path: Path) -> None:
    migration = _migration()
    root = (tmp_path / "storage").resolve()
    root.mkdir()

    with pytest.raises(ValueError, match="outside STORAGE_DIR"):
        migration._safe_relative(str(tmp_path / "outside.jpg"), root)
    with pytest.raises(ValueError):
        migration._safe_relative("../outside.jpg", root)
