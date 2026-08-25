from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from sqlalchemy import BigInteger, CheckConstraint, Integer, inspect
from sqlalchemy.exc import IntegrityError

from app.models import (
    Annotation,
    Dataset,
    ExportArtifact,
    Image,
    Membership,
    MediaObject,
    Organization,
    Project,
    Team,
    TrainingRun,
    UserStorage,
)


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0003_multiuser_ownership.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "annodock_multiuser_ownership_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _check_names(model: type[object]) -> set[str]:
    return {
        constraint.name or ""
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_owner_ids_match_auth_integer_subject_without_cross_database_fks() -> None:
    for model in (Dataset, MediaObject, TrainingRun, Organization):
        owner = model.__table__.c.owner_id
        assert isinstance(owner.type, Integer)
        assert owner.nullable is False
        assert not owner.foreign_keys

    user_id = Membership.__table__.c.user_id
    assert isinstance(user_id.type, Integer)
    assert user_id.nullable is False
    assert not user_id.foreign_keys
    assert UserStorage.__table__.c.owner_id.autoincrement is False


def test_accounting_columns_are_non_nullable_bigints() -> None:
    accounting_columns = (
        Image.__table__.c.original_bytes,
        Image.__table__.c.display_bytes,
        Image.__table__.c.thumb_bytes,
        MediaObject.__table__.c.original_bytes,
        MediaObject.__table__.c.display_bytes,
        MediaObject.__table__.c.thumb_bytes,
        Annotation.__table__.c.serialized_bytes,
        ExportArtifact.__table__.c.archive_bytes,
        UserStorage.__table__.c.bytes_used,
    )
    for column in accounting_columns:
        assert isinstance(column.type, BigInteger)
        assert column.nullable is False
        assert column.server_default is not None
        assert str(column.server_default.arg) == "0"

    assert "ck_images_bytes_nonnegative" in _check_names(Image)
    assert "ck_media_objects_bytes_nonnegative" in _check_names(MediaObject)
    assert "ck_annotations_serialized_bytes_nonnegative" in _check_names(Annotation)
    assert "ck_exports_archive_bytes_nonnegative" in _check_names(ExportArtifact)
    assert "ck_user_storage_bytes_used_nonnegative" in _check_names(UserStorage)
    quota_limit = UserStorage.__table__.c.quota_limit_bytes
    assert isinstance(quota_limit.type, BigInteger)
    assert quota_limit.nullable is True
    assert quota_limit.server_default is None
    assert "ck_user_storage_quota_limit_positive" in _check_names(UserStorage)
    assert "ck_training_runs_artifact_bytes_nonnegative" in _check_names(
        TrainingRun
    )


def test_organization_team_membership_skeleton_has_explicit_permissions() -> None:
    assert Team.__table__.c.org_id.foreign_keys
    assert Membership.__table__.c.team_id.foreign_keys
    assert tuple(
        column.name for column in Membership.__table__.primary_key.columns
    ) == ("team_id", "user_id")
    assert {
        "role",
        "can_view",
        "can_edit",
        "can_manage",
    } <= set(Membership.__table__.columns.keys())
    assert "ck_memberships_role" in _check_names(Membership)


@pytest.mark.asyncio
async def test_migrated_database_enforces_owner_scoped_dataset_names(app) -> None:
    name = f"test-owner-scope-{uuid4().hex}"
    async with app.state.session_factory() as session:
        owner_one_project = Project(
            owner_id=1,
            name=f"test-owner-one-project-{uuid4().hex}",
        )
        owner_two_project = Project(
            owner_id=2,
            name=f"test-owner-two-project-{uuid4().hex}",
        )
        session.add_all([owner_one_project, owner_two_project])
        await session.flush()
        session.add_all(
            [
                Dataset(
                    owner_id=1,
                    project_id=owner_one_project.id,
                    name=name,
                    status="pending",
                    storage_path="a",
                ),
                Dataset(
                    owner_id=2,
                    project_id=owner_two_project.id,
                    name=name,
                    status="pending",
                    storage_path="b",
                ),
            ]
        )
        await session.commit()

        session.add(
            Dataset(
                owner_id=1,
                project_id=owner_one_project.id,
                name=name,
                status="pending",
                storage_path="c",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


@pytest.mark.asyncio
async def test_migrated_database_contains_accounting_and_collaboration_schema(
    app,
) -> None:
    async with app.state.engine.connect() as connection:
        tables, dataset_constraints, run_indexes, image_columns = (
            await connection.run_sync(
                lambda sync_connection: (
                    set(inspect(sync_connection).get_table_names()),
                    {
                        constraint["name"]
                        for constraint in inspect(
                            sync_connection
                        ).get_unique_constraints("datasets")
                    },
                    {
                        index["name"]
                        for index in inspect(sync_connection).get_indexes(
                            "training_runs"
                        )
                    },
                    {
                        column["name"]: column
                        for column in inspect(sync_connection).get_columns(
                            "images"
                        )
                    },
                )
            )
        )

    assert {"exports", "user_storage", "orgs", "teams", "memberships"} <= tables
    assert "uq_datasets_owner_name" in dataset_constraints
    assert "uq_datasets_name" not in dataset_constraints
    assert "ix_training_runs_owner_id" in run_indexes
    assert image_columns["media_object_id"]["nullable"] is False


class _RecordingOp:
    def __init__(self) -> None:
        self.statements: list[tuple[str, dict[str, object]]] = []

    def execute(self, statement: object) -> None:
        compiled = statement.compile()
        self.statements.append((str(compiled), dict(compiled.params)))


def test_migration_backfills_runs_from_dataset_then_uses_stable_fallback(
    monkeypatch,
) -> None:
    migration = _migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration._backfill_owner_ids()

    assert migration.LEGACY_OWNER_ID == 1
    normalized = [" ".join(sql.split()).lower() for sql, _ in recorder.statements]
    assert len(normalized) == 3
    assert normalized[0].startswith("update datasets set owner_id")
    assert "from datasets as dataset" in normalized[1]
    assert "run.dataset_id = dataset.id" in normalized[1]
    assert normalized[2].startswith("update training_runs set owner_id")
    for _, params in recorder.statements:
        assert params["legacy_owner_id"] == 1
