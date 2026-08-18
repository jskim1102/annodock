"""Detached Ultralytics worker for one persisted training run."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row
from ultralytics import YOLO

from app.config import get_settings
from app.services.cleanup import stage_run_workdir
from app.services.quota import increase_bytes_used_sync
from app.services.rundir import collect_run_artifacts
from app.services.storage import (
    contained_storage_path,
    finalize_staged_deletion,
    restore_staged_deletion,
)
from app.services.training import WEIGHTS_DIR
from app.training_params import normalize_training_args, ultralytics_training_args
from app.worker.callbacks import make_epoch_callback
from app.worker.failure import (
    FailureReport,
    classify_failure,
    persist_worker_failure,
)


AUTOBATCH_FLOOR = 4
AUTOBATCH_FLOOR_DETAIL = (
    "AutoBatch가 선택한 batch {batch}는 최소값 4보다 작아 학습을 시작하지 않았습니다. "
    "더 작은 모델이나 imgsz를 선택하세요."
)


@dataclass(frozen=True)
class RunConfig:
    run_id: int
    owner_id: int
    weights: str
    epochs: int
    imgsz: int
    batch: int
    training_args: dict[str, object]
    out_dir: Path


class AutoBatchFloorError(RuntimeError):
    """Raised before the first epoch when automatic batch is unusably small."""


def database_dsn() -> str:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required by the training worker")
    return database_url.replace("+asyncpg", "", 1)


def load_run_config(run_id: int, owner_id: int, dsn: str) -> RunConfig:
    with psycopg.connect(
        dsn,
        connect_timeout=5,
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, owner_id, weights, epochs, imgsz, batch,
                       training_args, out_dir
                FROM training_runs
                WHERE id=%s AND owner_id=%s AND state='running'
                """,
                (run_id, owner_id),
            )
            row = cursor.fetchone()
    if row is None:
        raise LookupError(f"training run {run_id} is unavailable")
    return RunConfig(
        run_id=row["id"],
        owner_id=row["owner_id"],
        weights=row["weights"],
        epochs=row["epochs"],
        imgsz=row["imgsz"],
        batch=row["batch"],
        training_args=normalize_training_args(row["training_args"]),
        out_dir=contained_storage_path(
            get_settings().storage_dir,
            row["out_dir"],
        ),
    )


def make_autobatch_floor_callback(
    requested_batch: int,
) -> Callable[[Any], None]:
    def enforce_floor(trainer: Any) -> None:
        if requested_batch != -1:
            return
        effective_batch = int(trainer.batch_size)
        if effective_batch < AUTOBATCH_FLOOR:
            raise AutoBatchFloorError(
                AUTOBATCH_FLOOR_DETAIL.format(batch=effective_batch)
            )

    return enforce_floor


def mark_run_failed(
    run_id: int,
    owner_id: int,
    dsn: str,
    error: str,
    *,
    out_dir: Path | None = None,
    storage_dir: Path | None = None,
) -> bool:
    """Compatibility wrapper around the shared idempotent failure recorder."""
    return persist_worker_failure(
        run_id,
        owner_id,
        dsn,
        FailureReport(
            reason=error,
            is_oom=False,
            effective_batch=None,
            exit_code=None,
        ),
        out_dir=out_dir,
        storage_dir=storage_dir,
    )


def complete_run(
    run_id: int,
    owner_id: int,
    dsn: str,
    out_dir: Path,
    trainer: Any,
    *,
    storage_dir: Path | None = None,
) -> bool:
    artifacts = out_dir / "artifacts"
    artifacts.mkdir(exist_ok=True)
    sources = {
        "best.pt": Path(trainer.best),
        "last.pt": Path(trainer.last),
        "results.csv": Path(trainer.csv),
    }
    missing = [
        name
        for name, path in sources.items()
        if not path.is_file() and not (artifacts / name).is_file()
    ]
    if missing:
        raise FileNotFoundError(
            "training completed without required artifacts: " + ", ".join(missing)
        )
    artifact_bytes = collect_run_artifacts(out_dir, sources=sources)

    staged = stage_run_workdir(
        storage_dir or get_settings().storage_dir,
        out_dir,
    )
    try:
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE training_runs
                    SET state='done',
                        finished_at=COALESCE(finished_at, now()),
                        artifact_bytes=%s
                    WHERE id=%s AND owner_id=%s AND state='running'
                    """,
                    (artifact_bytes, run_id, owner_id),
                )
                updated = cursor.rowcount == 1
                if updated:
                    increase_bytes_used_sync(
                        cursor,
                        owner_id,
                        artifact_bytes,
                    )
    except BaseException:
        restore_staged_deletion(staged)
        raise
    if not updated:
        restore_staged_deletion(staged)
        return False
    finalize_staged_deletion(staged)
    return True


def run_training(run_id: int, owner_id: int, dsn: str) -> None:
    config: RunConfig | None = None
    try:
        config = load_run_config(run_id, owner_id, dsn)
        data_yaml = (config.out_dir / "workdir" / "data.yaml").resolve()
        weights_path = (WEIGHTS_DIR / config.weights).resolve()
        model = YOLO(str(weights_path))
        model.add_callback(
            "on_pretrain_routine_end",
            make_autobatch_floor_callback(config.batch),
        )
        model.add_callback(
            "on_fit_epoch_end",
            make_epoch_callback(run_id, config.owner_id, dsn),
        )
        model.train(
            data=str(data_yaml),
            epochs=config.epochs,
            imgsz=config.imgsz,
            batch=config.batch,
            **ultralytics_training_args(config.training_args),
            project=str((config.out_dir / "workdir").resolve()),
            name="train",
            exist_ok=True,
        )
        complete_run(
            run_id,
            config.owner_id,
            dsn,
            config.out_dir,
            model.trainer,
        )
    except Exception as error:
        try:
            report = classify_failure(
                error=error,
                out_dir=config.out_dir if config is not None else None,
            )
            mark_run_failed(
                run_id,
                owner_id,
                dsn,
                report.reason,
                out_dir=config.out_dir if config is not None else None,
                storage_dir=get_settings().storage_dir,
            )
        except Exception as write_error:
            print(f"failed to persist worker error: {write_error}", flush=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--owner-id", type=int, required=True)
    args = parser.parse_args()
    run_training(args.run_id, args.owner_id, database_dsn())


if __name__ == "__main__":
    main()
