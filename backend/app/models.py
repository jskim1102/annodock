"""SQLAlchemy models for dataset import, review, and export state."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_projects_owner_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # auth-service lives in a separate database, so this remains a logical
    # reference instead of a cross-database foreign key.
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
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
        UniqueConstraint("owner_id", "name", name="uq_datasets_owner_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # auth-service lives in a separate database, so this is deliberately a
    # logical reference instead of a cross-database foreign key.
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
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
    is_merged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Rows created by the pre-project UI are retained during migration. Empty,
    # untouched rows become hidden placeholders so no user data is deleted or
    # misclassified as an uploaded dataset.
    is_placeholder: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
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
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False
    )
    stem: Mapped[str] = mapped_column(String(1024), nullable=False)
    filename: Mapped[str] = mapped_column(String(1024), nullable=False)
    rel_path: Mapped[str] = mapped_column(Text, nullable=False)
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
    is_modified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    dataset: Mapped[Dataset] = relationship(back_populates="images")
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


class UploadSession(Base):
    __tablename__ = "upload_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(1024), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
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


class UploadJob(Base):
    __tablename__ = "upload_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False
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


class UserStorage(Base):
    __tablename__ = "user_storage"
    __table_args__ = (
        CheckConstraint(
            "bytes_used >= 0",
            name="ck_user_storage_bytes_used_nonnegative",
        ),
    )

    # Logical auth-service user reference; no cross-database FK is possible.
    owner_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=False
    )
    bytes_used: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Organization(Base):
    __tablename__ = "orgs"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_orgs_owner_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Logical auth-service user reference; no cross-database FK is possible.
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    teams: Mapped[list[Team]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_teams_org_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    organization: Mapped[Organization] = relationship(back_populates="teams")
    memberships: Mapped[list[Membership]] = relationship(
        back_populates="team",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        CheckConstraint(
            "role IN ('owner','admin','editor','viewer')",
            name="ck_memberships_role",
        ),
        Index("ix_memberships_user_id", "user_id"),
    )

    team_id: Mapped[int] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True
    )
    # Logical auth-service user reference; no cross-database FK is possible.
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(
        String(32), nullable=False, default="viewer", server_default="viewer"
    )
    can_view: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    can_edit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    can_manage: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    team: Mapped[Team] = relationship(back_populates="memberships")


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
        Index("ix_training_runs_owner_id", "owner_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Logical auth-service user reference; no cross-database FK is possible.
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
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
