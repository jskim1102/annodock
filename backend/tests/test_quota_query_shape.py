from __future__ import annotations

import unittest
from collections.abc import Iterable
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

from sqlalchemy.dialects import postgresql

from app.models import Image, MediaObject
from app.services.quota import (
    DatasetStorageReleasePlan,
    apply_dataset_storage_release,
    plan_dataset_storage_release,
)
from app.services.uploads import existing_upload_directories


class _ScalarRows:
    def __init__(self, rows: Iterable[Any]) -> None:
        self._rows = list(rows)

    def all(self) -> list[Any]:
        return self._rows


class _RowResult(_ScalarRows):
    pass


class _QueryShapeSession:
    """Return a huge target ID set without touching a database."""

    def __init__(self) -> None:
        self.captured_statements: list[Any] = []

    async def scalar(self, _statement: Any) -> int:
        return 0

    async def scalars(self, statement: Any) -> _ScalarRows:
        selected_entity = statement.column_descriptions[0].get("entity")
        if selected_entity is Image:
            return _ScalarRows(range(1, 40_001))
        if selected_entity is MediaObject:
            self.captured_statements.append(statement)
            return _ScalarRows(())
        raise AssertionError(f"unexpected scalar statement: {statement}")

    async def execute(self, statement: Any) -> _RowResult:
        self.captured_statements.append(statement)
        return _RowResult(())


class _ApplySession:
    def __init__(self) -> None:
        self.delete_calls = 0
        self.captured_statements: list[Any] = []

    async def delete(self, _row: Any) -> None:
        self.delete_calls += 1

    async def execute(self, statement: Any) -> _RowResult:
        self.captured_statements.append(statement)
        return _RowResult(())


class StorageReleaseQueryShapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_media_set_never_expands_into_driver_parameters(
        self,
    ) -> None:
        session = _QueryShapeSession()

        await plan_dataset_storage_release(
            session,  # type: ignore[arg-type]
            [11, 12],
        )

        self.assertTrue(session.captured_statements)
        for statement in session.captured_statements:
            compiled = statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"render_postcompile": True},
            )
            self.assertLess(
                len(compiled.params),
                32_767,
                "large media-object IDs must stay inside a database subquery",
            )

    async def test_large_upload_id_set_scans_only_existing_directories(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            storage_dir = Path(temporary)
            upload_root = storage_dir / "uploads"
            (upload_root / "7").mkdir(parents=True)
            (upload_root / "40000").mkdir()
            (upload_root / "unrelated").mkdir()

            paths = existing_upload_directories(
                SimpleNamespace(storage_dir=storage_dir),  # type: ignore[arg-type]
                range(1, 40_001),
            )

            self.assertEqual([path.name for path in paths], ["7", "40000"])

    async def test_large_orphan_set_uses_one_bulk_delete_statement(
        self,
    ) -> None:
        session = _ApplySession()
        plan = DatasetStorageReleasePlan(
            released_bytes=0,
            orphan_media_objects=tuple(
                SimpleNamespace(id=media_id)  # type: ignore[arg-type]
                for media_id in range(1, 40_001)
            ),
        )

        await apply_dataset_storage_release(
            session,  # type: ignore[arg-type]
            plan,
        )

        self.assertEqual(session.delete_calls, 0)
        self.assertEqual(len(session.captured_statements), 1)
        compiled = session.captured_statements[0].compile(
            dialect=postgresql.dialect(),
        )
        self.assertLessEqual(len(compiled.params), 1)


if __name__ == "__main__":
    unittest.main()
