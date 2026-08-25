"""Import a mounted YOLO directory through Annodock's normal ingest pipeline.

This avoids copying every source file through an HTTP upload session when the
same storage is already mounted on the Annodock host. Source files are treated
as read-only; originals and browser thumbnails are still copied into the
configured managed storage directory.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import create_engine, create_session_factory  # noqa: E402
from app.models import (  # noqa: E402
    Dataset,
    DatasetClass,
    Project,
    ProjectClass,
    UploadJob,
)
from app.services.collect import (  # noqa: E402
    CollectedFile,
    SourceFile,
    collect_directory,
    collect_sources,
)
from app.services.ingest import ingest_collected  # noqa: E402
from app.services.quota import quota_status  # noqa: E402
from app.services.storage import (  # noqa: E402
    create_dataset_storage,
    storage_relative_path,
)
from app.services.validate import validate_upload_capacity  # noqa: E402


ACTIVE_JOB_STATES = frozenset(
    {"queued", "running", "awaiting_class_resolution"}
)


@dataclass(frozen=True)
class MountedImportSummary:
    file_count: int
    image_count: int
    label_count: int
    classfile_count: int
    total_bytes: int


@dataclass(frozen=True)
class MountedImportTarget:
    dataset_id: int
    job_id: int


def _validated_source_directory(source: Path) -> Path:
    if source.is_symlink():
        raise ValueError(f"symbolic source directory is not allowed: {source}")
    resolved = source.resolve()
    if not resolved.is_dir():
        raise ValueError(f"source directory does not exist: {source}")
    return resolved


def _validated_metadata(metadata: Path) -> Path:
    if metadata.is_symlink():
        raise ValueError(f"symbolic class metadata is not allowed: {metadata}")
    resolved = metadata.resolve()
    if not resolved.is_file():
        raise ValueError(f"class metadata does not exist: {metadata}")
    return resolved


def build_mounted_items(
    source: Path,
    metadata: Path,
    allowed_extensions: tuple[str, ...],
) -> list[CollectedFile]:
    """Collect supported files and add class metadata outside the data root."""

    source = _validated_source_directory(source)
    metadata = _validated_metadata(metadata)
    items = [
        item
        for item in collect_directory(source, allowed_extensions)
        if item.kind != "other"
    ]
    known_paths = {item.abs_path for item in items}
    if metadata not in known_paths:
        try:
            metadata_rel_path = metadata.relative_to(source).as_posix()
        except ValueError:
            metadata_rel_path = metadata.name
        metadata_items = collect_sources(
            [SourceFile(rel_path=metadata_rel_path, abs_path=metadata)],
            allowed_extensions,
        )
        if (
            len(metadata_items) != 1
            or metadata_items[0].kind != "classfile"
        ):
            raise ValueError(f"unsupported class metadata: {metadata}")
        items.extend(metadata_items)
    return sorted(items, key=lambda item: item.rel_path)


def summarize_items(items: list[CollectedFile]) -> MountedImportSummary:
    image_count = sum(item.kind == "image" for item in items)
    label_count = sum(item.kind == "label" for item in items)
    classfile_count = sum(item.kind == "classfile" for item in items)
    if image_count == 0:
        raise ValueError("mounted dataset contains no supported images")
    if classfile_count == 0:
        raise ValueError("mounted dataset contains no supported class metadata")
    return MountedImportSummary(
        file_count=len(items),
        image_count=image_count,
        label_count=label_count,
        classfile_count=classfile_count,
        total_bytes=sum(item.abs_path.stat().st_size for item in items),
    )


async def _check_quota(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    owner_id: int,
    required_bytes: int,
) -> None:
    async with session_factory() as session:
        status = await quota_status(
            session,
            owner_id,
            limit_bytes=settings.quota_bytes_per_user,
            required_bytes=required_bytes,
        )
    if not status.allowed:
        raise ValueError(status.detail)


async def _create_import_target(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    owner_id: int,
    project_id: int,
    name: str,
    file_count: int,
) -> MountedImportTarget:
    async with session_factory() as session:
        project = await session.scalar(
            select(Project)
            .where(Project.id == project_id, Project.owner_id == owner_id)
            .with_for_update()
        )
        if project is None:
            raise LookupError(
                f"project {project_id} does not belong to owner {owner_id}"
            )

        existing = await session.scalar(
            select(Dataset)
            .where(Dataset.owner_id == owner_id, Dataset.name == name)
            .with_for_update()
        )
        if existing is not None:
            active_job = await session.scalar(
                select(UploadJob)
                .where(
                    UploadJob.dataset_id == existing.id,
                    UploadJob.state.in_(ACTIVE_JOB_STATES),
                )
                .order_by(UploadJob.id.desc())
            )
            if existing.status == "ready":
                raise ValueError(
                    f"dataset '{name}' is already ready as id {existing.id}"
                )
            if existing.image_count > 0:
                raise ValueError(
                    f"dataset '{name}' is not empty and cannot be reused"
                )
            if active_job is not None:
                raise ValueError(
                    f"dataset '{name}' already has active job {active_job.id}"
                )
            dataset = existing
            dataset.status = "processing"
            dataset.is_placeholder = True
        else:
            project_classes = list(
                (
                    await session.scalars(
                        select(ProjectClass)
                        .where(ProjectClass.project_id == project_id)
                        .order_by(ProjectClass.class_id)
                    )
                ).all()
            )
            dataset = Dataset(
                owner_id=owner_id,
                project_id=project_id,
                name=name,
                status="processing",
                storage_path="",
                class_count=len(project_classes),
                is_placeholder=True,
            )
            session.add(dataset)
            await session.flush()
            storage_path = create_dataset_storage(
                settings.storage_dir,
                dataset.id,
            )
            dataset.storage_path = storage_relative_path(
                settings.storage_dir,
                storage_path,
            )
            session.add_all(
                [
                    DatasetClass(
                        dataset_id=dataset.id,
                        class_id=item.class_id,
                        name=item.name,
                    )
                    for item in project_classes
                ]
            )

        job = UploadJob(
            dataset_id=dataset.id,
            kind="mounted-folder",
            state="queued",
            phase="collecting",
            total=file_count,
            processed=0,
            failed=0,
            upload_ids=[],
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        return MountedImportTarget(dataset_id=dataset.id, job_id=job.id)


async def import_mounted_dataset(
    settings: Settings,
    *,
    database_url: str,
    source: Path,
    metadata: Path,
    owner_id: int,
    project_id: int,
    name: str,
    dry_run: bool = False,
) -> tuple[MountedImportSummary, MountedImportTarget | None]:
    print(f"collecting mounted source: {source}", flush=True)
    items = await asyncio.to_thread(
        build_mounted_items,
        source,
        metadata,
        settings.allowed_image_exts,
    )
    summary = await asyncio.to_thread(summarize_items, items)
    print(
        "collected "
        f"files={summary.file_count} images={summary.image_count} "
        f"labels={summary.label_count} classes={summary.classfile_count} "
        f"bytes={summary.total_bytes}",
        flush=True,
    )
    validate_upload_capacity(
        settings,
        size=summary.total_bytes,
        file_count=summary.file_count,
        expected_extracted_size=summary.total_bytes,
    )
    if dry_run:
        return summary, None

    engine = create_engine(database_url)
    session_factory = create_session_factory(engine)
    try:
        await _check_quota(
            settings,
            session_factory,
            owner_id=owner_id,
            required_bytes=summary.total_bytes,
        )
        target = await _create_import_target(
            settings,
            session_factory,
            owner_id=owner_id,
            project_id=project_id,
            name=name,
            file_count=summary.file_count,
        )
        print(
            f"created dataset_id={target.dataset_id} job_id={target.job_id}",
            flush=True,
        )
        await ingest_collected(
            settings,
            session_factory,
            target.job_id,
            items,
            initial_phases=("collecting",),
            phase_observer=lambda phase: print(
                f"job_id={target.job_id} phase={phase}",
                flush=True,
            ),
            require_class_resolution=False,
        )
        return summary, target
    finally:
        await engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--owner-id", type=int, required=True)
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--database-url")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = Settings()
    summary, target = asyncio.run(
        import_mounted_dataset(
            settings,
            database_url=args.database_url or settings.database_url,
            source=args.source,
            metadata=args.metadata,
            owner_id=args.owner_id,
            project_id=args.project_id,
            name=args.name,
            dry_run=args.dry_run,
        )
    )
    if target is None:
        print(
            f"dry-run ready: files={summary.file_count} "
            f"bytes={summary.total_bytes}",
            flush=True,
        )
        return
    print(
        f"import complete: dataset_id={target.dataset_id} "
        f"job_id={target.job_id}",
        flush=True,
    )


if __name__ == "__main__":
    main()
