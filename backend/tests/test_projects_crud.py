from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from app.models import (
    Annotation,
    Dataset,
    ExportArtifact,
    Image,
    Project,
    ProjectClass,
    RunImage,
    RunMetric,
    TrainingRun,
    UploadJob,
    UserStorage,
)
from app.services.storage import (
    contained_storage_path,
    create_dataset_storage,
    storage_relative_path,
)
from tests.factories import image_with_media


pytestmark = pytest.mark.asyncio


def unique_name(suffix: str) -> str:
    return f"test-project-crud-{suffix}-{uuid4().hex}"


async def create_project(
    client: httpx.AsyncClient,
    name: str,
    *,
    headers: dict[str, str] | None = None,
    classes: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    response = await client.post(
        "/api/projects",
        json={"name": name, "classes": classes or []},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def create_dataset(
    client: httpx.AsyncClient,
    project_id: int,
    name: str,
) -> dict[str, object]:
    response = await client.post(
        "/api/datasets",
        json={"name": name, "project_id": project_id},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_patch_is_owner_scoped_and_name_unique_per_owner(
    client: httpx.AsyncClient,
    auth_headers,
) -> None:
    source = await create_project(client, unique_name("source"))
    shared_name = unique_name("shared")
    await create_project(
        client,
        shared_name,
        headers=auth_headers(2),
    )

    allowed = await client.patch(
        f"/api/projects/{source['id']}",
        json={"name": shared_name},
    )

    assert allowed.status_code == 200
    assert allowed.json() == {"id": source["id"], "name": shared_name}

    duplicate_name = unique_name("duplicate")
    await create_project(client, duplicate_name)
    conflict = await client.patch(
        f"/api/projects/{source['id']}",
        json={"name": duplicate_name},
    )
    foreign = await client.patch(
        f"/api/projects/{source['id']}",
        json={"name": unique_name("foreign")},
        headers=auth_headers(2),
    )

    assert conflict.status_code == 409
    assert foreign.status_code == 404


async def test_delete_empty_project_cleans_hidden_placeholder_without_confirmation(
    client: httpx.AsyncClient,
    app,
    auth_headers,
) -> None:
    project = await create_project(client, unique_name("placeholder"))
    project_id = int(project["id"])
    async with app.state.session_factory() as session:
        placeholder = Dataset(
            owner_id=1,
            project_id=project_id,
            name=unique_name("hidden"),
            status="pending",
            storage_path="",
            is_placeholder=True,
        )
        session.add(placeholder)
        await session.flush()
        placeholder_root = create_dataset_storage(
            app.state.settings.storage_dir,
            placeholder.id,
        )
        placeholder.storage_path = storage_relative_path(
            app.state.settings.storage_dir,
            placeholder_root,
        )
        placeholder_id = placeholder.id
        await session.commit()

    foreign = await client.delete(
        f"/api/projects/{project_id}",
        headers=auth_headers(2),
    )
    deleted = await client.delete(f"/api/projects/{project_id}")

    assert foreign.status_code == 404
    assert deleted.status_code == 204
    assert not placeholder_root.exists()
    async with app.state.session_factory() as session:
        assert await session.get(Project, project_id) is None
        assert await session.get(Dataset, placeholder_id) is None


async def test_delete_requires_confirmation_and_lists_every_visible_dataset(
    client: httpx.AsyncClient,
    app,
) -> None:
    project = await create_project(client, unique_name("confirmation"))
    project_id = int(project["id"])
    first = await create_dataset(client, project_id, unique_name("first"))
    second = await create_dataset(client, project_id, unique_name("second"))

    async with app.state.session_factory() as session:
        dataset_roots = [
            contained_storage_path(
                app.state.settings.storage_dir,
                (await session.get(Dataset, int(item["id"]))).storage_path,
            )
            for item in (first, second)
        ]

    preview = await client.delete(f"/api/projects/{project_id}")

    assert preview.status_code == 409
    detail = preview.json()["detail"]
    assert detail == {
        "code": "project-delete-confirmation-required",
        "requires_confirmation": True,
        "warning": "이 작업은 되돌릴 수 없습니다.",
        "datasets": [
            {"id": first["id"], "name": first["name"]},
            {"id": second["id"], "name": second["name"]},
        ],
    }
    assert all(path.is_dir() for path in dataset_roots)

    deleted = await client.delete(
        f"/api/projects/{project_id}?confirm=true"
    )

    assert deleted.status_code == 204
    assert all(not path.exists() for path in dataset_roots)


async def test_delete_rejects_running_or_canceling_training_runs(
    client: httpx.AsyncClient,
    app,
) -> None:
    project = await create_project(client, unique_name("active-run"))
    project_id = int(project["id"])
    dataset = await create_dataset(
        client,
        project_id,
        unique_name("active-dataset"),
    )
    dataset_id = int(dataset["id"])
    async with app.state.session_factory() as session:
        run = TrainingRun(
            owner_id=1,
            dataset_id=dataset_id,
            dataset_name=str(dataset["name"]),
            weights="yolo26n.pt",
            epochs=1,
            imgsz=64,
            batch=1,
            split_mode="2way",
            ratios={"train": 0.8, "valid": 0.2},
            seed=7,
            state="canceling",
            out_dir=storage_relative_path(
                app.state.settings.storage_dir,
                app.state.settings.storage_dir
                / "training-runs"
                / unique_name("active-out"),
            ),
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    response = await client.delete(
        f"/api/projects/{project_id}?confirm=true"
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "project-active-runs"
    assert detail["message"] == (
        "진행 중이거나 취소 중인 학습이 있어 프로젝트를 삭제할 수 없습니다. "
        "학습이 끝난 뒤 다시 시도하세요."
    )
    assert detail["runs"] == [
        {
            "id": run_id,
            "dataset_id": dataset_id,
            "dataset_name": dataset["name"],
            "state": "canceling",
        }
    ]
    async with app.state.session_factory() as session:
        assert await session.get(Project, project_id) is not None
        assert await session.get(Dataset, dataset_id) is not None
        assert await session.get(TrainingRun, run_id) is not None


async def test_confirmed_delete_reclaims_files_runs_and_exact_accounting(
    client: httpx.AsyncClient,
    app,
) -> None:
    target_project = await create_project(
        client,
        unique_name("accounted-target"),
        classes=[{"name": "person", "color": "#EF4444"}],
    )
    target_project_id = int(target_project["id"])
    target_dataset = await create_dataset(
        client,
        target_project_id,
        unique_name("accounted-dataset"),
    )
    target_dataset_id = int(target_dataset["id"])

    survivor_project = await create_project(
        client,
        unique_name("accounted-survivor"),
    )
    survivor_dataset = await create_dataset(
        client,
        int(survivor_project["id"]),
        unique_name("survivor-dataset"),
    )
    survivor_dataset_id = int(survivor_dataset["id"])

    storage_dir = app.state.settings.storage_dir
    async with app.state.session_factory() as session:
        target = await session.get(Dataset, target_dataset_id)
        survivor = await session.get(Dataset, survivor_dataset_id)
        assert target is not None and survivor is not None
        target_root = contained_storage_path(storage_dir, target.storage_path)
        survivor_root = contained_storage_path(storage_dir, survivor.storage_path)

        original = target_root / "original" / "sample.jpg"
        display = target_root / "derived" / "sample.display.jpg"
        thumb = target_root / "derived" / "sample.thumb.jpg"
        archive = target_root / "exports" / "dataset.zip"
        for path, content in (
            (original, b"target-original"),
            (display, b"target-display"),
            (thumb, b"target-thumb"),
            (archive, b"target-export"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        image = image_with_media(
            owner_id=target.owner_id,
            dataset_id=target_dataset_id,
            stem="sample",
            filename="sample.jpg",
            rel_path="images/sample.jpg",
            split="train",
            width=32,
            height=24,
            file_path=storage_relative_path(storage_dir, original),
            display_path=storage_relative_path(storage_dir, display),
            thumb_path=storage_relative_path(storage_dir, thumb),
            original_bytes=original.stat().st_size,
            display_bytes=display.stat().st_size,
            thumb_bytes=thumb.stat().st_size,
            box_count=1,
        )
        image.annotations.append(
            Annotation(
                class_id=0,
                cx=0.5,
                cy=0.5,
                w=0.25,
                h=0.25,
                serialized_bytes=24,
            )
        )
        session.add(image)
        export_job = UploadJob(
            dataset_id=target_dataset_id,
            kind="export",
            state="done",
            phase="done",
            total=1,
            processed=1,
            failed=0,
        )
        session.add(export_job)
        await session.flush()
        session.add(
            ExportArtifact(
                job_id=export_job.id,
                dataset_id=target_dataset_id,
                archive_path=storage_relative_path(storage_dir, archive),
                archive_bytes=archive.stat().st_size,
            )
        )

        survivor_original = survivor_root / "original" / "keep.jpg"
        survivor_thumb = survivor_root / "derived" / "keep.thumb.jpg"
        survivor_original.parent.mkdir(parents=True, exist_ok=True)
        survivor_thumb.parent.mkdir(parents=True, exist_ok=True)
        survivor_original.write_bytes(b"survivor-original")
        survivor_thumb.write_bytes(b"survivor-thumb")
        survivor_image = image_with_media(
            owner_id=survivor.owner_id,
            dataset_id=survivor_dataset_id,
            stem="keep",
            filename="keep.jpg",
            rel_path="images/keep.jpg",
            split="train",
            width=16,
            height=16,
            file_path=storage_relative_path(storage_dir, survivor_original),
            display_path=None,
            thumb_path=storage_relative_path(storage_dir, survivor_thumb),
            original_bytes=survivor_original.stat().st_size,
            display_bytes=0,
            thumb_bytes=survivor_thumb.stat().st_size,
        )
        session.add(survivor_image)
        await session.flush()

        run = TrainingRun(
            owner_id=1,
            dataset_id=target_dataset_id,
            dataset_name=target.name,
            weights="yolo26n.pt",
            epochs=1,
            imgsz=64,
            batch=1,
            split_mode="2way",
            ratios={"train": 0.8, "valid": 0.2},
            seed=9,
            state="done",
            out_dir="pending",
            finished_at=datetime.now(timezone.utc),
        )
        session.add(run)
        await session.flush()
        run_root = storage_dir / "training-runs" / str(run.id)
        artifacts = run_root / "artifacts"
        workdir = run_root / "workdir"
        artifacts.mkdir(parents=True)
        workdir.mkdir()
        (artifacts / "best.pt").write_bytes(b"best-weights")
        (artifacts / "results.csv").write_bytes(b"epoch,map\n1,0.5\n")
        (workdir / "scratch.bin").write_bytes(b"not-accounted-workdir")
        artifact_bytes = sum(path.stat().st_size for path in artifacts.iterdir())
        run.out_dir = storage_relative_path(storage_dir, run_root)
        run.artifact_bytes = artifact_bytes
        run_image = RunImage(
            run_id=run.id,
            image_id=image.id,
            split="train",
            stem=image.stem,
            filename=image.filename,
            rel_path=image.rel_path,
        )
        metric = RunMetric(run_id=run.id, epoch=1, map50=0.5)
        session.add_all([run_image, metric])
        await session.flush()

        target_bytes = (
            original.stat().st_size
            + display.stat().st_size
            + thumb.stat().st_size
            + archive.stat().st_size
            + artifact_bytes
        )
        survivor_bytes = (
            survivor_original.stat().st_size + survivor_thumb.stat().st_size
        )
        session.add(
            UserStorage(
                owner_id=1,
                bytes_used=target_bytes + survivor_bytes,
            )
        )
        await session.commit()
        image_id = image.id
        annotation_id = image.annotations[0].id
        export_job_id = export_job.id
        run_id = run.id
        run_image_id = run_image.id
        metric_id = metric.id

    deleted = await client.delete(
        f"/api/projects/{target_project_id}?confirm=true"
    )

    assert deleted.status_code == 204, deleted.text
    assert not target_root.exists()
    assert not run_root.exists()
    assert survivor_root.is_dir()
    async with app.state.session_factory() as session:
        usage = await session.get(UserStorage, 1)
        assert usage is not None and usage.bytes_used == survivor_bytes
        assert await session.get(Project, target_project_id) is None
        assert await session.get(ProjectClass, (target_project_id, 0)) is None
        assert await session.get(Dataset, target_dataset_id) is None
        assert await session.get(Image, image_id) is None
        assert await session.get(Annotation, annotation_id) is None
        assert await session.get(UploadJob, export_job_id) is None
        assert await session.get(ExportArtifact, export_job_id) is None
        assert await session.get(TrainingRun, run_id) is None
        assert await session.get(RunImage, run_image_id) is None
        assert await session.get(RunMetric, metric_id) is None
        assert await session.get(Dataset, survivor_dataset_id) is not None
