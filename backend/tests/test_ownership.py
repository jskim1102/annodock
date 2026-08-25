from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import (
    Annotation,
    Dataset,
    DatasetClass,
    Image,
    ImportIssue,
    Project,
    UploadJob,
    UploadSession,
)
from app.services.storage import storage_relative_path
from tests.factories import image_with_media


pytestmark = pytest.mark.asyncio

USER_A = 101
USER_B = 202


async def _create_dataset(
    client,
    auth_headers,
    owner_id: int,
    name: str,
    *,
    project_id: int | None = None,
) -> int:
    response = await client.post(
        "/api/datasets",
        headers=auth_headers(owner_id),
        json={
            "name": name,
            **({"project_id": project_id} if project_id is not None else {}),
        },
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def _create_project(client, auth_headers, owner_id: int) -> int:
    response = await client.post(
        "/api/projects",
        headers=auth_headers(owner_id),
        json={
            "name": f"test-merge-project-{uuid4().hex}",
            "classes": [],
        },
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def _mark_ready(app, *dataset_ids: int) -> None:
    async with app.state.session_factory() as session:
        rows = (
            await session.scalars(
                select(Dataset).where(Dataset.id.in_(dataset_ids))
            )
        ).all()
        assert len(rows) == len(dataset_ids)
        for dataset in rows:
            dataset.status = "ready"
        await session.commit()


async def _seed_owned_resource_graph(app, tmp_path: Path) -> dict[str, int]:
    storage_root = app.state.settings.storage_dir
    dataset_root = storage_root / "datasets" / uuid4().hex
    dataset_root.mkdir(parents=True)
    original = dataset_root / "original.jpg"
    thumb = dataset_root / "thumb.jpg"
    original.write_bytes(b"owned-original")
    thumb.write_bytes(b"owned-thumb")

    async with app.state.session_factory() as session:
        project = Project(
            owner_id=USER_A,
            name=f"test-ownership-project-{uuid4().hex}",
        )
        session.add(project)
        await session.flush()
        dataset = Dataset(
            owner_id=USER_A,
            project_id=project.id,
            name=f"test-ownership-graph-{uuid4().hex}",
            status="ready",
            storage_path=storage_relative_path(storage_root, dataset_root),
            image_count=1,
            annotation_count=1,
            class_count=1,
        )
        session.add(dataset)
        await session.flush()
        session.add(DatasetClass(dataset_id=dataset.id, class_id=0, name="box"))
        image = image_with_media(
            owner_id=dataset.owner_id,
            dataset_id=dataset.id,
            stem="original",
            filename="original.jpg",
            rel_path="images/original.jpg",
            split="train",
            width=16,
            height=12,
            file_path=storage_relative_path(storage_root, original),
            display_path=None,
            thumb_path=storage_relative_path(storage_root, thumb),
            original_bytes=original.stat().st_size,
            thumb_bytes=thumb.stat().st_size,
            box_count=1,
        )
        session.add(image)
        await session.flush()
        session.add(
            Annotation(
                image_id=image.id,
                class_id=0,
                cx=0.5,
                cy=0.5,
                w=0.25,
                h=0.25,
            )
        )
        upload = UploadSession(
            dataset_id=dataset.id,
            filename="owned.jpg",
            size=1,
            chunk_size=1,
            received_chunks=[],
            kind="file",
            state="open",
        )
        session.add(upload)
        upload_job = UploadJob(
            dataset_id=dataset.id,
            kind="upload",
            state="done",
            phase="done",
            total=1,
            processed=1,
            failed=0,
        )
        session.add(upload_job)
        await session.flush()
        session.add(
            ImportIssue(
                job_id=upload_job.id,
                kind="ignored_file",
                path="ignored.txt",
                detail="ignored",
            )
        )
        await session.commit()

        return {
            "dataset_id": dataset.id,
            "image_id": image.id,
            "upload_id": upload.id,
            "job_id": upload_job.id,
        }


async def test_dataset_names_and_lists_are_scoped_to_authenticated_owner(
    client,
    auth_headers,
) -> None:
    name = f"test-owner-name-{uuid4().hex}"
    dataset_a = await _create_dataset(client, auth_headers, USER_A, name)
    dataset_b = await _create_dataset(client, auth_headers, USER_B, name)

    duplicate = await client.post(
        "/api/datasets",
        headers=auth_headers(USER_A),
        json={"name": name},
    )
    assert duplicate.status_code == 409

    list_a = await client.get("/api/datasets", headers=auth_headers(USER_A))
    list_b = await client.get("/api/datasets", headers=auth_headers(USER_B))
    assert list_a.status_code == list_b.status_code == 200
    assert {row["id"] for row in list_a.json()["items"]} == {dataset_a}
    assert {row["id"] for row in list_b.json()["items"]} == {dataset_b}


async def test_foreign_dataset_resource_graph_is_hidden_as_404(
    client,
    app,
    auth_headers,
    tmp_path: Path,
) -> None:
    resource = await _seed_owned_resource_graph(app, tmp_path)
    headers = auth_headers(USER_B)
    dataset_id = resource["dataset_id"]
    image_id = resource["image_id"]
    upload_id = resource["upload_id"]
    job_id = resource["job_id"]

    requests = [
        ("GET", f"/api/datasets/{dataset_id}", {}),
        ("GET", f"/api/datasets/{dataset_id}/classes", {}),
        ("PATCH", f"/api/datasets/{dataset_id}", {"json": {"name": "hidden"}}),
        (
            "PATCH",
            f"/api/datasets/{dataset_id}/classes/0",
            {"json": {"name": "hidden"}},
        ),
        ("GET", f"/api/datasets/{dataset_id}/images", {}),
        ("GET", f"/api/images/{image_id}/file", {}),
        ("GET", f"/api/images/{image_id}/thumb", {}),
        ("GET", f"/api/images/{image_id}/annotations", {}),
        (
            "PUT",
            f"/api/images/{image_id}/annotations",
            {"json": {"boxes": []}},
        ),
        (
            "POST",
            f"/api/datasets/{dataset_id}/uploads",
            {
                "json": {
                    "filename": "foreign.jpg",
                    "size": 1,
                    "chunk_size": 1,
                    "kind": "file",
                }
            },
        ),
        (
            "POST",
            f"/api/datasets/{dataset_id}/upload-batches/preflight",
            {
                "json": {
                    "total_size": 1,
                    "largest_file_size": 1,
                    "file_count": 1,
                    "expected_extracted_size": 1,
                }
            },
        ),
        ("GET", f"/api/uploads/{upload_id}", {}),
        ("PUT", f"/api/uploads/{upload_id}/chunks/0", {"content": b"x"}),
        ("POST", f"/api/uploads/{upload_id}/complete", {}),
        ("GET", f"/api/jobs/{job_id}", {}),
        ("GET", f"/api/datasets/{dataset_id}/issues", {}),
        ("DELETE", f"/api/datasets/{dataset_id}", {}),
    ]
    for method, path, kwargs in requests:
        response = await client.request(method, path, headers=headers, **kwargs)
        assert response.status_code == 404, (method, path, response.text)


async def test_foreign_upload_cannot_be_added_to_owned_batch(
    client,
    app,
    auth_headers,
    tmp_path: Path,
) -> None:
    resource = await _seed_owned_resource_graph(app, tmp_path)
    dataset_b = await _create_dataset(
        client,
        auth_headers,
        USER_B,
        f"test-owner-batch-{uuid4().hex}",
    )

    response = await client.post(
        f"/api/datasets/{dataset_b}/upload-batches/complete",
        headers=auth_headers(USER_B),
        json={"upload_ids": [resource["upload_id"]]},
    )

    assert response.status_code == 404


async def test_merge_hides_foreign_sources_and_assigns_verified_owner(
    client,
    app,
    auth_headers,
) -> None:
    project_a = await _create_project(client, auth_headers, USER_A)
    source_a1 = await _create_dataset(
        client,
        auth_headers,
        USER_A,
        f"test-merge-a1-{uuid4().hex}",
        project_id=project_a,
    )
    source_a2 = await _create_dataset(
        client,
        auth_headers,
        USER_A,
        f"test-merge-a2-{uuid4().hex}",
        project_id=project_a,
    )
    foreign_name = f"test-merge-target-{uuid4().hex}"
    source_b = await _create_dataset(client, auth_headers, USER_B, foreign_name)
    await _mark_ready(app, source_a1, source_a2, source_b)

    hidden = await client.post(
        "/api/datasets/merge",
        headers=auth_headers(USER_A),
        json={
            "name": f"test-hidden-merge-{uuid4().hex}",
            "dataset_ids": [source_a1, source_b],
        },
    )
    assert hidden.status_code == 404

    # A may use B's dataset name because the unique key is owner-scoped.
    merged = await client.post(
        "/api/datasets/merge",
        headers=auth_headers(USER_A),
        json={"name": foreign_name, "dataset_ids": [source_a1, source_a2]},
    )
    assert merged.status_code == 201, merged.text
    merged_id = int(merged.json()["id"])

    async with app.state.session_factory() as session:
        row = await session.get(Dataset, merged_id)
        assert row is not None
        assert row.owner_id == USER_A
