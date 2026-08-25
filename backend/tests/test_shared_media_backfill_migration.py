from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0012_backfill_media_objects.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "annodock_shared_media_backfill_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.alterations: list[tuple[str, str, dict[str, object]]] = []

    def execute(self, statement: object) -> None:
        self.statements.append(" ".join(str(statement).split()).lower())

    def alter_column(
        self,
        table_name: str,
        column_name: str,
        **values: object,
    ) -> None:
        self.alterations.append((table_name, column_name, values))


def test_upgrade_backfills_every_legacy_image_from_its_dataset_owner(
    monkeypatch,
) -> None:
    migration = _migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    assert migration.down_revision == "0011_shared_media_objects"
    assert len(recorder.statements) == 4
    assert "where image.media_object_id is null" in recorder.statements[1]
    assert "join datasets as dataset on dataset.id = image.dataset_id" in (
        recorder.statements[2]
    )
    assert "dataset.owner_id" in recorder.statements[2]
    assert "set media_object_id = mapping.media_object_id" in recorder.statements[3]
    assert len(recorder.alterations) == 1
    table_name, column_name, values = recorder.alterations[0]
    assert (table_name, column_name) == ("images", "media_object_id")
    assert isinstance(values["existing_type"], migration.sa.Integer)
    assert values["nullable"] is False


def test_downgrade_preserves_backfilled_rows_and_only_relaxes_nullability(
    monkeypatch,
) -> None:
    migration = _migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    assert recorder.statements == []
    assert len(recorder.alterations) == 1
    table_name, column_name, values = recorder.alterations[0]
    assert (table_name, column_name) == ("images", "media_object_id")
    assert values["nullable"] is True
