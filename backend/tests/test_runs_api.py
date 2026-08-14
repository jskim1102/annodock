from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import event

from app.models import Dataset, Project, RunImage, RunMetric, TrainingRun
from app.training_params import LEGACY_TRAINING_ARGS


pytestmark = pytest.mark.asyncio


def _run(
    out_dir: Path,
    *,
    dataset_id: int | None = None,
    state: str = "done",
    split_mode: str = "2way",
    artifacts_deleted_at: datetime | None = None,
) -> TrainingRun:
    return TrainingRun(
        owner_id=1,
        dataset_id=dataset_id,
        dataset_name=f"test-runs-api-{uuid4().hex}",
        weights="yolo26n.pt",
        epochs=10,
        imgsz=640,
        batch=4,
        split_mode=split_mode,
        ratios=(
            {"train": 0.7, "valid": 0.2, "test": 0.1}
            if split_mode == "3way"
            else {"train": 0.8, "valid": 0.2}
        ),
        seed=17,
        state=state,
        started_at=datetime.now(timezone.utc),
        finished_at=(
            datetime.now(timezone.utc)
            if state in {"done", "failed", "canceled"}
            else None
        ),
        out_dir=str(out_dir),
        error="test failure" if state == "failed" else None,
        artifacts_deleted_at=artifacts_deleted_at,
    )


async def _persist_run(
    app,
    run: TrainingRun,
    *,
    metric_epochs: tuple[int, ...] = (),
    image_splits: tuple[str, ...] = (),
) -> int:
    async with app.state.session_factory() as session:
        session.add(run)
        await session.flush()
        for epoch in metric_epochs:
            session.add(
                RunMetric(
                    run_id=run.id,
                    epoch=epoch,
                    box_loss=epoch / 10,
                    cls_loss=epoch / 20,
                    dfl_loss=None,
                    map50=epoch / 100,
                    map5095=None,
                    lr={"lr/pg0": epoch / 1000, "lr/pg1": epoch / 2000},
                )
            )
        for index, split in enumerate(image_splits, start=1):
            session.add(
                RunImage(
                    run_id=run.id,
                    image_id=None,
                    split=split,
                    stem=f"snapshot-{index}",
                    filename=f"snapshot-{index}.jpg",
                    rel_path=f"snapshot-{index}.jpg",
                )
            )
        await session.commit()
        return run.id


async def test_runs_api_list_aggregates_epoch_without_n_plus_one(
    client,
    app,
    tmp_path: Path,
) -> None:
    async with app.state.session_factory() as session:
        project = Project(
            owner_id=1,
            name=f"test-runs-api-project-{uuid4().hex}",
        )
        session.add(project)
        await session.flush()
        dataset = Dataset(
            owner_id=1,
            project_id=project.id,
            name=f"test-runs-api-dataset-{uuid4().hex}",
            status="ready",
            storage_path=str(tmp_path / "dataset"),
            image_count=0,
            annotation_count=0,
            class_count=0,
        )
        session.add(dataset)
        await session.commit()
        dataset_id = dataset.id

    run_ids = [
        await _persist_run(
            app,
            _run(tmp_path / "one", dataset_id=dataset_id),
            metric_epochs=(1, 3),
        ),
        await _persist_run(
            app,
            _run(tmp_path / "two", dataset_id=dataset_id, state="failed"),
        ),
        await _persist_run(app, _run(tmp_path / "deleted-dataset")),
    ]
    statements: list[str] = []

    def count_selects(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", count_selects)
    try:
        response = await client.get("/api/runs?offset=0&limit=200")
    finally:
        event.remove(
            app.state.engine.sync_engine,
            "before_cursor_execute",
            count_selects,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert {item["id"] for item in body["items"]} == set(run_ids)
    by_id = {item["id"]: item for item in body["items"]}
    assert by_id[run_ids[0]]["epoch"] == 3
    assert by_id[run_ids[1]]["epoch"] == 0
    assert by_id[run_ids[2]]["dataset_id"] is None
    assert set(by_id[run_ids[0]]) == {
        "id",
        "dataset_id",
        "dataset_name",
        "weights",
        "state",
        "epochs",
        "epoch",
        "started_at",
        "finished_at",
        "artifacts_deleted_at",
    }
    assert len(statements) == 2
    assert sum("run_metrics" in statement for statement in statements) == 1

    filtered = await client.get(
        f"/api/runs?dataset_id={dataset_id}&offset=0&limit=1"
    )
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 2
    assert len(filtered.json()["items"]) == 1


async def test_runs_api_detail_and_metrics_preserve_contract_order(
    client,
    app,
    tmp_path: Path,
) -> None:
    run_id = await _persist_run(
        app,
        _run(tmp_path / "detail", state="failed"),
        metric_epochs=(3, 1, 2),
        image_splits=("train", "train", "valid"),
    )

    detail = await client.get(f"/api/runs/{run_id}")
    metrics = await client.get(f"/api/runs/{run_id}/metrics")

    assert detail.status_code == 200
    assert detail.json() == {
        "id": run_id,
        "dataset_id": None,
        "dataset_name": detail.json()["dataset_name"],
        "weights": "yolo26n.pt",
        "state": "failed",
        "epochs": 10,
        "epoch": 3,
        "started_at": detail.json()["started_at"],
        "finished_at": detail.json()["finished_at"],
        "artifacts_deleted_at": None,
        "imgsz": 640,
        "batch": 4,
        "split_mode": "2way",
        "ratios": {"train": 0.8, "valid": 0.2},
        "seed": 17,
        "training_args": LEGACY_TRAINING_ARGS,
        "error": "test failure",
        "image_counts": {"train": 2, "valid": 1, "test": 0},
    }
    assert metrics.status_code == 200
    assert [row["epoch"] for row in metrics.json()] == [1, 2, 3]
    assert metrics.json()[1]["lr"] == {"lr/pg0": 0.002, "lr/pg1": 0.001}

    missing = await client.get("/api/runs/999999999")
    assert missing.status_code == 404


async def test_runs_api_detail_counts_three_way_run_images(
    client,
    app,
    tmp_path: Path,
) -> None:
    run_id = await _persist_run(
        app,
        _run(tmp_path / "three-way-detail", split_mode="3way"),
        image_splits=("train", "train", "valid", "test", "test"),
    )

    detail = await client.get(f"/api/runs/{run_id}")

    assert detail.status_code == 200
    assert detail.json()["split_mode"] == "3way"
    assert detail.json()["image_counts"] == {
        "train": 2,
        "valid": 1,
        "test": 2,
    }


async def test_runs_api_log_missing_is_empty_200_and_tail_is_bounded(
    client,
    app,
    tmp_path: Path,
) -> None:
    out_dir = app.state.settings.storage_dir / "training-runs" / uuid4().hex
    run_id = await _persist_run(app, _run(out_dir, state="running"))

    missing_log = await client.get(f"/api/runs/{run_id}/log?tail=2")
    assert missing_log.status_code == 200
    assert missing_log.text == ""

    log_path = out_dir / "artifacts" / "log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")
    tail = await client.get(f"/api/runs/{run_id}/log?tail=2")
    assert tail.status_code == 200
    assert tail.text == "line3\nline4"


async def test_runs_api_artifact_allowlist_and_symlink_guards(
    client,
    app,
    tmp_path: Path,
) -> None:
    out_dir = app.state.settings.storage_dir / "training-runs" / uuid4().hex
    artifacts = out_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "results.csv").write_bytes(b"epoch,map50\n1,0.5\n")
    secret = tmp_path / "secret.pt"
    secret.write_bytes(b"not an artifact")
    (artifacts / "best.pt").symlink_to(secret)
    run_id = await _persist_run(app, _run(out_dir))

    allowed = await client.get(f"/api/runs/{run_id}/artifacts/results.csv")
    assert allowed.status_code == 200
    assert allowed.content == b"epoch,map50\n1,0.5\n"
    assert "attachment" in allowed.headers["content-disposition"]

    rejected_paths = [
        f"/api/runs/{run_id}/artifacts/evil.pt",
        f"/api/runs/{run_id}/artifacts/%2E%2E%2Fetc%2Fpasswd",
        f"/api/runs/{run_id}/artifacts/",
        f"/api/runs/{run_id}/artifacts/best.pt",
    ]
    for path in rejected_paths:
        response = await client.get(path)
        assert response.status_code == 404, path

    deleted_out = tmp_path / "deleted-artifact-run"
    deleted_artifacts = deleted_out / "artifacts"
    deleted_artifacts.mkdir(parents=True)
    (deleted_artifacts / "last.pt").write_bytes(b"last")
    deleted_id = await _persist_run(
        app,
        _run(
            deleted_out,
            artifacts_deleted_at=datetime.now(timezone.utc),
        ),
    )
    deleted = await client.get(f"/api/runs/{deleted_id}/artifacts/last.pt")
    assert deleted.status_code == 410
