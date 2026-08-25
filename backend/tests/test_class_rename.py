from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from sqlalchemy import select

from app.models import Annotation, Dataset, DatasetClass, Image, TrainingRun
from app.services import training
from app.services.storage import contained_storage_path
from tests.factories import image_with_media


pytestmark = pytest.mark.asyncio


def _name(suffix: str) -> str:
    return f"test-class-rename-{suffix}-{uuid4().hex}"


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


async def _dataset_with_labels(client, app) -> int:
    response = await client.post("/api/datasets", json={"name": _name("data")})
    assert response.status_code == 201
    dataset_id = response.json()["id"]
    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        storage = contained_storage_path(
            app.state.settings.storage_dir,
            dataset.storage_path,
        )
        session.add_all(
            [
                DatasetClass(dataset_id=dataset_id, class_id=0, name="orklift"),
                DatasetClass(dataset_id=dataset_id, class_id=1, name="person"),
            ]
        )
        for index in range(10):
            source = storage / f"image-{index}.jpg"
            source.write_bytes(f"image-{index}".encode())
            image = image_with_media(
                owner_id=dataset.owner_id,
                dataset_id=dataset_id,
                stem=f"image-{index}",
                filename=source.name,
                rel_path=f"incoming/{source.name}",
                split=None,
                width=32,
                height=24,
                file_path=str(source),
                display_path=None,
                thumb_path=str(storage / f"thumb-{index}.jpg"),
                box_count=2,
            )
            session.add(image)
            await session.flush()
            session.add_all(
                [
                    Annotation(
                        image_id=image.id,
                        class_id=0,
                        cx=0.25,
                        cy=0.25,
                        w=0.2,
                        h=0.2,
                    ),
                    Annotation(
                        image_id=image.id,
                        class_id=1,
                        cx=0.75,
                        cy=0.75,
                        w=0.2,
                        h=0.2,
                    ),
                ]
            )
        dataset.status = "ready"
        dataset.image_count = 10
        dataset.annotation_count = 20
        dataset.class_count = 2
        await session.commit()
    return dataset_id


async def test_class_rename_changes_name_only_and_rejects_extra_fields(
    client,
    app,
) -> None:
    dataset_id = await _dataset_with_labels(client, app)

    invalid = await client.patch(
        f"/api/datasets/{dataset_id}/classes/0",
        json={"name": "forklift", "class_id": 7},
    )
    renamed = await client.patch(
        f"/api/datasets/{dataset_id}/classes/0",
        json={"name": "forklift"},
    )

    assert invalid.status_code == 422
    assert renamed.status_code == 200
    assert renamed.json() == {"class_id": 0, "name": "forklift"}
    async with app.state.session_factory() as session:
        classes = (
            await session.scalars(
                select(DatasetClass)
                .where(DatasetClass.dataset_id == dataset_id)
                .order_by(DatasetClass.class_id)
            )
        ).all()
        annotation_ids = set(
            await session.scalars(
                select(Annotation.class_id)
                .join(Image, Image.id == Annotation.image_id)
                .where(Image.dataset_id == dataset_id)
            )
        )
    assert [(row.class_id, row.name) for row in classes] == [
        (0, "forklift"),
        (1, "person"),
    ]
    assert annotation_ids == {0, 1}
    duplicate = await client.patch(
        f"/api/datasets/{dataset_id}/classes/0",
        json={"name": "person"},
    )
    assert duplicate.status_code == 200


async def test_run_keeps_submit_time_class_name_snapshot_after_rename(
    client,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_ready(monkeypatch)
    dataset_id = await _dataset_with_labels(client, app)

    started = await client.post(
        f"/api/datasets/{dataset_id}/train",
        json={
            "weights": "yolo26n.pt",
            "epochs": 2,
            "imgsz": 640,
            "batch": 2,
            "seed": 123,
        },
    )
    assert started.status_code == 201
    async with app.state.session_factory() as session:
        run = await session.get(TrainingRun, started.json()["run_id"])
        assert run is not None
        data_yaml = (
            contained_storage_path(
                app.state.settings.storage_dir,
                run.out_dir,
            )
            / "workdir"
            / "data.yaml"
        )

    before = yaml.safe_load(data_yaml.read_text(encoding="utf-8"))
    renamed = await client.patch(
        f"/api/datasets/{dataset_id}/classes/0",
        json={"name": "forklift"},
    )
    after = yaml.safe_load(data_yaml.read_text(encoding="utf-8"))
    label_ids = {
        line.split()[0]
        for label in data_yaml.parent.glob("labels/*/*.txt")
        for line in label.read_text(encoding="utf-8").splitlines()
        if line
    }

    assert renamed.status_code == 200
    assert list(before["names"]) == [0, 1]
    assert before["names"] == {0: "orklift", 1: "person"}
    assert after == before
    assert label_ids == {"0", "1"}
