from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0019_training_split_provenance.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "annodock_training_split_provenance_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    def __init__(self) -> None:
        self.added_columns: list[tuple[str, object]] = []
        self.statements: list[str] = []
        self.altered_columns: list[tuple[str, str, bool | None]] = []
        self.dropped_columns: list[tuple[str, str]] = []

    def add_column(self, table_name: str, column: object) -> None:
        self.added_columns.append((table_name, column))

    def execute(self, statement: object) -> None:
        self.statements.append(" ".join(str(statement).split()).lower())

    def alter_column(
        self,
        table_name: str,
        column_name: str,
        *,
        existing_type: object,
        nullable: bool | None = None,
    ) -> None:
        del existing_type
        self.altered_columns.append((table_name, column_name, nullable))

    def drop_column(self, table_name: str, column_name: str) -> None:
        self.dropped_columns.append((table_name, column_name))


def test_upgrade_backfills_source_and_atomic_group_keys(monkeypatch) -> None:
    migration = _migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    assert migration.down_revision == "0018_dataset_merge_purpose"
    assert [
        (table, column.name, column.nullable)
        for table, column in recorder.added_columns
    ] == [
        ("images", "training_source_key", True),
        ("images", "training_group_key", True),
    ]
    combined_sql = " ".join(recorder.statements)
    assert "source_dataset.upload_group_id" in combined_sql
    assert "dataset_merge_sources" in combined_sql
    assert "source_image.media_object_id = target.media_object_id" in combined_sql
    assert "sequence:" in combined_sql
    assert "media:" in combined_sql
    assert recorder.altered_columns == [
        ("images", "training_source_key", False),
        ("images", "training_group_key", False),
    ]


def test_downgrade_removes_only_training_split_provenance(monkeypatch) -> None:
    migration = _migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    assert recorder.dropped_columns == [
        ("images", "training_group_key"),
        ("images", "training_source_key"),
    ]
