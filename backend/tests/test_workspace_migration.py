from __future__ import annotations

import importlib.util
import re
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from dotenv import dotenv_values
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool


ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "backend" / "alembic" / "versions"
REVISION = "0021_workspace_ownership"
OWNED_TABLES = ("projects", "datasets", "media_objects", "training_runs")


@pytest.fixture(scope="session", autouse=True)
def migrate_test_database() -> None:
    """Override the shared fixture: this module never migrates public."""


@pytest.fixture(autouse=True)
def cleanup_test_datasets() -> None:
    """Override old-ORM cleanup; each test rolls back its entire schema."""


@pytest.fixture(autouse=True)
def legacy_personal_workspaces() -> None:
    """Migration cases own their schema and do not need application seeds."""


def _load_migration(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"test_migration_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _apply(connection: Connection, migration: ModuleType, direction: str) -> None:
    migration.op = Operations(MigrationContext.configure(connection))
    getattr(migration, direction)()


@pytest.fixture
def migration() -> ModuleType:
    return _load_migration(VERSIONS / f"{REVISION}.py")


@pytest.fixture
def legacy_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Connection]:
    # The old application imports conftest first, but neither its default
    # database nor its ORM fixtures are used by these migration tests.
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
                schema = f"workspace_migration_{uuid4().hex}"
                connection.execute(sa.schema.CreateSchema(schema))
                # Deliberately omit public: a missing migration table must
                # fail rather than resolving to the shared test schema.
                connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
                previous = None
                for path in sorted(VERSIONS.glob("[0-9][0-9][0-9][0-9]_*.py")):
                    if int(path.name[:4]) > 20:
                        continue
                    step = _load_migration(path)
                    assert step.down_revision == previous
                    _apply(connection, step, "upgrade")
                    previous = step.revision
                assert previous == "0020_upload_size_bigint"
                yield connection
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def _rows(connection: Connection, statement: str, **values) -> list[tuple]:
    return [tuple(row) for row in connection.execute(sa.text(statement), values)]


def _schema_snapshot(connection: Connection) -> dict[str, list[tuple]]:
    return {
        "columns": _rows(connection, """
            SELECT table_name, column_name, data_type, is_nullable,
                   column_default, character_maximum_length
            FROM information_schema.columns
            WHERE table_schema = current_schema()
            ORDER BY table_name, ordinal_position
        """),
        "constraints": _rows(connection, """
            SELECT rel.relname, con.conname, con.contype,
                   pg_get_constraintdef(con.oid)
            FROM pg_constraint con
            JOIN pg_class rel ON rel.oid = con.conrelid
            JOIN pg_namespace ns ON ns.oid = rel.relnamespace
            WHERE ns.nspname = current_schema()
            ORDER BY rel.relname, con.conname
        """),
        "indexes": _rows(connection, """
            SELECT tablename, indexname, indexdef
            FROM pg_indexes WHERE schemaname = current_schema()
            ORDER BY tablename, indexname
        """),
    }


def _seed_run(connection: Connection, owner_id: int, *, column: str = "owner_id",
              created_by: int | None = None) -> int:
    actor_column = ", created_by" if column == "workspace_id" else ""
    actor_value = ", :created_by" if column == "workspace_id" else ""
    return connection.scalar(sa.text(f"""
        INSERT INTO training_runs
            ({column}, dataset_name, weights, epochs, imgsz, batch,
             split_mode, ratios, seed, state, out_dir{actor_column})
        VALUES (:owner, 'test-migration-run', 'model.pt', 2, 64, 1,
                '2way', '{{"train": 0.8, "valid": 0.2}}', 7, 'done',
                'training-runs/migration'{actor_value})
        RETURNING id
    """), {"owner": owner_id, "created_by": created_by})


def _seed_resources(connection: Connection, owner_id: int, *, column: str = "owner_id",
                    created_by: int | None = None) -> dict[str, int]:
    project_id = connection.scalar(sa.text(f"""
        INSERT INTO projects ({column}, name)
        VALUES (:owner, 'test-shared-project-name') RETURNING id
    """), {"owner": owner_id})
    dataset_id = connection.scalar(sa.text(f"""
        INSERT INTO datasets ({column}, project_id, name, storage_path)
        VALUES (:owner, :project, 'test-shared-dataset-name', 'datasets/migration')
        RETURNING id
    """), {"owner": owner_id, "project": project_id})
    media_id = connection.scalar(sa.text(f"""
        INSERT INTO media_objects
            ({column}, created_by_dataset_id, original_bytes, display_bytes, thumb_bytes)
        VALUES (:owner, :dataset, 100, 20, 5) RETURNING id
    """), {"owner": owner_id, "dataset": dataset_id})
    run_id = _seed_run(connection, owner_id, column=column, created_by=created_by)
    return dict(projects=project_id, datasets=dataset_id, media_objects=media_id,
                training_runs=run_id)


def _seed_legacy_accounts(connection: Connection) -> dict[str, int]:
    resources = _seed_resources(connection, 11)
    _seed_run(connection, 27)
    connection.execute(sa.text("""
        UPDATE user_storage SET bytes_used = 77 WHERE owner_id = 1
    """))
    connection.execute(sa.text("""
        INSERT INTO user_storage (owner_id, bytes_used, quota_limit_bytes)
        VALUES (11, 125, 5000), (42, 4096, 8192)
    """))
    # An admin grant is a user reference, not an ownership source.
    connection.execute(sa.text("INSERT INTO admin_users (owner_id) VALUES (999)"))
    return resources


def _data_snapshot(connection: Connection) -> dict[str, list[tuple]]:
    tables = (*OWNED_TABLES, "user_storage", "admin_users")
    return {table: _rows(connection, f"SELECT * FROM {table} ORDER BY 1")
            for table in tables}


def test_workspace_revision_follows_upload_size_bigint(migration: ModuleType) -> None:
    assert migration.revision == REVISION
    assert migration.down_revision == "0020_upload_size_bigint"


def test_upgrade_backfills_owners_members_accounting_and_actor(
    legacy_db: Connection, migration: ModuleType,
) -> None:
    _seed_legacy_accounts(legacy_db)
    old_storage = _rows(legacy_db, "SELECT owner_id, bytes_used, quota_limit_bytes, updated_at "
                        "FROM user_storage ORDER BY owner_id")
    old_admin = _rows(legacy_db, "SELECT owner_id, granted_at FROM admin_users")
    _apply(legacy_db, migration, "upgrade")

    assert _rows(legacy_db, "SELECT id, kind, name, created_by FROM workspaces ORDER BY id") == [
        (owner, "personal", "개인", owner) for owner in (1, 11, 27, 42)
    ]
    assert _rows(legacy_db, "SELECT workspace_id, user_id, role FROM workspace_members "
                 "ORDER BY workspace_id") == [(owner, owner, "owner") for owner in (1, 11, 27, 42)]
    assert _rows(legacy_db, "SELECT workspace_id, bytes_used, quota_limit_bytes, updated_at "
                 "FROM workspace_storage ORDER BY workspace_id") == old_storage
    assert _rows(legacy_db, "SELECT user_id, granted_at FROM admin_users") == old_admin
    assert _rows(legacy_db, "SELECT workspace_id, created_by FROM training_runs ORDER BY id") == [
        (11, 11), (27, 27),
    ]
    for table in ("projects", "datasets", "media_objects"):
        assert _rows(legacy_db, f"SELECT workspace_id FROM {table}") == [(11,)]
    assert legacy_db.scalar(sa.text(
        "SELECT nextval(pg_get_serial_sequence('workspaces', 'id'))"
    )) > 42
    names = set(sa.inspect(legacy_db).get_table_names(schema=legacy_db.scalar(sa.text("SELECT current_schema()"))))
    assert {"orgs", "teams", "memberships", "user_storage"}.isdisjoint(names)
    assert {"workspaces", "workspace_members", "workspace_invites", "workspace_storage"} <= names


@pytest.mark.parametrize("source", [*OWNED_TABLES, "user_storage"])
def test_every_legacy_owner_source_receives_a_personal_workspace(
    legacy_db: Connection, migration: ModuleType, source: str,
) -> None:
    owner = 87
    if source == "projects":
        legacy_db.execute(sa.text("INSERT INTO projects (owner_id, name) VALUES (87, 'only-project')"))
    elif source == "datasets":
        project = legacy_db.scalar(sa.text(
            "INSERT INTO projects (owner_id, name) VALUES (1, 'legacy-project') RETURNING id"
        ))
        # Old schemas permit a dataset owner absent from projects. The
        # migration must account for the stored row instead of inferring it.
        legacy_db.execute(sa.text("""
            INSERT INTO datasets (owner_id, project_id, name, storage_path)
            VALUES (87, :project, 'only-dataset', 'datasets/only')
        """), {"project": project})
    elif source == "media_objects":
        legacy_db.execute(sa.text("INSERT INTO media_objects (owner_id) VALUES (87)"))
    elif source == "training_runs":
        _seed_run(legacy_db, owner)
    else:
        legacy_db.execute(sa.text("INSERT INTO user_storage (owner_id, bytes_used) VALUES (87, 99)"))
    _apply(legacy_db, migration, "upgrade")
    assert _rows(legacy_db, "SELECT id, created_by FROM workspaces ORDER BY id") == [(1, 1), (owner, owner)]
    assert _rows(legacy_db, "SELECT workspace_id, user_id, role FROM workspace_members "
                 "ORDER BY workspace_id") == [(1, 1, "owner"), (owner, owner, "owner")]


def test_empty_database_keeps_first_workspace_sequence_value(
    legacy_db: Connection, migration: ModuleType,
) -> None:
    # 0003 always creates a bootstrap storage row; remove it to exercise
    # the genuinely empty union rather than the bootstrap-only branch.
    legacy_db.execute(sa.text("DELETE FROM user_storage"))
    _apply(legacy_db, migration, "upgrade")
    assert legacy_db.scalar(sa.text("SELECT count(*) FROM workspaces")) == 0
    assert legacy_db.scalar(sa.text("SELECT count(*) FROM workspace_members")) == 0
    assert legacy_db.scalar(sa.text(
        "SELECT nextval(pg_get_serial_sequence('workspaces', 'id'))"
    )) == 1


@pytest.mark.parametrize("populated_through", ["orgs", "teams", "memberships"])
def test_upgrade_refuses_existing_collaboration_rows_without_schema_changes(
    legacy_db: Connection, migration: ModuleType, populated_through: str,
) -> None:
    org = legacy_db.scalar(sa.text(
        "INSERT INTO orgs (owner_id, name) VALUES (11, 'legacy-org') RETURNING id"
    ))
    if populated_through != "orgs":
        team = legacy_db.scalar(sa.text(
            "INSERT INTO teams (org_id, name) VALUES (:org, 'legacy-team') RETURNING id"
        ), {"org": org})
        if populated_through == "memberships":
            legacy_db.execute(sa.text(
                "INSERT INTO memberships (team_id, user_id, role) VALUES (:team, 11, 'owner')"
            ), {"team": team})
    counts = {table: legacy_db.scalar(sa.text(f"SELECT count(*) FROM {table}"))
              for table in ("orgs", "teams", "memberships")}
    before = _schema_snapshot(legacy_db)
    with pytest.raises(RuntimeError) as error:
        with legacy_db.begin_nested():
            _apply(legacy_db, migration, "upgrade")
    for table, count in counts.items():
        assert re.search(rf"{table}\D+{count}\b", str(error.value)), str(error.value)
        assert legacy_db.scalar(sa.text(f"SELECT count(*) FROM {table}")) == count
    assert _schema_snapshot(legacy_db) == before


def test_upgrade_installs_named_ownership_keys_and_workspace_contracts(
    legacy_db: Connection, migration: ModuleType,
) -> None:
    _seed_legacy_accounts(legacy_db)
    _apply(legacy_db, migration, "upgrade")
    inspector = sa.inspect(legacy_db)
    for table in OWNED_TABLES:
        columns = {column["name"]: column for column in inspector.get_columns(table)}
        assert "owner_id" not in columns
        assert columns["workspace_id"]["nullable"] is False
        foreign_keys = [key for key in inspector.get_foreign_keys(table)
                        if key["constrained_columns"] == ["workspace_id"]]
        assert len(foreign_keys) == 1
        assert foreign_keys[0]["referred_table"] == "workspaces"
        assert foreign_keys[0]["referred_columns"] == ["id"]
        assert foreign_keys[0]["options"].get("ondelete") == "RESTRICT"
    for table in ("projects", "datasets"):
        uniques = {key["name"]: key["column_names"] for key in inspector.get_unique_constraints(table)}
        assert uniques[f"uq_{table}_workspace_name"] == ["workspace_id", "name"]
        assert f"uq_{table}_owner_name" not in uniques
    for table in ("media_objects", "training_runs"):
        indexes = {index["name"]: index["column_names"] for index in inspector.get_indexes(table)}
        assert indexes[f"ix_{table}_workspace_id"] == ["workspace_id"]
        assert f"ix_{table}_owner_id" not in indexes
    assert {index["name"] for index in inspector.get_indexes("training_runs")} >= {"uq_single_active_run"}
    assert next(column for column in inspector.get_columns("training_runs")
                if column["name"] == "created_by")["nullable"] is False
    assert inspector.get_pk_constraint("workspace_storage")["constrained_columns"] == ["workspace_id"]
    assert {check["name"] for check in inspector.get_check_constraints("workspace_storage")} == {
        "ck_workspace_storage_bytes_used_nonnegative", "ck_workspace_storage_quota_limit_positive",
    }
    assert inspector.get_pk_constraint("workspace_members")["constrained_columns"] == ["workspace_id", "user_id"]
    assert any(index["column_names"] == ["user_id"] for index in inspector.get_indexes("workspace_members"))
    for table in ("workspace_members", "workspace_invites"):
        foreign_keys = inspector.get_foreign_keys(table)
        assert any(key["constrained_columns"] == ["workspace_id"]
                   and key["referred_table"] == "workspaces"
                   and key["options"].get("ondelete") == "CASCADE" for key in foreign_keys)
    invite_columns = {column["name"]: column for column in inspector.get_columns("workspace_invites")}
    assert not any("email" in name for name in invite_columns)
    assert invite_columns["token_hash"]["type"].length == 64
    assert not invite_columns["expires_at"]["nullable"]
    assert inspector.get_pk_constraint("admin_users")["constrained_columns"] == ["user_id"]
    assert not inspector.get_foreign_keys("admin_users")


def test_workspace_uniqueness_roles_invites_and_accounting_are_enforced(
    legacy_db: Connection, migration: ModuleType,
) -> None:
    _seed_legacy_accounts(legacy_db)
    _apply(legacy_db, migration, "upgrade")
    legacy_db.execute(sa.text("""
        INSERT INTO workspaces (id, kind, name, created_by)
        VALUES (700, 'team', 'team-one', 1), (701, 'team', 'team-two', 1)
    """))
    legacy_db.execute(sa.text("""
        INSERT INTO workspace_members (workspace_id, user_id, role)
        VALUES (700, 1, 'owner'), (700, 2, 'admin'), (700, 3, 'editor'), (700, 4, 'viewer')
    """))
    legacy_db.execute(sa.text("""
        INSERT INTO workspace_invites (workspace_id, role, token_hash, created_by, expires_at)
        VALUES (700, 'admin', :token, 1, now() + interval '7 days')
    """), {"token": "a" * 64})
    assert _rows(legacy_db, "SELECT uses, max_uses, revoked_at FROM workspace_invites") == [(0, None, None)]
    invalid_statements = [
        "INSERT INTO workspaces (kind, name, created_by) VALUES ('personal', 'duplicate', 1)",
        "INSERT INTO workspaces (kind, name, created_by) VALUES ('invalid', 'invalid', 2)",
        "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (700, 5, 'owner')",
        "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (700, 1, 'admin')",
        "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (700, 5, 'invalid')",
        "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (999999, 1, 'owner')",
        "UPDATE workspace_invites SET role = 'owner'",
        "UPDATE workspace_invites SET uses = -1",
        "UPDATE workspace_storage SET bytes_used = -1 WHERE workspace_id = 11",
        "UPDATE workspace_storage SET quota_limit_bytes = 0 WHERE workspace_id = 11",
        "DELETE FROM workspaces WHERE id = 11",
    ]
    for statement in invalid_statements:
        with pytest.raises(IntegrityError):
            with legacy_db.begin_nested():
                legacy_db.execute(sa.text(statement))
    with pytest.raises(IntegrityError):
        with legacy_db.begin_nested():
            legacy_db.execute(sa.text("""
                INSERT INTO workspace_invites (workspace_id, role, token_hash, created_by, expires_at)
                VALUES (701, 'viewer', :token, 1, now() + interval '7 days')
            """), {"token": "a" * 64})
    legacy_db.execute(sa.text("DELETE FROM workspaces WHERE id = 700"))
    assert legacy_db.scalar(sa.text("SELECT count(*) FROM workspace_members WHERE workspace_id = 700")) == 0
    assert legacy_db.scalar(sa.text("SELECT count(*) FROM workspace_invites WHERE workspace_id = 700")) == 0


def test_downgrade_round_trip_restores_legacy_rows_and_schema(
    legacy_db: Connection, migration: ModuleType,
) -> None:
    _seed_legacy_accounts(legacy_db)
    before_schema = _schema_snapshot(legacy_db)
    before_rows = _data_snapshot(legacy_db)
    _apply(legacy_db, migration, "upgrade")
    _apply(legacy_db, migration, "downgrade")
    assert _data_snapshot(legacy_db) == before_rows
    assert _schema_snapshot(legacy_db) == before_schema
    for table in ("orgs", "teams", "memberships"):
        assert legacy_db.scalar(sa.text(f"SELECT count(*) FROM {table}")) == 0


def test_downgrade_remaps_late_personal_ids_including_crossing_user_ids(
    legacy_db: Connection, migration: ModuleType,
) -> None:
    _apply(legacy_db, migration, "upgrade")
    resources = {}
    for workspace, user in ((901, 902), (902, 901)):
        legacy_db.execute(sa.text("""
            INSERT INTO workspaces (id, kind, name, created_by)
            VALUES (:workspace, 'personal', '개인', :user)
        """), {"workspace": workspace, "user": user})
        legacy_db.execute(sa.text("""
            INSERT INTO workspace_members (workspace_id, user_id, role)
            VALUES (:workspace, :user, 'owner')
        """), {"workspace": workspace, "user": user})
        resources[user] = _seed_resources(legacy_db, workspace, column="workspace_id", created_by=user)
        legacy_db.execute(sa.text("""
            INSERT INTO workspace_storage (workspace_id, bytes_used, quota_limit_bytes)
            VALUES (:workspace, :bytes, :quota)
        """), {"workspace": workspace, "bytes": user * 10, "quota": user * 100})
        legacy_db.execute(sa.text("INSERT INTO admin_users (user_id) VALUES (:user)"), {"user": user})

    _apply(legacy_db, migration, "downgrade")

    for user, row_ids in resources.items():
        for table, row_id in row_ids.items():
            assert legacy_db.scalar(sa.text(f"SELECT owner_id FROM {table} WHERE id = :id"), {"id": row_id}) == user
        assert _rows(legacy_db, "SELECT bytes_used, quota_limit_bytes FROM user_storage "
                     "WHERE owner_id = :owner", owner=user) == [(user * 10, user * 100)]
    assert _rows(legacy_db, "SELECT owner_id FROM admin_users ORDER BY owner_id") == [(901,), (902,)]
    assert "created_by" not in {column["name"] for column in sa.inspect(legacy_db).get_columns("training_runs")}


def test_downgrade_refuses_team_workspace_without_schema_changes(
    legacy_db: Connection, migration: ModuleType,
) -> None:
    _apply(legacy_db, migration, "upgrade")
    legacy_db.execute(sa.text("""
        INSERT INTO workspaces (kind, name, created_by) VALUES ('team', 'must-preserve', 1)
    """))
    before = _schema_snapshot(legacy_db)
    workspaces = _rows(legacy_db, "SELECT * FROM workspaces ORDER BY id")
    with pytest.raises(RuntimeError):
        with legacy_db.begin_nested():
            _apply(legacy_db, migration, "downgrade")
    assert _schema_snapshot(legacy_db) == before
    assert _rows(legacy_db, "SELECT * FROM workspaces ORDER BY id") == workspaces
