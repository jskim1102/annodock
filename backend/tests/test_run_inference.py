from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image as PillowImage

from app.models import Dataset, Image, Project, RunImage, TrainingRun
from app.services import inference as inference_service
from app.services.storage import storage_relative_path
from tests.factories import image_with_media


pytestmark = pytest.mark.asyncio


def _png_bytes(size: tuple[int, int] = (32, 24)) -> bytes:
    output = io.BytesIO()
    PillowImage.new("RGB", size, (245, 245, 245)).save(output, "PNG")
    return output.getvalue()


async def _persist_run_with_images(
    app,
    tmp_path: Path,
    *,
    split_mode: str,
    splits: tuple[str, ...],
    state: str = "done",
    artifacts_deleted_at: datetime | None = None,
) -> tuple[int, dict[str, list[tuple[int, int, Path]]], Path]:
    async with app.state.session_factory() as session:
        project = Project(
            owner_id=1,
            name=f"test-run-inference-project-{uuid4().hex}",
        )
        session.add(project)
        await session.flush()
        dataset = Dataset(
            owner_id=1,
            project_id=project.id,
            name=f"test-run-inference-dataset-{uuid4().hex}",
            status="ready",
            storage_path=str(tmp_path / "pending"),
            image_count=len(splits),
            annotation_count=0,
            class_count=1,
        )
        session.add(dataset)
        await session.flush()
        dataset_root = app.state.settings.storage_dir / "datasets" / str(dataset.id)
        original_dir = dataset_root / "original"
        thumb_dir = dataset_root / "thumbs"
        original_dir.mkdir(parents=True)
        thumb_dir.mkdir(parents=True)
        dataset.storage_path = storage_relative_path(
            app.state.settings.storage_dir,
            dataset_root,
        )

        images: list[tuple[str, Image, Path]] = []
        for index, split in enumerate(splits, start=1):
            filename = f"image-{index:04d}.png"
            source = original_dir / filename
            thumb = thumb_dir / filename
            contents = _png_bytes()
            source.write_bytes(contents)
            thumb.write_bytes(contents)
            image = image_with_media(
                owner_id=dataset.owner_id,
                dataset_id=dataset.id,
                stem=Path(filename).stem,
                filename=filename,
                rel_path=filename,
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
                    thumb,
                ),
                box_count=0,
                is_modified=False,
            )
            session.add(image)
            images.append((split, image, source))
        await session.flush()

        out_dir = (
            app.state.settings.storage_dir
            / "training-runs"
            / uuid4().hex
        )
        artifacts = out_dir / "artifacts"
        artifacts.mkdir(parents=True)
        best_path = artifacts / "best.pt"
        best_path.write_bytes(b"trusted training artifact")
        run = TrainingRun(
            owner_id=1,
            dataset_id=dataset.id,
            dataset_name=f"test-run-inference-{uuid4().hex}",
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
            finished_at=(datetime.now(timezone.utc) if state == "done" else None),
            out_dir=storage_relative_path(
                app.state.settings.storage_dir,
                out_dir,
            ),
            error=None,
            artifacts_deleted_at=artifacts_deleted_at,
        )
        session.add(run)
        await session.flush()

        by_split: dict[str, list[tuple[int, int, Path]]] = {}
        for split, image, source in images:
            run_image = RunImage(
                run_id=run.id,
                image_id=image.id,
                split=split,
                stem=image.stem,
                filename=image.filename,
                rel_path=image.rel_path,
            )
            session.add(run_image)
            await session.flush()
            by_split.setdefault(split, []).append(
                (run_image.id, image.id, source)
            )
        await session.commit()
        return run.id, by_split, best_path


async def test_inference_images_uses_run_split_and_cursor_pagination(
    client,
    app,
    tmp_path: Path,
) -> None:
    run_id, rows, _best_path = await _persist_run_with_images(
        app,
        tmp_path,
        split_mode="2way",
        splits=("train", "valid", "valid", "valid", "valid", "valid"),
    )

    first = await client.get(
        f"/api/runs/{run_id}/inference-images?limit=2"
    )
    assert first.status_code == 200
    assert first.json()["split"] == "valid"
    assert first.json()["total"] == 5
    assert [item["id"] for item in first.json()["items"]] == [
        row[0] for row in rows["valid"][:2]
    ]
    assert all(
        item["image_id"] != rows["train"][0][1]
        for item in first.json()["items"]
    )
    cursor = first.json()["next_cursor"]
    assert cursor == rows["valid"][1][1]

    second = await client.get(
        f"/api/runs/{run_id}/inference-images?limit=2&cursor={cursor}"
    )
    assert [item["id"] for item in second.json()["items"]] == [
        row[0] for row in rows["valid"][2:4]
    ]
    assert second.json()["total"] is None
    assert second.json()["next_cursor"] == rows["valid"][3][1]

    final = await client.get(
        f"/api/runs/{run_id}/inference-images?limit=2&cursor={second.json()['next_cursor']}"
    )
    assert [item["id"] for item in final.json()["items"]] == [rows["valid"][4][0]]
    assert final.json()["next_cursor"] is None


async def test_inference_images_uses_test_for_three_way_run(
    client,
    app,
    tmp_path: Path,
) -> None:
    run_id, rows, _best_path = await _persist_run_with_images(
        app,
        tmp_path,
        split_mode="3way",
        splits=("train", "valid", "test", "test"),
    )

    response = await client.get(f"/api/runs/{run_id}/inference-images")

    assert response.status_code == 200
    assert response.json()["split"] == "test"
    assert response.json()["total"] == 2
    assert [item["id"] for item in response.json()["items"]] == [
        row[0] for row in rows["test"]
    ]


async def test_run_image_inference_uses_selected_split_image_and_best_artifact(
    client,
    app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, rows, best_path = await _persist_run_with_images(
        app,
        tmp_path,
        split_mode="2way",
        splits=("train", "valid"),
    )
    selected_id, _image_id, source = rows["valid"][0]
    observed: dict[str, object] = {}

    def fake_render(model_path: Path, image_path: Path, imgsz: int) -> bytes:
        observed.update(
            model_path=model_path,
            image_path=image_path,
            imgsz=imgsz,
        )
        return b"rendered-png"

    monkeypatch.setattr(
        "app.routers.runs.render_prediction_file",
        fake_render,
    )
    response = await client.post(
        f"/api/runs/{run_id}/inference-images/{selected_id}"
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == b"rendered-png"
    assert observed == {
        "model_path": best_path.resolve(),
        "image_path": source.resolve(),
        "imgsz": 640,
    }


async def test_run_image_inference_rejects_image_outside_selected_split(
    client,
    app,
    tmp_path: Path,
) -> None:
    run_id, rows, _best_path = await _persist_run_with_images(
        app,
        tmp_path,
        split_mode="2way",
        splits=("train", "valid"),
    )

    response = await client.post(
        f"/api/runs/{run_id}/inference-images/{rows['train'][0][0]}"
    )

    assert response.status_code == 404


async def test_render_prediction_file_caches_cpu_model_and_returns_png(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "best.pt"
    model_path.write_bytes(b"checkpoint")
    image_path = tmp_path / "source.png"
    image_path.write_bytes(_png_bytes())
    loaded: list[str] = []
    predicted: list[tuple[tuple[int, int], int, str, bool]] = []

    class FakeResult:
        def plot(self, *, pil: bool):
            assert pil is True
            rendered = PillowImage.new("RGB", (32, 24), (245, 245, 245))
            rendered.putpixel((4, 5), (255, 0, 0))
            return rendered

    class FakeModel:
        def __init__(self, path: str) -> None:
            loaded.append(path)

        def predict(self, *, source, imgsz: int, device: str, verbose: bool):
            predicted.append((source.size, imgsz, device, verbose))
            return [FakeResult()]

    inference_service.clear_model_cache()
    monkeypatch.setattr(inference_service, "YOLO", FakeModel)
    try:
        first = inference_service.render_prediction_file(
            model_path,
            image_path,
            640,
        )
        second = inference_service.render_prediction_file(
            model_path,
            image_path,
            640,
        )
    finally:
        inference_service.clear_model_cache()

    assert loaded == [str(model_path.resolve())]
    assert predicted == [
        ((32, 24), 640, "cpu", False),
        ((32, 24), 640, "cpu", False),
    ]
    with PillowImage.open(io.BytesIO(first)) as rendered:
        assert rendered.format == "PNG"
        assert rendered.getpixel((4, 5)) == (255, 0, 0)
    assert second == first
