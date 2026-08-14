from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import psycopg
import pytest
from sqlalchemy import func, select

from app.inference.models_dir import PRESET_MODELS
from app.models import Annotation, Dataset, DatasetClass, Image, RunImage, TrainingRun
from app.services.proc_identity import ProcessIdentity, parse_proc_stat, read_process_identity
from app.services.storage import contained_storage_path, storage_relative_path
from app.services import training
from app.worker import callbacks, train_worker


pytestmark = pytest.mark.asyncio


def _name(suffix: str) -> str:
    return f"test-training-{suffix}-{uuid4().hex}"


async def _dataset(
    client: httpx.AsyncClient,
    app,
    *,
    count: int = 10,
    class_ids: tuple[int, ...] = (0, 1),
    annotation_class_ids: tuple[int, ...] | None = None,
    annotated: bool = True,
    annotated_count: int | None = None,
    label_source_count: int | None = None,
) -> int:
    created = await client.post("/api/datasets", json={"name": _name("data")})
    assert created.status_code == 201
    dataset_id = created.json()["id"]

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        root = contained_storage_path(
            app.state.settings.storage_dir,
            dataset.storage_path,
        )
        root.mkdir(parents=True, exist_ok=True)
        session.add_all(
            DatasetClass(dataset_id=dataset_id, class_id=class_id, name=f"class-{class_id}")
            for class_id in class_ids
        )
        label_class_ids = (
            class_ids if annotation_class_ids is None else annotation_class_ids
        )
        images: list[Image] = []
        for index in range(count):
            image_is_annotated = annotated and (
                annotated_count is None or index < annotated_count
            )
            source = root / f"image {index}.jpg"
            source.write_bytes(f"image-{index}".encode())
            image = Image(
                dataset_id=dataset_id,
                stem=f"image {index}",
                filename=source.name,
                rel_path=f"incoming/{source.name}",
                split=None,
                width=32,
                height=24,
                file_path=storage_relative_path(
                    app.state.settings.storage_dir,
                    source,
                ),
                display_path=None,
                thumb_path=storage_relative_path(
                    app.state.settings.storage_dir,
                    root / f"thumb-{index}.jpg",
                ),
                box_count=len(label_class_ids) if image_is_annotated else 0,
                has_label_source=(
                    label_source_count is None or index < label_source_count
                ),
            )
            session.add(image)
            await session.flush()
            images.append(image)
            if image_is_annotated:
                for class_id in label_class_ids:
                    session.add(
                        Annotation(
                            image_id=image.id,
                            class_id=class_id,
                            cx=0.5,
                            cy=0.5,
                            w=0.25,
                            h=0.25,
                        )
                    )
        dataset.image_count = count
        labeled_images = (
            count if annotated_count is None else min(count, annotated_count)
        ) if annotated else 0
        dataset.annotation_count = labeled_images * len(label_class_ids)
        dataset.class_count = len(class_ids)
        dataset.status = "ready"
        await session.commit()
    return dataset_id


def _host_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(training, "is_container_environment", lambda: False)
    monkeypatch.setattr(training.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(
        training.torch.cuda,
        "mem_get_info",
        lambda: (24 * 1024**3, 24 * 1024**3),
    )
    monkeypatch.setattr(
        training,
        "spawn_worker",
        lambda _run_id, _owner_id, _out_dir, _database_url: training.SpawnedWorker(
            pid=4242,
            pid_started_at="123456",
            boot_id="test-boot-id",
        ),
    )


def _body(**overrides) -> dict:
    body = {
        "weights": "yolo26n.pt",
        "epochs": 3,
        "imgsz": 640,
        "batch": -1,
    }
    body.update(overrides)
    return body


RTX_3090_TRAINING_ARGS = {
    "device": 0,
    "optimizer": "auto",
    "lr0": 0.01,
    "lrf": 0.01,
    "warmup_epochs": 3.0,
    "cos_lr": True,
    "patience": 30,
    "augment": True,
    "mosaic": 1.0,
    "mixup": 0.0,
    "copy_paste": 0.0,
    "close_mosaic": 10,
    "hsv_h": 0.015,
    "hsv_s": 0.7,
    "hsv_v": 0.4,
    "fliplr": 0.5,
    "scale": 0.5,
    "translate": 0.1,
    "workers": 8,
    "cache": "ram",
    "amp": True,
    "compile": True,
    "deterministic": False,
    "save_period": 25,
    "multi_scale": 0.0,
    "exclude_unlabeled_images": False,
    "include_unlabeled_images_in_test": False,
}


async def test_models_list_returns_only_the_five_presets(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/models")

    assert response.status_code == 200
    assert response.json() == [
        {"name": name, "type": "preset", "size_mb": None}
        for name in PRESET_MODELS
    ]


async def test_training_recommendation_uses_owned_dataset_statistics(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id = await _dataset(client, app, count=10)

    response = await client.get(
        f"/api/datasets/{dataset_id}/training-recommendation",
        params={
            "weights": "yolo26s.pt",
            "imgsz": 640,
            "multi_scale": 0,
            "train_ratio": 0.7,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["policy_version"] == "rtx3090-detect-v1"
    assert body["train_images"] == 7
    assert body["total_instances"] == 20
    assert body["instances_per_image"] == 2.0
    assert body["small_object_ratio"] == 0.0
    assert body["epochs"] == 250
    assert body["batch"] == 32
    assert body["copy_paste"] == 0.0
    assert body["close_mosaic"] == 10
    assert body["total_images"] == 10
    assert body["labeled_images"] == 10
    assert body["unlabeled_images"] == 0


async def test_training_recommendation_can_use_only_labeled_images(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id = await _dataset(
        client,
        app,
        count=10,
        annotated_count=6,
        label_source_count=6,
    )

    response = await client.get(
        f"/api/datasets/{dataset_id}/training-recommendation",
        params={
            "weights": "yolo26s.pt",
            "imgsz": 640,
            "multi_scale": 0,
            "train_ratio": 0.7,
            "exclude_unlabeled_images": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total_images"] == 10
    assert body["labeled_images"] == 6
    assert body["unlabeled_images"] == 4
    assert body["train_images"] == 4


async def test_train_validation_rejects_invalid_payloads(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_ready(monkeypatch)
    dataset_id = await _dataset(client, app)

    invalid_bodies = [
        _body(weights="untrusted.pt"),
        _body(epochs=0),
        _body(imgsz=0),
        _body(batch=0),
        _body(batch=-2),
        _body(split_mode="four-way"),
        _body(ratios={"train": 0.7, "valid": 0.2, "test": 0.1}),
        _body(ratios={"train": 0.7, "valid": 0.4}),
    ]
    for body in invalid_bodies:
        response = await client.post(f"/api/datasets/{dataset_id}/train", json=body)
        assert response.status_code == 422, body


async def test_train_validation_defaults_and_seed_are_persisted(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_ready(monkeypatch)
    dataset_id = await _dataset(client, app)

    response = await client.post(
        f"/api/datasets/{dataset_id}/train",
        json=_body(batch=2),
    )

    assert response.status_code == 201
    assert response.json()["warnings"] == []
    async with app.state.session_factory() as session:
        run = await session.get(TrainingRun, response.json()["run_id"])
        assert run is not None
        assert run.batch == 2
        assert run.split_mode == "2way"
        assert run.ratios == {"train": 0.8, "valid": 0.2}
        assert isinstance(run.seed, int)
        assert run.training_args == RTX_3090_TRAINING_ARGS
        assert run.state == "running"
        assert run.out_dir == f"training-runs/{run.id}"
        assert contained_storage_path(
            app.state.settings.storage_dir,
            run.out_dir,
        ).is_dir()
        split_counts = dict(
            (
                await session.execute(
                    select(RunImage.split, func.count(RunImage.id))
                    .where(RunImage.run_id == run.id)
                    .group_by(RunImage.split)
                )
            ).all()
        )
        assert split_counts == {"train": 8, "valid": 2}


async def test_train_custom_arguments_are_validated_and_persisted(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_ready(monkeypatch)
    dataset_id = await _dataset(client, app)
    custom = {
        **RTX_3090_TRAINING_ARGS,
        "optimizer": "MuSGD",
        "lr0": 0.02,
        "workers": 12,
        "cache": "disk",
        "compile": False,
        "multi_scale": 0.5,
    }

    response = await client.post(
        f"/api/datasets/{dataset_id}/train",
        json=_body(**custom),
    )

    assert response.status_code == 201
    async with app.state.session_factory() as session:
        run = await session.get(TrainingRun, response.json()["run_id"])
        assert run is not None
        assert run.training_args == custom


async def test_train_excludes_unlabeled_images_from_run_snapshot(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_ready(monkeypatch)
    dataset_id = await _dataset(
        client,
        app,
        count=10,
        annotated_count=6,
        label_source_count=7,
    )

    response = await client.post(
        f"/api/datasets/{dataset_id}/train",
        json=_body(batch=2, exclude_unlabeled_images=True),
    )

    assert response.status_code == 201
    async with app.state.session_factory() as session:
        run = await session.get(TrainingRun, response.json()["run_id"])
        assert run is not None
        assert run.training_args["exclude_unlabeled_images"] is True
        rows = (
            await session.execute(
                select(RunImage, func.count(Annotation.id))
                .join(Image, Image.id == RunImage.image_id)
                .outerjoin(Annotation, Annotation.image_id == Image.id)
                .where(RunImage.run_id == run.id)
                .group_by(RunImage.id)
            )
        ).all()
        assert len(rows) == 7
        assert sum(annotation_count == 0 for _, annotation_count in rows) == 1


async def test_train_assigns_missing_label_images_only_to_test(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_ready(monkeypatch)
    dataset_id = await _dataset(
        client,
        app,
        count=10,
        annotated_count=6,
        label_source_count=7,
    )

    response = await client.post(
        f"/api/datasets/{dataset_id}/train",
        json=_body(
            batch=2,
            split_mode="3way",
            ratios={"train": 0.7, "valid": 0.2, "test": 0.1},
            exclude_unlabeled_images=True,
            include_unlabeled_images_in_test=True,
        ),
    )

    assert response.status_code == 201
    async with app.state.session_factory() as session:
        run = await session.get(TrainingRun, response.json()["run_id"])
        assert run is not None
        assert run.training_args["include_unlabeled_images_in_test"] is True
        rows = (
            await session.execute(
                select(RunImage.split, Image.has_label_source, func.count(Annotation.id))
                .join(Image, Image.id == RunImage.image_id)
                .outerjoin(Annotation, Annotation.image_id == Image.id)
                .where(RunImage.run_id == run.id)
                .group_by(RunImage.id, Image.has_label_source)
            )
        ).all()
        assert len(rows) == 10
        assert {split for split, has_source, count in rows if not has_source and count == 0} == {"test"}
        assert sum(not has_source and count == 0 for _, has_source, count in rows) == 3


async def test_unlabeled_test_policy_requires_exclusion_and_three_way_mode(
    client: httpx.AsyncClient,
    app,
) -> None:
    dataset_id = await _dataset(client, app)

    two_way = await client.post(
        f"/api/datasets/{dataset_id}/train",
        json=_body(
            exclude_unlabeled_images=True,
            include_unlabeled_images_in_test=True,
        ),
    )
    missing_parent = await client.post(
        f"/api/datasets/{dataset_id}/train",
        json=_body(
            split_mode="3way",
            ratios={"train": 0.7, "valid": 0.2, "test": 0.1},
            include_unlabeled_images_in_test=True,
        ),
    )

    assert two_way.status_code == 422
    assert missing_parent.status_code == 422


async def test_train_rejects_invalid_or_incompatible_training_arguments(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_ready(monkeypatch)
    dataset_id = await _dataset(client, app)
    invalid = [
        {"device": 1},
        {"optimizer": "unknown"},
        {"lr0": 0},
        {"mixup": 1.1},
        {"copy_paste": 0.1},
        {"workers": -1},
        {"save_period": 0},
        {"compile": True, "multi_scale": 0.5},
    ]

    for overrides in invalid:
        response = await client.post(
            f"/api/datasets/{dataset_id}/train",
            json=_body(**overrides),
        )
        assert response.status_code == 422, overrides


async def test_train_validation_host_and_dataset_preflight_errors(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dataset_id = await _dataset(client, app)

    monkeypatch.setattr(training, "is_container_environment", lambda: True)
    container = await client.post(
        f"/api/datasets/{dataset_id}/train", json=_body()
    )
    assert container.status_code == 503
    assert container.json()["detail"] == training.CONTAINER_DETAIL

    monkeypatch.setattr(training, "is_container_environment", lambda: False)
    monkeypatch.setattr(training.torch.cuda, "is_available", lambda: False)
    no_gpu = await client.post(f"/api/datasets/{dataset_id}/train", json=_body())
    assert no_gpu.status_code == 400
    assert no_gpu.json()["detail"] == training.GPU_DETAIL

    _host_ready(monkeypatch)
    gapped_id = await _dataset(client, app, class_ids=(0, 2))
    gapped = await client.post(f"/api/datasets/{gapped_id}/train", json=_body())
    assert gapped.status_code == 400
    assert gapped.json()["detail"] == training.GAPPED_CLASS_DETAIL

    small_id = await _dataset(client, app, count=4)
    small = await client.post(f"/api/datasets/{small_id}/train", json=_body())
    assert small.status_code == 400
    assert small.json()["detail"] == training.SMALL_DATASET_DETAIL

    monkeypatch.setattr(training, "WEIGHTS_DIR", tmp_path / "missing-weights")
    monkeypatch.setattr(training, "is_online", lambda: False)
    offline = await client.post(
        f"/api/datasets/{dataset_id}/train", json=_body()
    )
    assert offline.status_code == 400
    assert offline.json()["detail"] == training.OFFLINE_DETAIL


async def test_gapped_annotation_class_range_rejects_used_id_outside_metadata(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_ready(monkeypatch)
    dataset_id = await _dataset(
        client,
        app,
        class_ids=(0, 1),
        annotation_class_ids=(0, 1, 99),
    )

    response = await client.post(
        f"/api/datasets/{dataset_id}/train",
        json=_body(),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == training.GAPPED_CLASS_DETAIL
    async with app.state.session_factory() as session:
        run_count = await session.scalar(
            select(func.count(TrainingRun.id)).where(
                TrainingRun.dataset_id == dataset_id
            )
        )
    assert run_count == 0


async def test_gapped_configured_gap_is_not_masked_by_annotations(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_ready(monkeypatch)
    dataset_id = await _dataset(
        client,
        app,
        class_ids=(0, 2),
        annotation_class_ids=(0, 1, 2),
    )

    response = await client.post(
        f"/api/datasets/{dataset_id}/train",
        json=_body(),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == training.GAPPED_CLASS_DETAIL
    async with app.state.session_factory() as session:
        run_count = await session.scalar(
            select(func.count(TrainingRun.id)).where(
                TrainingRun.dataset_id == dataset_id
            )
        )
    assert run_count == 0


async def test_gapped_annotation_class_range_allows_normal_dataset(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_ready(monkeypatch)
    dataset_id = await _dataset(
        client,
        app,
        class_ids=(0, 1),
        annotation_class_ids=(0, 1),
    )

    response = await client.post(
        f"/api/datasets/{dataset_id}/train",
        json=_body(),
    )

    assert response.status_code == 201
    assert response.json()["warnings"] == []


async def test_active_run_409_is_enforced_by_concurrent_database_inserts(
    app,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers,
) -> None:
    _host_ready(monkeypatch)
    transport = httpx.ASGITransport(app=app)
    headers = auth_headers(1)
    async with (
        httpx.AsyncClient(
            transport=transport,
            base_url="http://one",
            headers=headers,
        ) as first,
        httpx.AsyncClient(
            transport=transport,
            base_url="http://two",
            headers=headers,
        ) as second,
    ):
        dataset_id = await _dataset(first, app)
        responses = await asyncio.gather(
            first.post(f"/api/datasets/{dataset_id}/train", json=_body()),
            second.post(f"/api/datasets/{dataset_id}/train", json=_body()),
        )

    assert sorted(response.status_code for response in responses) == [201, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["detail"] == training.ACTIVE_RUN_DETAIL
    async with app.state.session_factory() as session:
        runs = (
            await session.scalars(
                select(TrainingRun).where(TrainingRun.dataset_id == dataset_id)
            )
        ).all()
        assert len(runs) == 1
        assert runs[0].state == "running"


async def test_warnings_can_include_vram_and_background_together(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_ready(monkeypatch)
    monkeypatch.setattr(
        training.torch.cuda,
        "mem_get_info",
        lambda: (4 * 1024**3, 24 * 1024**3),
    )
    dataset_id = await _dataset(
        client,
        app,
        count=10,
        class_ids=(0,),
        annotated=False,
    )

    response = await client.post(
        f"/api/datasets/{dataset_id}/train", json=_body(seed=42)
    )

    assert response.status_code == 201
    warnings = response.json()["warnings"]
    assert len(warnings) == 2
    assert any("GPU 메모리" in warning for warning in warnings)
    assert any("valid" in warning and "background" in warning for warning in warnings)


async def test_worker_spawn_uses_detached_argv_and_inherited_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_dir = tmp_path / "run with spaces"
    (out_dir / "artifacts").mkdir(parents=True)
    observed: dict[str, object] = {}

    class FakeProcess:
        pid = 8123

    def fake_popen(argv, **kwargs):
        observed["argv"] = argv
        observed.update(kwargs)
        assert not kwargs["stdout"].closed
        return FakeProcess()

    monkeypatch.setenv("WORKER_PARENT_MARKER", "preserved")
    monkeypatch.setattr(training.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        training,
        "read_process_identity",
        lambda pid: ProcessIdentity(
            pid=pid,
            state="S",
            started_at="88033929",
            boot_id="boot-without-newline",
        ),
    )

    spawned = training.spawn_worker(
        17,
        171,
        out_dir,
        "postgresql+asyncpg://postgres:postgres@localhost/test_db",
    )

    assert observed["argv"] == [
        sys.executable,
        "-u",
        "-m",
        "app.worker.train_worker",
        "--run-id",
        "17",
        "--owner-id",
        "171",
    ]
    assert observed["start_new_session"] is True
    assert observed["stderr"] is subprocess.STDOUT
    assert observed["cwd"] == training.BACKEND_ROOT
    environment = observed["env"]
    assert environment["WORKER_PARENT_MARKER"] == "preserved"
    assert environment["DATABASE_URL"].endswith("/test_db")
    assert (
        environment["PYTORCH_CUDA_ALLOC_CONF"]
        == "expandable_segments:True"
    )
    assert observed["stdout"].closed
    assert (out_dir / "artifacts" / "log").is_file()
    assert spawned == training.SpawnedWorker(
        pid=8123,
        pid_started_at="88033929",
        boot_id="boot-without-newline",
    )


async def test_worker_spawn_happens_after_commit_and_persists_identity(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_ready(monkeypatch)
    dataset_id = await _dataset(client, app)
    observed: dict[str, object] = {}

    def committed_spawn(
        run_id: int,
        owner_id: int,
        out_dir: Path,
        database_url: str,
    ):
        dsn = database_url.replace("+asyncpg", "", 1)
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT state, out_dir, owner_id FROM training_runs WHERE id=%s",
                    (run_id,),
                )
                observed["row"] = cursor.fetchone()
        observed["out_dir"] = out_dir
        return training.SpawnedWorker(
            pid=9001,
            pid_started_at="88033929",
            boot_id="boot-id",
        )

    monkeypatch.setattr(training, "spawn_worker", committed_spawn)

    response = await client.post(
        f"/api/datasets/{dataset_id}/train",
        json=_body(batch=2),
    )

    assert response.status_code == 201
    assert observed["row"] == (
        "running",
        storage_relative_path(
            app.state.settings.storage_dir,
            observed["out_dir"],
        ),
        1,
    )
    async with app.state.session_factory() as session:
        run = await session.get(TrainingRun, response.json()["run_id"])
        assert run is not None
        assert (run.pid, run.pid_started_at, run.boot_id) == (
            9001,
            "88033929",
            "boot-id",
        )


async def test_worker_identity_write_is_owner_scoped(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_ready(monkeypatch)
    dataset_id = await _dataset(client, app)

    def ownership_changed_spawn(
        run_id: int,
        _owner_id: int,
        _out_dir: Path,
        database_url: str,
    ) -> training.SpawnedWorker:
        dsn = database_url.replace("+asyncpg", "", 1)
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE training_runs SET owner_id=%s WHERE id=%s",
                    (2, run_id),
                )
        return training.SpawnedWorker(
            pid=9002,
            pid_started_at="88033930",
            boot_id="other-owner-boot",
        )

    monkeypatch.setattr(training, "spawn_worker", ownership_changed_spawn)

    response = await client.post(
        f"/api/datasets/{dataset_id}/train",
        json=_body(batch=2),
    )

    assert response.status_code == 201
    async with app.state.session_factory() as session:
        run = await session.get(TrainingRun, response.json()["run_id"])
        assert run is not None
        assert run.owner_id == 2
        assert (run.pid, run.pid_started_at, run.boot_id) == (None, None, None)


async def test_worker_spawn_failure_marks_committed_run_failed(
    client: httpx.AsyncClient,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_ready(monkeypatch)
    dataset_id = await _dataset(client, app)

    def fail_spawn(
        _run_id: int,
        _owner_id: int,
        _out_dir: Path,
        _database_url: str,
    ):
        raise OSError("simulated Popen failure")

    monkeypatch.setattr(training, "spawn_worker", fail_spawn)

    response = await client.post(
        f"/api/datasets/{dataset_id}/train",
        json=_body(batch=2),
    )

    assert response.status_code == 500
    async with app.state.session_factory() as session:
        run = await session.scalar(
            select(TrainingRun).where(TrainingRun.dataset_id == dataset_id)
        )
        assert run is not None
        assert run.state == "failed"
        assert run.finished_at is not None
        assert "simulated Popen failure" in (run.error or "")


async def test_worker_spawn_identity_handles_complex_comm_and_zombie(
    tmp_path: Path,
) -> None:
    raw = "321 (sl (x) ee p) S " + " ".join(
        [str(index) for index in range(4, 22)] + ["88033929", "0"]
    )
    state, started_at = parse_proc_stat(raw)
    assert state == "S"
    assert started_at == "88033929"

    proc_root = tmp_path / "proc"
    process_dir = proc_root / "321"
    process_dir.mkdir(parents=True)
    (process_dir / "stat").write_text(raw.replace(") S ", ") Z "))
    boot_id_path = tmp_path / "boot_id"
    boot_id_path.write_text("boot-id-with-newline\n")
    assert (
        read_process_identity(
            321,
            proc_root=proc_root,
            boot_id_path=boot_id_path,
        )
        is None
    )


async def test_autobatch_floor_rejects_only_auto_batch_below_four() -> None:
    auto_callback = train_worker.make_autobatch_floor_callback(-1)
    with pytest.raises(train_worker.AutoBatchFloorError) as error:
        auto_callback(SimpleNamespace(batch_size=3))
    assert "batch 3" in str(error.value)

    explicit_callback = train_worker.make_autobatch_floor_callback(2)
    explicit_callback(SimpleNamespace(batch_size=2))


class _RecordingCursor:
    def __init__(self, observed: dict[str, object], before_execute=None) -> None:
        self.observed = observed
        self.before_execute = before_execute
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, statement, parameters=None) -> None:
        if self.before_execute is not None:
            self.before_execute()
        self.observed.setdefault("calls", []).append((statement, parameters))
        self.observed["statement"] = statement
        self.observed["parameters"] = parameters


class _RecordingConnection:
    def __init__(self, cursor: _RecordingCursor) -> None:
        self.recording_cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def cursor(self) -> _RecordingCursor:
        return self.recording_cursor


async def test_epoch_callback_upserts_one_based_metrics_and_all_lr_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    cursor = _RecordingCursor(observed)
    connection = _RecordingConnection(cursor)
    monkeypatch.setattr(
        callbacks.psycopg,
        "connect",
        lambda *_args, **_kwargs: connection,
    )
    trainer = SimpleNamespace(
        epoch=4,
        tloss=[0.11, 0.22, 0.33],
        metrics={
            "val/box_loss": 0.4,
            "metrics/mAP50(B)": 0.77,
            "metrics/mAP50-95(B)": 0.55,
        },
        lr={"lr/pg0": 0.01, "lr/pg1": 0.02, "lr/pg2": 0.03},
        label_loss_items=lambda _loss: {
            "train/box_loss": 0.11,
            "train/cls_loss": 0.22,
            "train/dfl_loss": 0.33,
        },
    )

    callback = callbacks.make_epoch_callback(7, 71, "postgresql://test")
    callback(trainer)

    statement = str(observed["statement"])
    parameters = observed["parameters"]
    assert "ON CONFLICT (run_id, epoch) DO UPDATE" in statement
    assert "owned_run.owner_id=%s" in statement
    assert "IS DISTINCT FROM" not in statement
    assert parameters[:7] == (7, 5, 0.11, 0.22, 0.33, 0.77, 0.55)
    assert parameters[7].obj == trainer.lr
    assert parameters[8:] == (7, 71)


async def test_epoch_callback_skips_final_eval_and_swallows_db_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = 0

    def fail_connect(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise OSError("database unavailable")

    monkeypatch.setattr(callbacks.psycopg, "connect", fail_connect)
    callback = callbacks.make_epoch_callback(8, 81, "postgresql://test")
    callback(SimpleNamespace(metrics={"metrics/mAP50(B)": 0.5}))
    assert calls == 0

    trainer = SimpleNamespace(
        epoch=0,
        tloss=[1.0, 2.0, 3.0],
        metrics={"val/box_loss": 1.0},
        lr={},
        label_loss_items=lambda _loss: {},
    )
    callback(trainer)
    assert calls == 1
    assert "epoch metric write failed" in capsys.readouterr().out


async def test_run_complete_moves_trainer_paths_before_conditional_done(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_dir = tmp_path / "storage"
    out_dir = storage_dir / "training-runs" / "9"
    artifacts = out_dir / "artifacts"
    source = out_dir / "workdir" / "train"
    weights = source / "weights"
    weights.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    best = weights / "best.pt"
    last = weights / "last.pt"
    csv = source / "results.csv"
    best.write_bytes(b"best")
    last.write_bytes(b"last")
    csv.write_text("epoch,metric\n1,0.5\n")
    observed: dict[str, object] = {}

    def assert_moved() -> None:
        assert (artifacts / "best.pt").read_bytes() == b"best"
        assert (artifacts / "last.pt").read_bytes() == b"last"
        assert (artifacts / "results.csv").is_file()

    cursor = _RecordingCursor(observed, before_execute=assert_moved)
    monkeypatch.setattr(
        train_worker.psycopg,
        "connect",
        lambda *_args, **_kwargs: _RecordingConnection(cursor),
    )
    trainer = SimpleNamespace(best=best, last=last, csv=csv)

    updated = train_worker.complete_run(
        9,
        91,
        "postgresql://test",
        out_dir,
        trainer,
        storage_dir=storage_dir,
    )

    assert updated is True
    calls = observed["calls"]
    assert "WHERE id=%s AND owner_id=%s AND state='running'" in str(calls[0][0])
    artifact_bytes = (
        len(b"best")
        + len(b"last")
        + len(b"epoch,metric\n1,0.5\n")
    )
    assert calls[0][1] == (artifact_bytes, 9, 91)
    assert "INSERT INTO user_storage" in str(calls[1][0])
    assert calls[1][1] == (91, artifact_bytes)
    assert not best.exists()
    assert not last.exists()
    assert not csv.exists()
    assert not (out_dir / "workdir").exists()
    assert sorted(path.name for path in artifacts.iterdir()) == [
        "best.pt",
        "last.pt",
        "results.csv",
    ]


async def test_worker_spawn_training_uses_absolute_paths_and_exact_save_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_dir = tmp_path / "run output"
    (out_dir / "workdir").mkdir(parents=True)
    (out_dir / "artifacts").mkdir()
    data_yaml = out_dir / "workdir" / "data.yaml"
    data_yaml.write_text("path: /tmp\n")
    config = train_worker.RunConfig(
        run_id=10,
        owner_id=101,
        weights="yolo26n.pt",
        epochs=2,
        imgsz=320,
        batch=2,
        training_args=RTX_3090_TRAINING_ARGS,
        out_dir=out_dir,
    )
    observed: dict[str, object] = {}

    class FakeYOLO:
        def __init__(self, weights_path: str) -> None:
            observed["weights"] = weights_path
            self.registered: dict[str, object] = {}

        def add_callback(self, event: str, callback) -> None:
            self.registered[event] = callback

        def train(self, **kwargs) -> None:
            observed["kwargs"] = kwargs
            save_dir = Path(kwargs["project"]) / kwargs["name"]
            weight_dir = save_dir / "weights"
            weight_dir.mkdir(parents=True)
            best = weight_dir / "best.pt"
            last = weight_dir / "last.pt"
            csv = save_dir / "results.csv"
            best.write_bytes(b"best")
            last.write_bytes(b"last")
            csv.write_text("epoch\n1\n")
            self.trainer = SimpleNamespace(best=best, last=last, csv=csv)

    monkeypatch.setattr(train_worker, "load_run_config", lambda *_args: config)
    monkeypatch.setattr(train_worker, "YOLO", FakeYOLO)
    monkeypatch.setattr(
        train_worker,
        "complete_run",
        lambda run_id, owner_id, dsn, path, trainer: observed.setdefault(
            "completed", (run_id, owner_id, dsn, path, trainer)
        ),
    )

    train_worker.run_training(10, 101, "postgresql://test")

    assert Path(observed["weights"]).is_absolute()
    kwargs = observed["kwargs"]
    worker_training_args = {
        key: value
        for key, value in RTX_3090_TRAINING_ARGS.items()
        if key not in {
            "exclude_unlabeled_images",
            "include_unlabeled_images_in_test",
        }
    }
    assert kwargs == {
        "data": str(data_yaml.resolve()),
        "epochs": 2,
        "imgsz": 320,
        "batch": 2,
        **worker_training_args,
        "project": str((out_dir / "workdir").resolve()),
        "name": "train",
        "exist_ok": True,
    }
    assert "val" not in kwargs
    assert observed["completed"][:3] == (
        10,
        101,
        "postgresql://test",
    )
    assert observed["completed"][3] == out_dir


async def test_autobatch_floor_marks_run_failed_before_first_epoch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_dir = tmp_path / "auto floor"
    (out_dir / "workdir").mkdir(parents=True)
    (out_dir / "workdir" / "data.yaml").write_text("path: /tmp\n")
    config = train_worker.RunConfig(
        run_id=11,
        owner_id=111,
        weights="yolo26n.pt",
        epochs=3,
        imgsz=640,
        batch=-1,
        training_args=RTX_3090_TRAINING_ARGS,
        out_dir=out_dir,
    )
    observed: dict[str, object] = {"epoch_started": False}

    class FloorYOLO:
        def __init__(self, _weights_path: str) -> None:
            self.registered: dict[str, object] = {}

        def add_callback(self, event: str, callback) -> None:
            self.registered[event] = callback

        def train(self, **_kwargs) -> None:
            self.registered["on_pretrain_routine_end"](
                SimpleNamespace(batch_size=1)
            )
            observed["epoch_started"] = True

    monkeypatch.setattr(train_worker, "load_run_config", lambda *_args: config)
    monkeypatch.setattr(train_worker, "YOLO", FloorYOLO)
    monkeypatch.setattr(
        train_worker,
        "mark_run_failed",
        lambda run_id, owner_id, dsn, error, **_kwargs: observed.setdefault(
            "failed", (run_id, owner_id, dsn, error)
        ),
    )

    with pytest.raises(train_worker.AutoBatchFloorError):
        train_worker.run_training(11, 111, "postgresql://test")

    assert observed["epoch_started"] is False
    assert observed["failed"][0:3] == (11, 111, "postgresql://test")
    assert "batch 1" in observed["failed"][3]
