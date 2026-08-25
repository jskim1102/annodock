from sqlalchemy import UniqueConstraint

from app.db import Base
from app.models import (
    Annotation,
    Dataset,
    DatasetClass,
    DatasetMergeSource,
    ExportArtifact,
    Image,
    ImportIssue,
    Membership,
    MediaObject,
    Organization,
    Project,
    ProjectClass,
    RunImage,
    RunMetric,
    Team,
    TrainingRun,
    UploadJob,
    UploadSession,
    UserStorage,
)


EXPECTED_TABLES = {
    "projects",
    "project_classes",
    "datasets",
    "dataset_classes",
    "dataset_merge_sources",
    "images",
    "media_objects",
    "annotations",
    "upload_sessions",
    "upload_jobs",
    "import_issues",
    "exports",
    "user_storage",
    "admin_users",
    "orgs",
    "teams",
    "memberships",
}

TRAINER_TABLES = {
    "training_runs",
    "run_images",
    "run_metrics",
}

EXPECTED_COLUMNS = {
    Dataset: {
        "id",
        "owner_id",
        "project_id",
        "name",
        "status",
        "storage_path",
        "image_count",
        "annotation_count",
        "class_count",
        "is_merged",
        "is_extracted",
        "is_placeholder",
        "upload_group_id",
        "upload_part_index",
        "upload_part_count",
        "created_at",
    },
    Project: {
        "id",
        "owner_id",
        "name",
        "archived_at",
        "created_at",
        "updated_at",
    },
    ProjectClass: {"project_id", "class_id", "name", "color"},
    DatasetClass: {"dataset_id", "class_id", "name"},
    DatasetMergeSource: {
        "merged_dataset_id",
        "source_dataset_id",
        "position",
    },
    Image: {
        "id",
        "dataset_id",
        "media_object_id",
        "stem",
        "filename",
        "rel_path",
        "split",
        "width",
        "height",
        "file_path",
        "display_path",
        "thumb_path",
        "original_bytes",
        "display_bytes",
        "thumb_bytes",
        "box_count",
        "has_label_source",
        "is_modified",
        "created_at",
    },
    MediaObject: {
        "id",
        "owner_id",
        "created_by_dataset_id",
        "original_bytes",
        "display_bytes",
        "thumb_bytes",
        "created_at",
    },
    Annotation: {
        "id",
        "image_id",
        "class_id",
        "cx",
        "cy",
        "w",
        "h",
        "serialized_bytes",
        "created_at",
        "updated_at",
    },
    UploadSession: {
        "id",
        "dataset_id",
        "filename",
        "size",
        "chunk_size",
        "received_chunks",
        "kind",
        "state",
        "created_at",
    },
    UploadJob: {
        "id",
        "dataset_id",
        "kind",
        "state",
        "phase",
        "total",
        "processed",
        "failed",
        "ingest_cursor",
        "image_total",
        "image_processed",
        "upload_ids",
        "class_resolution_plan",
        "class_resolutions",
        "created_at",
        "updated_at",
    },
    ImportIssue: {"id", "job_id", "kind", "path", "detail"},
    ExportArtifact: {
        "job_id",
        "dataset_id",
        "archive_path",
        "archive_bytes",
        "created_at",
    },
    UserStorage: {
        "owner_id",
        "bytes_used",
        "quota_limit_bytes",
        "updated_at",
    },
    Organization: {"id", "owner_id", "name", "created_at"},
    Team: {"id", "org_id", "name", "created_at"},
    Membership: {
        "team_id",
        "user_id",
        "role",
        "can_view",
        "can_edit",
        "can_manage",
        "created_at",
    },
    TrainingRun: {
        "id",
        "owner_id",
        "dataset_id",
        "dataset_name",
        "weights",
        "epochs",
        "imgsz",
        "batch",
        "split_mode",
        "ratios",
        "seed",
        "training_args",
        "state",
        "pid",
        "pid_started_at",
        "boot_id",
        "started_at",
        "finished_at",
        "out_dir",
        "error",
        "artifacts_deleted_at",
        "artifact_bytes",
        "created_at",
    },
    RunImage: {
        "id",
        "run_id",
        "image_id",
        "split",
        "stem",
        "filename",
        "rel_path",
    },
    RunMetric: {
        "id",
        "run_id",
        "epoch",
        "box_loss",
        "cls_loss",
        "dfl_loss",
        "map50",
        "map5095",
        "lr",
    },
}


def test_metadata_contains_exactly_the_expected_tables() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES | TRAINER_TABLES


def test_initial_tables_contain_the_contract_columns() -> None:
    for model, expected_columns in EXPECTED_COLUMNS.items():
        assert set(model.__table__.columns.keys()) == expected_columns


def test_dataset_uses_precomputed_counts_and_owner_scoped_unique_names() -> None:
    columns = Dataset.__table__.columns
    assert {"image_count", "annotation_count", "class_count"} <= set(columns.keys())
    assert columns["name"].unique is not True

    unique_constraints = {
        tuple(column.name for column in constraint.columns)
        for constraint in Dataset.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("name",) not in unique_constraints
    assert ("owner_id", "name") in unique_constraints


def test_dataset_class_uses_the_required_composite_primary_key() -> None:
    primary_key = tuple(
        column.name for column in DatasetClass.__table__.primary_key.columns
    )
    assert primary_key == ("dataset_id", "class_id")


def test_merge_source_belongs_to_only_one_merged_dataset() -> None:
    table = DatasetMergeSource.__table__
    unique_constraints = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("source_dataset_id",) in unique_constraints


def test_image_supports_display_derivatives_and_split_navigation() -> None:
    table = Image.__table__
    assert table.c.display_path.nullable is True
    assert table.c.split.nullable is True
    assert table.c.media_object_id.nullable is False

    unique_constraints = {
        tuple(column.name for column in constraint.columns): constraint
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    indexes = {
        tuple(column.name for column in index.columns) for index in table.indexes
    }

    split_stem_unique = unique_constraints[
        ("dataset_id", "split", "stem")
    ]
    assert ("dataset_id", "stem") not in unique_constraints
    assert (
        split_stem_unique.dialect_options["postgresql"][
            "nulls_not_distinct"
        ]
        is True
    )
    assert ("dataset_id", "split", "stem") in indexes


def test_high_volume_child_tables_have_parent_indexes() -> None:
    annotation_indexes = {
        tuple(column.name for column in index.columns)
        for index in Annotation.__table__.indexes
    }
    issue_indexes = {
        tuple(column.name for column in index.columns)
        for index in ImportIssue.__table__.indexes
    }

    assert ("image_id",) in annotation_indexes
    assert ("job_id",) in issue_indexes


def test_schema_does_not_store_clamped_import_state() -> None:
    all_columns = {
        column.name
        for table in Base.metadata.tables.values()
        for column in table.columns
    }
    assert not {name for name in all_columns if "clamp" in name.lower()}
