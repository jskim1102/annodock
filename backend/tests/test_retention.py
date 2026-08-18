from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.models import Dataset, Project, TrainingRun, UserStorage
from app.services import cleanup


pytestmark = pytest.mark.asyncio

OWNER_A = 2_100_001
OWNER_B = 2_100_002
RETENTION_NOW = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)


def _name(kind: str) -> str:
    return f"test-retention-{kind}-{uuid4().hex}"


def _run(
    owner_id: int,
    *,
    state: str,
    finished_at: datetime | None,
    dataset_id: int | None = None,
) -> TrainingRun:
    return TrainingRun(
        owner_id=owner_id,
        dataset_id=dataset_id,
        dataset_name=_name("run"),
        weights="yolo26n.pt",
        epochs=2,
        imgsz=640,
        batch=4,
        split_mode="2way",
        ratios={"train": 0.8, "valid": 0.2},
        seed=11,
        state=state,
        started_at=(finished_at or RETENTION_NOW) - timedelta(hours=1),
        finished_at=finished_at,
        out_dir="training-runs/pending",
    )


async def _persist_dataset(app, owner_id: int, payload: bytes) -> tuple[int, Path]:
    async with app.state.session_factory() as session:
        project = Project(
            owner_id=owner_id,
            name=_name("project"),
        )
        session.add(project)
        await session.flush()
        dataset = Dataset(
            owner_id=owner_id,
            project_id=project.id,
            name=_name("dataset"),
            status="ready",
            storage_path="datasets/pending",
            image_count=0,
            annotation_count=0,
            class_count=0,
        )
        session.add(dataset)
        await session.flush()
        dataset.storage_path = f"datasets/{dataset.id}"
        await session.commit()
        dataset_id = dataset.id

    dataset_dir = app.state.settings.storage_dir / "datasets" / str(dataset_id)
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "original.bin").write_bytes(payload)
    return dataset_id, dataset_dir


async def _persist_run(
    app,
    run: TrainingRun,
    *,
    artifact_parts: tuple[bytes, ...],
    workdir_payload: bytes = b"temporary-workdir-data",
) -> tuple[int, Path, int]:
    async with app.state.session_factory() as session:
        session.add(run)
        await session.flush()
        run.out_dir = f"training-runs/{run.id}"
        await session.commit()
        run_id = run.id

    out_dir = app.state.settings.storage_dir / "training-runs" / str(run_id)
    artifacts_dir = out_dir / "artifacts"
    (artifacts_dir / "nested").mkdir(parents=True)
    for index, payload in enumerate(artifact_parts):
        target = (
            artifacts_dir / f"artifact-{index}.bin"
            if index % 2 == 0
            else artifacts_dir / "nested" / f"artifact-{index}.bin"
        )
        target.write_bytes(payload)
    workdir = out_dir / "workdir"
    workdir.mkdir()
    (workdir / "source-link.bin").write_bytes(workdir_payload)
    return run_id, out_dir, sum(len(payload) for payload in artifact_parts)


async def _set_usage(app, owner_id: int, bytes_used: int) -> None:
    async with app.state.session_factory() as session:
        session.add(UserStorage(owner_id=owner_id, bytes_used=bytes_used))
        await session.commit()


async def _run_and_usage(app, run_id: int, owner_id: int):
    async with app.state.session_factory() as session:
        return (
            await session.get(TrainingRun, run_id),
            await session.get(UserStorage, owner_id),
        )


@pytest_asyncio.fixture(autouse=True)
async def cleanup_retention_users(app):
    async def remove_rows() -> None:
        async with app.state.session_factory() as session:
            await session.execute(
                delete(UserStorage).where(
                    UserStorage.owner_id.in_((OWNER_A, OWNER_B))
                )
            )
            await session.commit()

    await remove_rows()
    yield
    await remove_rows()


@pytest.mark.parametrize("active_state", ("running", "canceling"))
async def test_retention_keep_count_is_per_user_and_excludes_active_runs(
    app,
    active_state: str,
) -> None:
    dataset_a_id, dataset_a_dir = await _persist_dataset(app, OWNER_A, b"data-a")
    dataset_b_id, dataset_b_dir = await _persist_dataset(app, OWNER_B, b"data-b")

    active_id, active_out, active_bytes = await _persist_run(
        app,
        _run(
            OWNER_A,
            state=active_state,
            finished_at=None,
            dataset_id=dataset_a_id,
        ),
        artifact_parts=(b"active",),
    )
    recent_id, recent_out, recent_bytes = await _persist_run(
        app,
        _run(
            OWNER_A,
            state="done",
            finished_at=RETENTION_NOW - timedelta(days=1),
            dataset_id=dataset_a_id,
        ),
        artifact_parts=(b"recent", b"artifacts"),
    )
    expired_id, expired_out, expired_bytes = await _persist_run(
        app,
        _run(
            OWNER_A,
            state="failed",
            finished_at=RETENTION_NOW - timedelta(days=2),
            dataset_id=dataset_a_id,
        ),
        artifact_parts=(b"expired", b"artifact-bytes"),
    )
    other_id, other_out, other_bytes = await _persist_run(
        app,
        _run(
            OWNER_B,
            state="done",
            finished_at=RETENTION_NOW - timedelta(days=10),
            dataset_id=dataset_b_id,
        ),
        artifact_parts=(b"other-owner",),
    )
    usage_a = len(b"data-a") + active_bytes + recent_bytes + expired_bytes
    usage_b = len(b"data-b") + other_bytes
    await _set_usage(app, OWNER_A, usage_a)
    await _set_usage(app, OWNER_B, usage_b)

    result = await cleanup.retain_run_artifacts(
        app.state.session_factory,
        storage_dir=app.state.settings.storage_dir,
        keep_count=1,
        keep_days=365,
        now=RETENTION_NOW,
    )

    assert result.removed_runs == 1
    assert result.removed_bytes == expired_bytes
    expired, owner_a_usage = await _run_and_usage(app, expired_id, OWNER_A)
    active, _ = await _run_and_usage(app, active_id, OWNER_A)
    recent, _ = await _run_and_usage(app, recent_id, OWNER_A)
    other, owner_b_usage = await _run_and_usage(app, other_id, OWNER_B)
    assert expired is not None and expired.artifacts_deleted_at is not None
    assert active is not None and active.artifacts_deleted_at is None
    assert recent is not None and recent.artifacts_deleted_at is None
    assert other is not None and other.artifacts_deleted_at is None
    assert owner_a_usage is not None
    assert owner_a_usage.bytes_used == usage_a - expired_bytes
    assert owner_b_usage is not None and owner_b_usage.bytes_used == usage_b

    assert not (expired_out / "artifacts").exists()
    assert (expired_out / "workdir" / "source-link.bin").is_file()
    assert (active_out / "artifacts").is_dir()
    assert (recent_out / "artifacts").is_dir()
    assert (other_out / "artifacts").is_dir()
    assert (dataset_a_dir / "original.bin").read_bytes() == b"data-a"
    assert (dataset_b_dir / "original.bin").read_bytes() == b"data-b"


async def test_retention_keep_days_removes_only_runs_older_than_cutoff(app) -> None:
    old_id, old_out, old_bytes = await _persist_run(
        app,
        _run(
            OWNER_A,
            state="canceled",
            finished_at=RETENTION_NOW - timedelta(days=31),
        ),
        artifact_parts=(b"older-than-thirty-days",),
    )
    recent_id, recent_out, recent_bytes = await _persist_run(
        app,
        _run(
            OWNER_A,
            state="failed",
            finished_at=RETENTION_NOW - timedelta(days=29),
        ),
        artifact_parts=(b"still-within-retention",),
    )
    await _set_usage(app, OWNER_A, old_bytes + recent_bytes)

    result = await cleanup.retain_run_artifacts(
        app.state.session_factory,
        storage_dir=app.state.settings.storage_dir,
        keep_count=10,
        keep_days=30,
        now=RETENTION_NOW,
    )

    assert result.removed_runs == 1
    assert result.removed_bytes == old_bytes
    old, usage = await _run_and_usage(app, old_id, OWNER_A)
    recent, _ = await _run_and_usage(app, recent_id, OWNER_A)
    assert old is not None and old.artifacts_deleted_at is not None
    assert recent is not None and recent.artifacts_deleted_at is None
    assert usage is not None and usage.bytes_used == recent_bytes
    assert not (old_out / "artifacts").exists()
    assert (recent_out / "artifacts").is_dir()


async def test_pending_deletion_finalizer_reclaims_only_quarantined_entries(
    app,
) -> None:
    storage_dir = app.state.settings.storage_dir
    protected = storage_dir / "datasets" / "protected"
    protected.mkdir(parents=True)
    protected_file = protected / "original.bin"
    protected_file.write_bytes(b"must-survive")
    pending_root = storage_dir / ".delete-pending"
    orphan_one = pending_root / "artifacts-deadbeef"
    orphan_two = pending_root / "dataset-cafebabe"
    (orphan_one / "nested").mkdir(parents=True)
    orphan_two.mkdir()
    (orphan_one / "nested" / "best.pt").write_bytes(b"weights")
    (orphan_two / "image.jpg").write_bytes(b"image")
    expired_at = (
        datetime.now(timezone.utc).timestamp()
        - cleanup.PENDING_DELETION_MIN_AGE_SECONDS
        - 1
    )
    os.utime(orphan_one, (expired_at, expired_at))
    os.utime(orphan_two, (expired_at, expired_at))

    result = await asyncio.to_thread(
        cleanup.finalize_pending_deletions,
        storage_dir,
    )

    assert result.finalized_pending == 2
    assert not orphan_one.exists()
    assert not orphan_two.exists()
    assert protected_file.read_bytes() == b"must-survive"


async def test_kept_run_remains_compatible_with_manual_artifact_delete(
    app,
    client,
    auth_headers,
) -> None:
    run_id, out_dir, artifact_bytes = await _persist_run(
        app,
        _run(
            OWNER_A,
            state="done",
            finished_at=RETENTION_NOW - timedelta(days=1),
        ),
        artifact_parts=(b"best-weights", b"last-weights"),
    )
    retained_bytes = 37
    await _set_usage(app, OWNER_A, retained_bytes + artifact_bytes)

    result = await cleanup.retain_run_artifacts(
        app.state.session_factory,
        storage_dir=app.state.settings.storage_dir,
        keep_count=1,
        keep_days=30,
        now=RETENTION_NOW,
    )
    assert result.removed_runs == 0

    hidden = await client.delete(
        f"/api/runs/{run_id}/artifacts",
        headers=auth_headers(OWNER_B),
    )
    deleted = await client.delete(
        f"/api/runs/{run_id}/artifacts",
        headers=auth_headers(OWNER_A),
    )
    repeated = await client.delete(
        f"/api/runs/{run_id}/artifacts",
        headers=auth_headers(OWNER_A),
    )

    assert hidden.status_code == 404
    assert deleted.status_code == 204
    assert repeated.status_code == 204
    run, usage = await _run_and_usage(app, run_id, OWNER_A)
    assert run is not None and run.artifacts_deleted_at is not None
    assert usage is not None and usage.bytes_used == retained_bytes
    assert not (out_dir / "artifacts").exists()
    assert (out_dir / "workdir" / "source-link.bin").is_file()
