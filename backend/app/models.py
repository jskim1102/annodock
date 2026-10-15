"""SQLAlchemy models for dataset import, review, and export state."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CHAR,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint("kind IN ('personal','team')", name="ck_workspaces_kind"),
        Index("uq_workspaces_personal_creator", "created_by", unique=True,
              postgresql_where=text("kind = 'personal'")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    __table_args__ = (
        CheckConstraint("role IN ('owner','admin','editor','viewer')",
                        name="ck_workspace_members_role"),
        Index("ix_workspace_members_user_id", "user_id"),
        Index("uq_workspace_members_owner", "workspace_id", unique=True,
              postgresql_where=text("role = 'owner'")),
    )

    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkspaceInvite(Base):
    __tablename__ = "workspace_invites"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_workspace_invites_token_hash"),
        CheckConstraint("role IN ('admin','editor','viewer')", name="ck_workspace_invites_role"),
        CheckConstraint("uses >= 0", name="ck_workspace_invites_uses_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    token_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_by: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uses: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_projects_workspace_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    classes: Mapped[list[ProjectClass]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    datasets: Mapped[list[Dataset]] = relationship(back_populates="project")


class ProjectClass(Base):
    __tablename__ = "project_classes"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "name",
            name="uq_project_classes_project_name",
        ),
    )

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    class_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    color: Mapped[str] = mapped_column(String(7), nullable=False)

    project: Mapped[Project] = relationship(back_populates="classes")


class Dataset(Base):
    __tablename__ = "datasets"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_datasets_workspace_name"),
        CheckConstraint(
            "(upload_group_id IS NULL AND upload_part_index IS NULL "
            "AND upload_part_count IS NULL) OR "
            "(upload_group_id IS NOT NULL AND upload_part_index >= 1 "
            "AND upload_part_count >= 2 "
            "AND upload_part_index <= upload_part_count)",
            name="ck_datasets_upload_partition_fields",
        ),
        CheckConstraint(
            "merge_purpose IS NULL OR "
            "(is_merged AND merge_purpose IN ('merge', 'train'))",
            name="ck_datasets_merge_purpose",
        ),
        UniqueConstraint(
            "workspace_id",
            "upload_group_id",
            "upload_part_index",
            name="uq_datasets_upload_group_part",
        ),
        Index("ix_datasets_upload_group_id", "upload_group_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    image_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    annotation_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    class_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    class_catalog_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    is_merged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    merge_purpose: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_extracted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Rows created by the pre-project UI are retained during migration. Empty,
    # untouched rows become hidden placeholders so no user data is deleted or
    # misclassified as an uploaded dataset.
    is_placeholder: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    upload_group_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    upload_part_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upload_part_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    project: Mapped[Project] = relationship(back_populates="datasets")
    classes: Mapped[list[DatasetClass]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan", passive_deletes=True
    )
    images: Mapped[list[Image]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan", passive_deletes=True
    )
    upload_sessions: Mapped[list[UploadSession]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan", passive_deletes=True
    )
    upload_batches: Mapped[list[UploadBatch]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan", passive_deletes=True
    )
    upload_jobs: Mapped[list[UploadJob]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan", passive_deletes=True
    )
    exports: Mapped[list[ExportArtifact]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan", passive_deletes=True
    )


class DatasetMergeSource(Base):
    __tablename__ = "dataset_merge_sources"
    __table_args__ = (
        CheckConstraint(
            "merged_dataset_id <> source_dataset_id",
            name="ck_dataset_merge_distinct",
        ),
        UniqueConstraint(
            "source_dataset_id",
            name="uq_dataset_merge_source",
        ),
        Index("ix_dataset_merge_source", "source_dataset_id"),
    )

    merged_dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), primary_key=True
    )
    source_dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class DatasetClass(Base):
    __tablename__ = "dataset_classes"

    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), primary_key=True
    )
    class_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    dataset: Mapped[Dataset] = relationship(back_populates="classes")


class MediaObject(Base):
    """One physically stored immutable image bundle shared by image rows."""

    __tablename__ = "media_objects"
    __table_args__ = (
        CheckConstraint(
            "original_bytes >= 0 AND display_bytes >= 0 AND thumb_bytes >= 0",
            name="ck_media_objects_bytes_nonnegative",
        ),
        Index("ix_media_objects_workspace_id", "workspace_id"),
        Index("ix_media_objects_created_by_dataset_id", "created_by_dataset_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Auth users live in another database, so this remains a logical owner
    # boundary.  Media is never shared across owners.
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    # Attribute each physical bundle to exactly one current dataset for the
    # per-dataset "actual occupancy" display.  Deletion transfers this pointer
    # to a surviving reference and the final reference removes the object.
    created_by_dataset_id: Mapped[int | None] = mapped_column(
        ForeignKey("datasets.id", ondelete="SET NULL"),
        nullable=True,
    )
    original_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    display_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    thumb_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    images: Mapped[list[Image]] = relationship(back_populates="media_object")


class Image(Base):
    __tablename__ = "images"
    __table_args__ = (
        CheckConstraint(
            "original_bytes >= 0 AND display_bytes >= 0 AND thumb_bytes >= 0",
            name="ck_images_bytes_nonnegative",
        ),
        UniqueConstraint(
            "dataset_id",
            "split",
            "stem",
            name="uq_images_dataset_split_stem",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_images_dataset_split_stem", "dataset_id", "split", "stem"),
        CheckConstraint(
            "label_status IN ('unlabeled', 'in_progress', 'labeled')",
            name="ck_images_label_status",
        ),
        CheckConstraint(
            "label_status != 'in_progress' OR assignee_id IS NOT NULL",
            name="ck_images_in_progress_assignee",
        ),
        Index("ix_images_dataset_label_status_assignee", "dataset_id", "label_status", "assignee_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False
    )
    # Migration 0012 backfills every pre-existing row. New ingestion always
    # creates an object; merge/extract rows reuse it when hardlinking succeeds.
    media_object_id: Mapped[int] = mapped_column(
        ForeignKey("media_objects.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    stem: Mapped[str] = mapped_column(String(1024), nullable=False)
    filename: Mapped[str] = mapped_column(String(1024), nullable=False)
    rel_path: Mapped[str] = mapped_column(Text, nullable=False)
    # The balancing key tracks the logical uploaded source (automatic upload
    # partitions share one key). The group key is the atomic leakage boundary
    # copied through merge/extract snapshots.
    training_source_key: Mapped[str] = mapped_column(Text, nullable=False)
    training_group_key: Mapped[str] = mapped_column(Text, nullable=False)
    split: Mapped[str | None] = mapped_column(String(64), nullable=True)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    display_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumb_path: Mapped[str] = mapped_column(Text, nullable=False)
    original_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    display_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    thumb_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    box_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    has_label_source: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    label_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unlabeled", server_default="unlabeled"
    )
    assignee_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    labeled_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    labeled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_modified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    dataset: Mapped[Dataset] = relationship(back_populates="images")
    media_object: Mapped[MediaObject] = relationship(back_populates="images")
    annotations: Mapped[list[Annotation]] = relationship(
        back_populates="image", cascade="all, delete-orphan", passive_deletes=True
    )


class Annotation(Base):
    __tablename__ = "annotations"
    __table_args__ = (
        CheckConstraint(
            "serialized_bytes >= 0",
            name="ck_annotations_serialized_bytes_nonnegative",
        ),
        Index("ix_annotations_image_id", "image_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    image_id: Mapped[int] = mapped_column(
        ForeignKey("images.id", ondelete="CASCADE"), nullable=False
    )
    class_id: Mapped[int] = mapped_column(Integer, nullable=False)
    cx: Mapped[float] = mapped_column(Float, nullable=False)
    cy: Mapped[float] = mapped_column(Float, nullable=False)
    w: Mapped[float] = mapped_column(Float, nullable=False)
    h: Mapped[float] = mapped_column(Float, nullable=False)
    serialized_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    image: Mapped[Image] = relationship(back_populates="annotations")


class UploadBatch(Base):
    """Durable manifest for one logical, potentially very large upload."""

    __tablename__ = "upload_batches"
    __table_args__ = (
        CheckConstraint(
            "expected_file_count > 0",
            name="ck_upload_batches_expected_file_count_positive",
        ),
        CheckConstraint(
            "expected_total_size >= 0",
            name="ck_upload_batches_expected_total_size_nonnegative",
        ),
        CheckConstraint(
            "expected_extracted_size >= 0",
            name="ck_upload_batches_expected_extracted_size_nonnegative",
        ),
        CheckConstraint(
            "largest_file_size >= 0 "
            "AND largest_file_size <= expected_total_size",
            name="ck_upload_batches_largest_file_size_valid",
        ),
        Index("ix_upload_batches_dataset_id", "dataset_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False
    )
    expected_file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_total_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_extracted_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    largest_file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="open", server_default="open"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    dataset: Mapped[Dataset] = relationship(back_populates="upload_batches")
    upload_sessions: Mapped[list[UploadSession]] = relationship(
        back_populates="upload_batch", passive_deletes=True
    )
    job: Mapped[UploadJob | None] = relationship(
        back_populates="upload_batch", passive_deletes=True, uselist=False
    )


class UploadSession(Base):
    __tablename__ = "upload_sessions"
    __table_args__ = (
        UniqueConstraint(
            "upload_batch_id",
            "file_key",
            name="uq_upload_sessions_batch_file_key",
        ),
        Index("ix_upload_sessions_upload_batch_id", "upload_batch_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False
    )
    upload_batch_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("upload_batches.id", ondelete="CASCADE"), nullable=True
    )
    file_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    filename: Mapped[str] = mapped_column(String(1024), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False)
    received_chunks: Mapped[list[int]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    dataset: Mapped[Dataset] = relationship(back_populates="upload_sessions")
    upload_batch: Mapped[UploadBatch | None] = relationship(
        back_populates="upload_sessions"
    )


class UploadJob(Base):
    __tablename__ = "upload_jobs"
    __table_args__ = (
        CheckConstraint(
            "ingest_cursor >= 0",
            name="ck_upload_jobs_ingest_cursor_nonnegative",
        ),
        CheckConstraint(
            "image_total >= 0",
            name="ck_upload_jobs_image_total_nonnegative",
        ),
        CheckConstraint(
            "image_processed >= 0",
            name="ck_upload_jobs_image_processed_nonnegative",
        ),
        UniqueConstraint(
            "upload_batch_id",
            name="uq_upload_jobs_upload_batch_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False
    )
    upload_batch_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("upload_batches.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", server_default="queued"
    )
    phase: Mapped[str] = mapped_column(
        String(64), nullable=False, default="queued", server_default="queued"
    )
    total: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    processed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    failed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Number of deterministically ordered image inputs committed to durable
    # dataset batches. Progress may run ahead, but only this cursor is resumed.
    ingest_cursor: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Dedicated image counters keep the progress UI independent from labels,
    # class metadata, and upload archive bookkeeping included in total/processed.
    image_total: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    image_processed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # The worker previously received upload ids only as an in-memory task
    # argument. Persisting them lets a class-conflict pause resume the exact
    # same assembled sources after the user has chosen canonical names.
    upload_ids: Mapped[list[int]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    class_resolution_plan: Mapped[dict | None] = mapped_column(
        JSON, nullable=True
    )
    class_resolutions: Mapped[list[dict] | None] = mapped_column(
        JSON, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    dataset: Mapped[Dataset] = relationship(back_populates="upload_jobs")
    upload_batch: Mapped[UploadBatch | None] = relationship(
        back_populates="job"
    )
    issues: Mapped[list[ImportIssue]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )
    export_artifact: Mapped[ExportArtifact | None] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )


class ImportIssue(Base):
    __tablename__ = "import_issues"
    __table_args__ = (Index("ix_import_issues_job_id", "job_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("upload_jobs.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)

    job: Mapped[UploadJob] = relationship(back_populates="issues")


class ExportArtifact(Base):
    __tablename__ = "exports"
    __table_args__ = (
        CheckConstraint(
            "archive_bytes >= 0",
            name="ck_exports_archive_bytes_nonnegative",
        ),
        Index("ix_exports_dataset_id", "dataset_id"),
    )

    job_id: Mapped[int] = mapped_column(
        ForeignKey("upload_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False
    )
    archive_path: Mapped[str] = mapped_column(Text, nullable=False)
    archive_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    job: Mapped[UploadJob] = relationship(back_populates="export_artifact")
    dataset: Mapped[Dataset] = relationship(back_populates="exports")


class WorkspaceStorage(Base):
    __tablename__ = "workspace_storage"
    __table_args__ = (
        CheckConstraint(
            "bytes_used >= 0",
            name="ck_workspace_storage_bytes_used_nonnegative",
        ),
        CheckConstraint(
            "quota_limit_bytes IS NULL OR quota_limit_bytes > 0",
            name="ck_workspace_storage_quota_limit_positive",
        ),
    )

    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True, autoincrement=False
    )
    bytes_used: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    # NULL inherits the deployment-wide default. Admin-set overrides remain
    # stable when that default changes later.
    quota_limit_bytes: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class TrainingRun(Base):
    __tablename__ = "training_runs"
    __table_args__ = (
        CheckConstraint(
            "split_mode IN ('2way','3way')",
            name="ck_training_runs_split_mode",
        ),
        CheckConstraint(
            "state IN ('queued','running','canceling','done','failed','canceled')",
            name="ck_training_runs_state",
        ),
        CheckConstraint(
            "artifact_bytes IS NULL OR artifact_bytes >= 0",
            name="ck_training_runs_artifact_bytes_nonnegative",
        ),
        Index(
            "uq_single_active_run",
            # PostgreSQL requires an expression index to make this global,
            # rather than unique per dataset or run identifier.
            text("(true)"),
            unique=True,
            postgresql_where=text("state IN ('running','canceling')"),
        ),
        Index("ix_training_runs_workspace_id", "workspace_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    created_by: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_id: Mapped[int | None] = mapped_column(
        ForeignKey("datasets.id", ondelete="SET NULL"), nullable=True
    )
    dataset_name: Mapped[str] = mapped_column(String(255), nullable=False)
    weights: Mapped[str] = mapped_column(String(255), nullable=False)
    epochs: Mapped[int] = mapped_column(Integer, nullable=False)
    imgsz: Mapped[int] = mapped_column(Integer, nullable=False)
    batch: Mapped[int] = mapped_column(Integer, nullable=False)
    split_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    ratios: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    training_args: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    # `queued` is reserved in the schema but intentionally unreachable: run
    # submission inserts `running` atomically under uq_single_active_run.
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pid_started_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    boot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    out_dir: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifacts_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # NULL marks legacy/unreconciled runs. New terminal transitions persist an
    # exact byte count so quota reads and deletes do not scan run directories.
    artifact_bytes: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    dataset: Mapped[Dataset | None] = relationship()
    images: Mapped[list[RunImage]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )
    metrics: Mapped[list[RunMetric]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )


class RunImage(Base):
    __tablename__ = "run_images"
    __table_args__ = (
        CheckConstraint(
            "split IN ('train','valid','test')",
            name="ck_run_images_split",
        ),
        UniqueConstraint("run_id", "image_id", name="uq_run_images_run_image"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("training_runs.id", ondelete="CASCADE"), nullable=False
    )
    image_id: Mapped[int | None] = mapped_column(
        ForeignKey("images.id", ondelete="SET NULL"), nullable=True
    )
    split: Mapped[str] = mapped_column(String(16), nullable=False)
    stem: Mapped[str] = mapped_column(String(1024), nullable=False)
    filename: Mapped[str] = mapped_column(String(1024), nullable=False)
    rel_path: Mapped[str] = mapped_column(Text, nullable=False)

    run: Mapped[TrainingRun] = relationship(back_populates="images")
    image: Mapped[Image | None] = relationship()


class RunMetric(Base):
    __tablename__ = "run_metrics"
    __table_args__ = (
        UniqueConstraint("run_id", "epoch", name="uq_run_metrics_run_epoch"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("training_runs.id", ondelete="CASCADE"), nullable=False
    )
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    box_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    cls_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    dfl_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    map50: Mapped[float | None] = mapped_column(Float, nullable=True)
    map5095: Mapped[float | None] = mapped_column(Float, nullable=True)
    lr: Mapped[dict[str, float] | None] = mapped_column(JSON, nullable=True)

    run: Mapped[TrainingRun] = relationship(back_populates="metrics")


class AdminUser(Base):
    """Operator grant for the read-only admin dashboard (F26)."""

    __tablename__ = "admin_users"

    user_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=False
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
