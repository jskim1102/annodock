"""Exercise 0022 against a real 0021 schema, without migrating shared public."""

from __future__ import annotations

from collections.abc import Iterator
from itertools import product
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dotenv import dotenv_values
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

from tests.test_workspace_migration import _apply, _load_migration, _rows, _schema_snapshot


ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "backend" / "alembic" / "versions"
REVISION = "0022_image_label_status"
PREDECESSOR = "0021_workspace_ownership"
LABEL_COLUMNS = {"label_status", "assignee_id", "assigned_at", "labeled_by", "labeled_at", "version"}
LEGACY_CASES = tuple(product((False, True), (0, 3), (False, True)))


@pytest.fixture(scope="session", autouse=True)
def migrate_test_database() -> None:
    """Override conftest's upgrade-head: each case must start at 0021."""


@pytest.fixture(autouse=True)
def cleanup_test_datasets() -> None:
    """No application ORM cleanup may access the private predecessor schema."""


@pytest.fixture(autouse=True)
def legacy_personal_workspaces() -> None:
    """The migration cases seed their own workspace using predecessor SQL."""


@pytest.fixture
def migration() -> ModuleType:
    return _load_migration(VERSIONS / f"{REVISION}.py")


@pytest.fixture
def predecessor_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Connection]:
    # Match test_workspace_migration's rollback-only schema isolation. In
    # particular, no missing relation can fall through to public via search_path.
    assert ROOT.name in ("annodock", "annodock-ws"), "migration tests run only in an annodock tree"
    raw_url = dotenv_values(ROOT / ".env").get("TEST_DATABASE_URL")
    assert raw_url, "isolated TEST_DATABASE_URL is required"
    url = make_url(raw_url)
    assert url.database.endswith("_test"), "refusing any non-test database"
    assert url.get_backend_name() == "postgresql"
    engine = sa.create_engine(url.set(drivername="postgresql+psycopg"), poolclass=NullPool)
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "migration-storage"))
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                assert connection.scalar(sa.text("SELECT current_database()")).endswith("_test")
                schema = f"label_status_migration_{uuid4().hex}"
                connection.execute(sa.schema.CreateSchema(schema))
                connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
                previous = None
                for path in sorted(VERSIONS.glob("[0-9][0-9][0-9][0-9]_*.py")):
                    if int(path.name[:4]) > 21:
                        continue
                    step = _load_migration(path)
                    assert step.down_revision == previous
                    _apply(connection, step, "upgrade")
                    previous = step.revision
                assert previous == PREDECESSOR
                assert LABEL_COLUMNS.isdisjoint(_image_columns(connection))
                yield connection
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def _image_columns(connection: Connection) -> dict[str, dict]:
    schema = connection.scalar(sa.text("SELECT current_schema()"))
    return {column["name"]: column for column in sa.inspect(connection).get_columns("images", schema=schema)}


def _seed_images(connection: Connection, cases=LEGACY_CASES) -> list[dict]:
    workspace = connection.scalar(sa.text("""
        INSERT INTO workspaces (kind, name, created_by)
        VALUES ('personal', 'test-label-migration', 973001) RETURNING id
    """))
    project = connection.scalar(sa.text("""
        INSERT INTO projects (workspace_id, name)
        VALUES (:workspace, 'test-label-project') RETURNING id
    """), {"workspace": workspace})
    dataset = connection.scalar(sa.text("""
        INSERT INTO datasets (workspace_id, project_id, name, storage_path)
        VALUES (:workspace, :project, 'test-label-dataset', 'datasets/label-migration')
        RETURNING id
    """), {"workspace": workspace, "project": project})
    rows = []
    for index, (source, box_count, modified) in enumerate(cases):
        media = connection.scalar(sa.text("""
            INSERT INTO media_objects (workspace_id, created_by_dataset_id)
            VALUES (:workspace, :dataset) RETURNING id
        """), {"workspace": workspace, "dataset": dataset})
        image = connection.scalar(sa.text("""
            INSERT INTO images
                (dataset_id, media_object_id, stem, filename, rel_path,
                 training_source_key, training_group_key, width, height,
                 file_path, thumb_path, has_label_source, box_count, is_modified)
            VALUES (:dataset, :media, :stem, :filename, :path,
                    :source_key, :group_key, 32, 24, :path, :path,
                    :source, :boxes, :modified)
            RETURNING id
        """), {"dataset": dataset, "media": media, "stem": f"image-{index}",
                "filename": f"image-{index}.jpg", "path": f"image-{index}.jpg", "source_key": f"dataset:{dataset}",
                "group_key": f"media:{media}", "source": source, "boxes": box_count,
                "modified": modified})
        for _ in range(box_count):
            connection.execute(sa.text("""
                INSERT INTO annotations (image_id, class_id, cx, cy, w, h)
                VALUES (:image, 0, 0.5, 0.5, 0.25, 0.25)
            """), {"image": image})
        rows.append({"id": image, "source": source, "box_count": box_count, "modified": modified})
    return rows


def _label_rows(connection: Connection) -> list[tuple]:
    return _rows(connection, """
        SELECT id, label_status, assignee_id, assigned_at, labeled_by, labeled_at,
               version, is_modified FROM images ORDER BY id
    """)


def _expected_backfill(seeded: list[dict]) -> list[tuple]:
    return [(row["id"], "labeled" if row["source"] or row["box_count"] > 0 else "unlabeled",
             None, None, None, None, 0, row["modified"]) for row in seeded]


def test_label_status_revision_follows_workspace_ownership(migration: ModuleType) -> None:
    assert migration.revision == REVISION
    assert migration.down_revision == PREDECESSOR


def test_backfill_uses_source_or_positive_boxes_without_inventing_actor_history(
    predecessor_db: Connection, migration: ModuleType,
) -> None:
    seeded = _seed_images(predecessor_db)
    old_columns = list(_image_columns(predecessor_db))
    old_rows = _rows(predecessor_db, "SELECT * FROM images ORDER BY id")
    old_annotations = _rows(predecessor_db, "SELECT * FROM annotations ORDER BY id")
    _apply(predecessor_db, migration, "upgrade")
    assert _label_rows(predecessor_db) == _expected_backfill(seeded)
    # Preserve every predecessor column (including flags/timestamps/provenance)
    # and the actual annotations, not just the number of images.
    assert _rows(predecessor_db, f"SELECT {', '.join(old_columns)} FROM images ORDER BY id") == old_rows
    assert _rows(predecessor_db, "SELECT * FROM annotations ORDER BY id") == old_annotations


def test_empty_image_table_upgrades_and_new_rows_receive_database_defaults(
    predecessor_db: Connection, migration: ModuleType,
) -> None:
    assert predecessor_db.scalar(sa.text("SELECT count(*) FROM images")) == 0
    _apply(predecessor_db, migration, "upgrade")
    assert predecessor_db.scalar(sa.text("SELECT count(*) FROM images")) == 0
    seeded = _seed_images(predecessor_db, cases=[(False, 0, False)])
    assert _label_rows(predecessor_db) == _expected_backfill(seeded)


def test_added_columns_are_nullable_actor_history_and_varchar_status_with_index(
    predecessor_db: Connection, migration: ModuleType,
) -> None:
    _apply(predecessor_db, migration, "upgrade")
    columns = _image_columns(predecessor_db)
    status = columns["label_status"]
    assert isinstance(status["type"], sa.String) and status["type"].length == 16
    assert not isinstance(status["type"], sa.Enum), "workflow statuses must not use a PostgreSQL enum"
    assert status["nullable"] is False and "unlabeled" in status["default"]
    for name in ("assignee_id", "labeled_by", "version"):
        assert isinstance(columns[name]["type"], sa.Integer)
        assert columns[name]["nullable"] is (name != "version")
    assert columns["version"]["default"] == "0"
    for name in ("assigned_at", "labeled_at"):
        assert isinstance(columns[name]["type"], sa.DateTime)
        assert columns[name]["type"].timezone is True
        assert columns[name]["nullable"] is True
    schema = predecessor_db.scalar(sa.text("SELECT current_schema()"))
    indexes = sa.inspect(predecessor_db).get_indexes("images", schema=schema)
    assert any(index["column_names"] == ["dataset_id", "label_status", "assignee_id"]
               and not index["unique"] for index in indexes)


@pytest.mark.parametrize("status,assignee", [("unlabeled", None), ("labeled", None), ("in_progress", 973001)])
def test_check_constraints_allow_documented_label_states(
    predecessor_db: Connection, migration: ModuleType, status: str, assignee: int | None,
) -> None:
    image = _seed_images(predecessor_db, cases=[(False, 0, False)])[0]["id"]
    _apply(predecessor_db, migration, "upgrade")
    predecessor_db.execute(sa.text("""
        UPDATE images SET label_status = :status, assignee_id = :assignee WHERE id = :image
    """), {"status": status, "assignee": assignee, "image": image})
    assert _rows(predecessor_db, "SELECT label_status, assignee_id FROM images") == [(status, assignee)]


@pytest.mark.parametrize("status,assignee,sqlstate", [
    ("reviewed", None, "23514"), ("in_progress", None, "23514"), (None, None, "23502"),
])
def test_invalid_status_or_unassigned_in_progress_is_rejected_atomically(
    predecessor_db: Connection, migration: ModuleType, status: str | None,
    assignee: int | None, sqlstate: str,
) -> None:
    seeded = _seed_images(predecessor_db, cases=[(False, 0, True)])
    _apply(predecessor_db, migration, "upgrade")
    with pytest.raises(IntegrityError) as error:
        with predecessor_db.begin_nested():
            predecessor_db.execute(sa.text("""
                UPDATE images SET label_status = :status, assignee_id = :assignee WHERE id = :image
            """), {"status": status, "assignee": assignee, "image": seeded[0]["id"]})
    assert error.value.orig.sqlstate == sqlstate
    assert _label_rows(predecessor_db) == _expected_backfill(seeded)


def test_downgrade_then_upgrade_preserves_legacy_data_and_rebuilds_label_backfill(
    predecessor_db: Connection, migration: ModuleType,
) -> None:
    seeded = _seed_images(predecessor_db)
    before_schema = _schema_snapshot(predecessor_db)
    before_images = _rows(predecessor_db, "SELECT * FROM images ORDER BY id")
    before_annotations = _rows(predecessor_db, "SELECT * FROM annotations ORDER BY id")
    _apply(predecessor_db, migration, "upgrade")
    predecessor_db.execute(sa.text("""
        UPDATE images SET label_status = 'in_progress', assignee_id = 973001,
                          assigned_at = now(), version = 7 WHERE id = :image
    """), {"image": seeded[0]["id"]})
    predecessor_db.execute(sa.text("""
        UPDATE images SET label_status = 'labeled', labeled_by = 973001,
                          labeled_at = now(), version = 3 WHERE id = :image
    """), {"image": seeded[-1]["id"]})
    _apply(predecessor_db, migration, "downgrade")
    assert LABEL_COLUMNS.isdisjoint(_image_columns(predecessor_db))
    assert _schema_snapshot(predecessor_db) == before_schema
    assert _rows(predecessor_db, "SELECT * FROM images ORDER BY id") == before_images
    assert _rows(predecessor_db, "SELECT * FROM annotations ORDER BY id") == before_annotations
    _apply(predecessor_db, migration, "upgrade")
    assert _label_rows(predecessor_db) == _expected_backfill(seeded)
    assert _rows(predecessor_db, "SELECT * FROM annotations ORDER BY id") == before_annotations
