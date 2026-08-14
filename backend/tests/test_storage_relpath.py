from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.config import BACKEND_ROOT, Settings
from app.models import Dataset
from app.services.storage import (
    StorageBoundaryError,
    contained_storage_path,
    storage_relative_path,
)


def test_relative_storage_values_round_trip_under_configured_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "storage"
    target = root / "datasets" / "42" / "images" / "sample.jpg"

    stored = storage_relative_path(root, target)

    assert stored == "datasets/42/images/sample.jpg"
    assert contained_storage_path(root, stored) == target.resolve()


def test_legacy_absolute_storage_value_is_still_readable(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    target = root / "datasets" / "7" / "legacy.jpg"

    assert contained_storage_path(root, target) == target.resolve()
    assert storage_relative_path(root, target) == "datasets/7/legacy.jpg"


@pytest.mark.parametrize(
    "candidate",
    ["../outside.jpg", "/tmp/outside.jpg"],
)
def test_relative_storage_values_reject_paths_outside_root(
    tmp_path: Path,
    candidate: str,
) -> None:
    with pytest.raises(StorageBoundaryError):
        contained_storage_path(tmp_path / "storage", candidate)


def test_relative_storage_value_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(StorageBoundaryError):
        contained_storage_path(root, "escape/file.jpg")


def test_settings_resolves_relative_storage_from_backend_directory() -> None:
    settings = Settings(storage_dir=Path("storage-relative-test"))

    assert settings.storage_dir == (
        BACKEND_ROOT / "storage-relative-test"
    ).resolve()


@pytest.mark.asyncio
async def test_dataset_creation_persists_only_storage_relative_path(
    client: httpx.AsyncClient,
    app,
) -> None:
    created = await client.post(
        "/api/datasets",
        json={"name": f"test-storage-relpath-{uuid4().hex}"},
    )
    assert created.status_code == 201
    dataset_id = created.json()["id"]

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        assert dataset.storage_path == f"datasets/{dataset_id}"

    resolved = contained_storage_path(
        app.state.settings.storage_dir,
        f"datasets/{dataset_id}",
    )
    assert resolved.is_dir()
