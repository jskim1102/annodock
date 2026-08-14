"""Ultralytics callbacks that persist one metric row per completed epoch."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

import psycopg
from psycopg.types.json import Json


METRIC_UPSERT = """
INSERT INTO run_metrics (
    run_id, epoch, box_loss, cls_loss, dfl_loss, map50, map5095, lr
) SELECT %s, %s, %s, %s, %s, %s, %s, %s
FROM training_runs AS owned_run
WHERE owned_run.id=%s
  AND owned_run.owner_id=%s
  AND owned_run.state='running'
ON CONFLICT (run_id, epoch) DO UPDATE SET
    box_loss = EXCLUDED.box_loss,
    cls_loss = EXCLUDED.cls_loss,
    dfl_loss = EXCLUDED.dfl_loss,
    map50 = EXCLUDED.map50,
    map5095 = EXCLUDED.map5095,
    lr = EXCLUDED.lr
"""


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def make_epoch_callback(
    run_id: int,
    owner_id: int,
    dsn: str,
) -> Callable[[Any], None]:
    """Build a failure-isolated on_fit_epoch_end metric writer."""

    def persist_epoch(trainer: Any) -> None:
        try:
            if "val/box_loss" not in trainer.metrics:
                return
            raw_losses = trainer.label_loss_items(trainer.tloss)
            losses = raw_losses if isinstance(raw_losses, Mapping) else {}
            metrics = trainer.metrics if isinstance(trainer.metrics, Mapping) else {}
            raw_lr = trainer.lr if isinstance(trainer.lr, Mapping) else {}
            lr = {str(key): float(value) for key, value in raw_lr.items()}
            parameters = (
                run_id,
                int(trainer.epoch) + 1,
                _optional_float(losses.get("train/box_loss")),
                _optional_float(losses.get("train/cls_loss")),
                _optional_float(losses.get("train/dfl_loss")),
                _optional_float(metrics.get("metrics/mAP50(B)")),
                _optional_float(metrics.get("metrics/mAP50-95(B)")),
                Json(lr),
                run_id,
                owner_id,
            )
            with psycopg.connect(dsn, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(METRIC_UPSERT, parameters)
        except Exception as error:
            print(f"epoch metric write failed: {error}", flush=True)

    return persist_epoch
