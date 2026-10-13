"""Opt-in upload/edit/real GPU worker regression using only test storage.

Run alone (the test database is shared with the ordinary suite):
ANNODOCK_GPU_SMOKE=1 .venv/bin/python -m pytest -q tests/test_stabilization_gpu.py
"""

from __future__ import annotations

import asyncio
import io
import os
import time
import zipfile
from uuid import uuid4

import httpx
import pytest
import torch
from PIL import Image as PillowImage
from sqlalchemy.engine import make_url

from app.models import TrainingRun
from app.services.storage import contained_storage_path
from app.services.training import WEIGHTS_DIR
from tests.test_e2e_integration import _upload_zip


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("ANNODOCK_GPU_SMOKE") != "1",
        reason="set ANNODOCK_GPU_SMOKE=1 to run the isolated GPU smoke",
    ),
]


def _heif_archive() -> tuple[bytes, int, int]:
    # The application registers the HEIF decoder/encoder during import.
    members = {"classes.txt": b"object\n"}
    for index in range(10):
        image = io.BytesIO()
        PillowImage.new("RGB", (64, 48), (20 + index * 10, 50, 100)).save(
            image, format="HEIF"
        )
        stem = f"sample-{index:02d}"
        members[f"images/{stem}.heif"] = image.getvalue()
        members[f"labels/{stem}.txt"] = b"0 0.5 0.5 0.25 0.25\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue(), sum(map(len, members.values())), len(members)


async def test_heif_upload_annotation_edit_to_gpu_artifacts(
    app, client: httpx.AsyncClient, monkeypatch, tmp_path
) -> None:
    assert torch.cuda.is_available(), "opt-in GPU smoke requires CUDA"
    assert (WEIGHTS_DIR / "yolo26n.pt").is_file(), "prefetch yolo26n.pt first"
    database_name = make_url(app.state.settings.database_url).database
    assert database_name and database_name.endswith("_test")
    app.state.settings.storage_dir.resolve().relative_to(tmp_path.resolve())
    # Detached workers must inherit the same isolated root as the test API.
    monkeypatch.setenv("STORAGE_DIR", str(app.state.settings.storage_dir))
    monkeypatch.setenv("YOLO_CONFIG_DIR", str(tmp_path / "ultralytics-config"))
    monkeypatch.setenv("OMP_NUM_THREADS", "2")
    monkeypatch.setenv("MKL_NUM_THREADS", "2")
    monkeypatch.setenv("WANDB_MODE", "disabled")

    created = await client.post(
        "/api/datasets", json={"name": f"test-gpu-stabilization-{uuid4().hex}"}
    )
    assert created.status_code == 201, created.text
    dataset_id = created.json()["id"]
    run_id = None
    try:
        payload, extracted_bytes, file_count = _heif_archive()
        await _upload_zip(
            client, app, dataset_id, payload,
            extracted_bytes=extracted_bytes, file_count=file_count,
        )
        images = await client.get(f"/api/datasets/{dataset_id}/images")
        assert images.status_code == 200 and images.json()["total"] == 10
        image_id = images.json()["items"][0]["id"]
        boxes = [{"class_id": 0, "cx": 0.4, "cy": 0.4, "w": 0.2, "h": 0.3}]
        saved = await client.put(
            f"/api/images/{image_id}/annotations", json={"boxes": boxes}
        )
        assert saved.status_code == 200, saved.text
        persisted = await client.get(f"/api/images/{image_id}/annotations")
        assert persisted.json()["boxes"][0]["cx"] == pytest.approx(0.4)

        started = await client.post(
            f"/api/datasets/{dataset_id}/train",
            json={
                "weights": "yolo26n.pt", "epochs": 1, "imgsz": 320,
                "batch": 2, "workers": 0, "cache": "none",
                "compile": False, "amp": False, "augment": False,
                "save_period": -1, "seed": 42,
            },
        )
        assert started.status_code == 201, started.text
        run_id = started.json()["run_id"]
        async with app.state.session_factory() as session:
            run = await session.get(TrainingRun, run_id)
            assert run is not None and run.pid is not None
            out_dir = contained_storage_path(app.state.settings.storage_dir, run.out_dir)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            response = await client.get(f"/api/runs/{run_id}")
            assert response.status_code == 200, response.text
            state = response.json()["state"]
            if state in {"done", "failed", "canceled"}:
                break
            await asyncio.sleep(0.5)
        else:
            pytest.fail("isolated GPU worker exceeded 180 seconds")
        log = await client.get(f"/api/runs/{run_id}/log?tail=80")
        assert state == "done", log.text
        metrics = await client.get(f"/api/runs/{run_id}/metrics")
        assert metrics.status_code == 200 and len(metrics.json()) >= 1
        for artifact in ("best.pt", "last.pt", "results.csv"):
            downloaded = await client.get(f"/api/runs/{run_id}/artifacts/{artifact}")
            assert downloaded.status_code == 200 and len(downloaded.content) > 0
        assert not (out_dir / "workdir").exists(), "completed run workdir was not reclaimed"
    finally:
        if run_id is not None:
            detail = await client.get(f"/api/runs/{run_id}")
            if detail.status_code == 200 and detail.json()["state"] in {"running", "canceling"}:
                stopped = await client.post(f"/api/runs/{run_id}/cancel")
                assert stopped.status_code in {202, 409}, stopped.text
            deleted = await client.delete(f"/api/runs/{run_id}?confirm=true")
            assert deleted.status_code == 204, deleted.text
        deleted = await client.delete(f"/api/datasets/{dataset_id}")
        assert deleted.status_code == 204, deleted.text
