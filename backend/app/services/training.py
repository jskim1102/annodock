"""Training submission preflight and atomic run creation."""

from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import torch
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from ultralytics.utils import is_online

from app.config import Settings
from app.models import Annotation, Dataset, DatasetClass, Image, TrainingRun
from app.services.cleanup import stage_run_workdir_async
from app.services.rundir import build_run_directory
from app.services.quota import (
    estimate_training_artifact_bytes,
    increase_bytes_used,
    path_tree_bytes,
    quota_status,
)
from app.services.rundir import collect_run_artifacts
from app.services.storage import (
    contained_storage_path,
    finalize_staged_deletion,
    restore_staged_deletion,
    storage_relative_path,
)
from app.services.proc_identity import read_process_identity
from app.services.split import (
    allocate_splits,
    load_dataset_images,
    persist_run_images,
    validate_split_size,
)


ACTIVE_RUN_DETAIL = (
    "이미 학습 중인 run 이 있습니다. GPU 가 1장이라 동시 학습은 하나만 됩니다. "
    "진행 중인 학습이 끝나거나 취소된 뒤 다시 시도하세요."
)
GPU_DETAIL = (
    "GPU 를 사용할 수 없습니다. "
    "학습은 GPU(RTX 3090)가 있는 dev.sh 호스트 환경에서만 됩니다."
)
GAPPED_CLASS_DETAIL = (
    "클래스 인덱스에 빈 번호가 있어(예: 0,2) 학습할 수 없습니다. "
    "데이터셋의 클래스 구성을 확인하세요."
)
SMALL_DATASET_DETAIL = (
    "데이터가 너무 적어 이 비율로는 train 또는 valid 가 0장이 됩니다. "
    "분할 비율을 조정하거나 이미지를 더 추가하세요."
)
OFFLINE_DETAIL = (
    "preset 가중치를 내려받지 못했습니다. 첫 학습은 인터넷 연결이 필요합니다. "
    "네트워크를 확인하고 다시 시도하세요."
)
CONTAINER_DETAIL = (
    "컨테이너에서는 학습을 시작할 수 없습니다. "
    "dev.sh 호스트 환경에서 실행하세요."
)
DATASET_NOT_FOUND_DETAIL = "데이터셋을 찾을 수 없습니다."
WORKER_START_DETAIL = (
    "학습 워커를 시작하지 못했습니다. 학습 기록에서 오류를 확인하세요."
)
WEIGHTS_DIR = Path(__file__).resolve().parents[2] / "weights"
BACKEND_ROOT = Path(__file__).resolve().parents[2]
FREE_VRAM_WARNING_RATIO = 0.8


@dataclass(frozen=True)
class TrainingConfig:
    weights: str
    epochs: int
    imgsz: int
    batch: int
    split_mode: str
    ratios: dict[str, float]
    seed: int | None
    exclude_unlabeled_images: bool
    include_unlabeled_images_in_test: bool
    training_args: dict[str, object]


@dataclass(frozen=True)
class StartTrainingResult:
    run_id: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class SpawnedWorker:
    pid: int
    pid_started_at: str
    boot_id: str


class TrainingRequestError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def is_container_environment() -> bool:
    return Path("/.dockerenv").exists()


def _vram_warnings() -> list[str]:
    try:
        free_bytes, total_bytes = torch.cuda.mem_get_info()
    except (OSError, RuntimeError):
        return []
    if total_bytes <= 0 or free_bytes >= total_bytes * FREE_VRAM_WARNING_RATIO:
        return []
    free_gib = free_bytes / 1024**3
    total_gib = total_bytes / 1024**3
    return [
        "현재 사용 가능한 GPU 메모리가 "
        f"{free_gib:.1f}GiB/{total_gib:.1f}GiB입니다. "
        "다른 GPU 작업을 종료하거나 batch 자동을 사용하세요."
    ]


def _weight_is_available(weights: str) -> bool:
    return (WEIGHTS_DIR / weights).is_file()


def spawn_worker(
    run_id: int,
    owner_id: int,
    out_dir: str | Path,
    database_url: str,
) -> SpawnedWorker:
    """Start a worker in a new session with a pre-created append-only log."""
    output = Path(out_dir)
    log_path = output / "artifacts" / "log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        sys.executable,
        "-u",
        "-m",
        "app.worker.train_worker",
        "--run-id",
        str(run_id),
        "--owner-id",
        str(owner_id),
    ]
    environment = {
        **os.environ,
        "DATABASE_URL": database_url,
        "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
    }
    with log_path.open("ab", buffering=0) as log_file:
        process = subprocess.Popen(
            argv,
            cwd=BACKEND_ROOT,
            env=environment,
            start_new_session=True,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    identity = read_process_identity(process.pid)
    if identity is None:
        raise RuntimeError("training worker exited before identity was recorded")
    return SpawnedWorker(
        pid=process.pid,
        pid_started_at=identity.started_at,
        boot_id=identity.boot_id,
    )


async def _mark_spawn_failed(
    session: AsyncSession,
    run_id: int,
    owner_id: int,
    error: Exception,
    *,
    storage_dir: Path,
) -> bool:
    return await mark_training_failed(
        session,
        run_id,
        f"워커 시작 실패: {error}",
        owner_id=owner_id,
        storage_dir=storage_dir,
    )


async def mark_training_failed(
    session: AsyncSession,
    run_id: int,
    reason: str,
    *,
    owner_id: int | None = None,
    active_states: tuple[str, ...] = ("running",),
    storage_dir: Path | None = None,
) -> bool:
    """Atomically persist the first active-run failure reason."""
    predicates = [
        TrainingRun.id == run_id,
        TrainingRun.state.in_(active_states),
    ]
    if owner_id is not None:
        predicates.append(TrainingRun.owner_id == owner_id)
    run = await session.scalar(
        select(TrainingRun).where(*predicates).with_for_update()
    )
    if run is None:
        await session.rollback()
        return False
    artifact_bytes = 0
    staged = None
    pid_confirmed = (
        run.pid is not None
        and run.pid_started_at is not None
        and run.boot_id is not None
    )
    if storage_dir is not None and pid_confirmed:
        out_dir = contained_storage_path(storage_dir, run.out_dir)
        try:
            artifact_bytes = await asyncio.to_thread(
                collect_run_artifacts,
                out_dir,
            )
        except (OSError, ValueError):
            artifact_bytes = await asyncio.to_thread(
                path_tree_bytes,
                out_dir / "artifacts",
            )
        else:
            staged = await stage_run_workdir_async(
                storage_dir,
                run.out_dir,
            )
    run.state = "failed"
    if run.finished_at is None:
        run.finished_at = datetime.now(timezone.utc)
    if run.error is None:
        run.error = reason
    run.artifact_bytes = artifact_bytes
    await increase_bytes_used(session, run.owner_id, artifact_bytes)
    try:
        await session.commit()
    except BaseException as error:
        restore_staged_deletion(staged)
        try:
            await session.rollback()
        except BaseException as rollback_error:
            error.add_note(
                "training failure rollback also failed: "
                f"{type(rollback_error).__name__}"
            )
        raise
    await asyncio.to_thread(finalize_staged_deletion, staged)
    return True


async def start_training(
    session: AsyncSession,
    settings: Settings,
    dataset_id: int,
    owner_id: int,
    config: TrainingConfig,
) -> StartTrainingResult:
    """Validate, atomically claim the GPU slot, split, and materialize a run."""
    # Serialize run submission with dataset deletion and class rename. This
    # makes the generated data.yaml a coherent submit-time snapshot.
    dataset = await session.scalar(
        select(Dataset)
        .where(
            Dataset.id == dataset_id,
            Dataset.owner_id == owner_id,
        )
        .with_for_update()
    )
    if dataset is None:
        raise TrainingRequestError(404, DATASET_NOT_FOUND_DETAIL)
    if is_container_environment():
        raise TrainingRequestError(503, CONTAINER_DETAIL)
    weights_path = WEIGHTS_DIR / config.weights
    expected_artifact_bytes = estimate_training_artifact_bytes(
        weight_bytes=(weights_path.stat().st_size if weights_path.is_file() else 0),
        epochs=config.epochs,
    )
    status = await quota_status(
        session,
        owner_id,
        limit_bytes=settings.quota_bytes_per_user,
        required_bytes=expected_artifact_bytes,
    )
    if not status.allowed:
        raise TrainingRequestError(413, status.detail)
    if not torch.cuda.is_available():
        raise TrainingRequestError(400, GPU_DETAIL)

    classes = (
        await session.scalars(
            select(DatasetClass)
            .where(DatasetClass.dataset_id == dataset_id)
            .order_by(DatasetClass.class_id)
        )
    ).all()
    used_class_ids = (
        await session.scalars(
            select(Annotation.class_id)
            .join(Image, Image.id == Annotation.image_id)
            .where(Image.dataset_id == dataset_id)
            .distinct()
        )
    ).all()
    configured_class_ids = [row.class_id for row in classes]
    effective_class_ids = sorted(set(configured_class_ids) | set(used_class_ids))
    if (
        configured_class_ids != list(range(len(configured_class_ids)))
        or effective_class_ids != list(range(len(effective_class_ids)))
    ):
        raise TrainingRequestError(400, GAPPED_CLASS_DETAIL)

    all_images = await load_dataset_images(
        session,
        dataset_id,
        settings.storage_dir,
    )
    missing_label_images = [image for image in all_images if image.is_unlabeled]
    images = (
        [image for image in all_images if not image.is_unlabeled]
        if (
            config.exclude_unlabeled_images
            or config.include_unlabeled_images_in_test
        )
        else all_images
    )
    try:
        validate_split_size(len(images), config.ratios)
    except ValueError as error:
        raise TrainingRequestError(400, SMALL_DATASET_DETAIL) from error
    if not _weight_is_available(config.weights) and not is_online():
        raise TrainingRequestError(400, OFFLINE_DETAIL)

    seed = config.seed if config.seed is not None else secrets.randbits(31)
    split_result = allocate_splits(
        images,
        config.ratios,
        seed=seed,
        test_only_images=(
            missing_label_images
            if config.include_unlabeled_images_in_test
            else ()
        ),
    )
    warnings = [*_vram_warnings(), *split_result.warnings]
    run = TrainingRun(
        owner_id=owner_id,
        dataset_id=dataset.id,
        dataset_name=dataset.name,
        weights=config.weights,
        epochs=config.epochs,
        imgsz=config.imgsz,
        batch=config.batch,
        split_mode=config.split_mode,
        ratios=dict(config.ratios),
        seed=seed,
        training_args=dict(config.training_args),
        state="running",
        started_at=datetime.now(timezone.utc),
        out_dir="",
    )
    session.add(run)
    try:
        await session.flush()
    except IntegrityError as error:
        await session.rollback()
        raise TrainingRequestError(409, ACTIVE_RUN_DETAIL) from error

    out_dir = (
        settings.storage_dir.resolve() / "training-runs" / str(run.id)
    )
    run.out_dir = storage_relative_path(settings.storage_dir, out_dir)
    persist_run_images(session, run.id, split_result.assignments)
    class_names: Mapping[int, str] = {
        row.class_id: row.name for row in classes
    }
    try:
        await asyncio.to_thread(
            build_run_directory,
            out_dir,
            split_result.assignments,
            class_names,
            split_mode=config.split_mode,
        )
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    try:
        spawned = await asyncio.to_thread(
            spawn_worker,
            run.id,
            owner_id,
            out_dir,
            settings.database_url,
        )
    except Exception as error:
        changed = await _mark_spawn_failed(
            session,
            run.id,
            owner_id,
            error,
            storage_dir=settings.storage_dir,
        )
        if changed:
            raise TrainingRequestError(500, WORKER_START_DETAIL) from error
        return StartTrainingResult(run_id=run.id, warnings=tuple(warnings))
    await session.execute(
        update(TrainingRun)
        .where(
            TrainingRun.id == run.id,
            TrainingRun.owner_id == owner_id,
            TrainingRun.state == "running",
        )
        .values(
            pid=spawned.pid,
            pid_started_at=spawned.pid_started_at,
            boot_id=spawned.boot_id,
        )
    )
    await session.commit()
    return StartTrainingResult(run_id=run.id, warnings=tuple(warnings))
