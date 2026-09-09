from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0018_dataset_merge_purpose.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "annodock_dataset_merge_purpose_migration",
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
        self.created_checks: list[tuple[str, str, str]] = []
        self.dropped_constraints: list[tuple[str, str, str | None]] = []
        self.dropped_columns: list[tuple[str, str]] = []

    def add_column(self, table_name: str, column: object) -> None:
        self.added_columns.append((table_name, column))

    def execute(self, statement: object) -> None:
        self.statements.append(" ".join(str(statement).split()).lower())

    def create_check_constraint(
        self,
        name: str,
        table_name: str,
        condition: str,
    ) -> None:
        self.created_checks.append((name, table_name, condition))

    def drop_constraint(
        self,
        name: str,
        table_name: str,
        *,
        type_: str | None = None,
    ) -> None:
        self.dropped_constraints.append((name, table_name, type_))

    def drop_column(self, table_name: str, column_name: str) -> None:
        self.dropped_columns.append((table_name, column_name))


def test_upgrade_distinguishes_trained_merges_from_other_merges(monkeypatch) -> None:
    migration = _migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    assert migration.down_revision == "0017_durable_upload_batches"
    assert len(recorder.added_columns) == 1
    table_name, column = recorder.added_columns[0]
    assert table_name == "datasets"
    assert column.name == "merge_purpose"
    assert column.nullable is True
    assert len(recorder.statements) == 1
    backfill = recorder.statements[0]
    assert "when exists" in backfill
    assert "from training_runs" in backfill
    assert "training_runs.dataset_id = datasets.id" in backfill
    assert "then 'train'" in backfill
    assert "else 'merge'" in backfill
    assert "where is_merged is true" in backfill
    assert recorder.created_checks == [
        (
            "ck_datasets_merge_purpose",
            "datasets",
            "merge_purpose IS NULL OR "
            "(is_merged AND merge_purpose IN ('merge', 'train'))",
        )
    ]


def test_downgrade_removes_only_the_merge_purpose_contract(monkeypatch) -> None:
    migration = _migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    assert recorder.dropped_constraints == [
        ("ck_datasets_merge_purpose", "datasets", "check")
    ]
    assert recorder.dropped_columns == [("datasets", "merge_purpose")]
