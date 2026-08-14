from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, select, text

from app.models import Dataset
from scripts.seed_scale import seed_scale_dataset


pytestmark = pytest.mark.asyncio

IMAGE_COUNT = 50_000
BOXES_PER_IMAGE = 20
SAMPLES = 20


async def p95_ms(
    request: Callable[[], Awaitable[httpx.Response]],
) -> float:
    warmup = await request()
    assert warmup.status_code == 200
    durations: list[float] = []
    for _ in range(SAMPLES):
        started = time.perf_counter_ns()
        response = await request()
        durations.append((time.perf_counter_ns() - started) / 1_000_000)
        assert response.status_code == 200
    durations.sort()
    return durations[math.ceil(len(durations) * 0.95) - 1]


def plan_node_types(plan: dict) -> set[str]:
    nodes = {plan["Node Type"]}
    for child in plan.get("Plans", []):
        nodes.update(plan_node_types(child))
    return nodes


async def test_scale_acceptance_thresholds_and_indexed_queries(
    client: httpx.AsyncClient,
    app,
) -> None:
    created = await client.post(
        "/api/datasets",
        json={"name": f"test-scale-{uuid4().hex}"},
    )
    assert created.status_code == 201
    dataset_id = created.json()["id"]
    await seed_scale_dataset(
        app.state.settings,
        app.state.session_factory,
        dataset_id,
        image_count=IMAGE_COUNT,
        boxes_per_image=BOXES_PER_IMAGE,
    )

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        assert (
            dataset.image_count,
            dataset.annotation_count,
            dataset.class_count,
        ) == (50_000, 1_000_000, 3)
        image_plan_json = await session.scalar(
            text(
                """
                EXPLAIN (FORMAT JSON)
                SELECT id, stem, split
                FROM images
                WHERE dataset_id = :dataset_id AND split = 'train'
                ORDER BY stem, split, id
                LIMIT 200
                """
            ),
            {"dataset_id": dataset_id},
        )
        annotation_plan_json = await session.scalar(
            text(
                """
                EXPLAIN (FORMAT JSON)
                SELECT id, class_id, cx, cy, w, h
                FROM annotations
                WHERE image_id = (
                    SELECT min(id)
                    FROM images
                    WHERE dataset_id = :dataset_id
                )
                ORDER BY id
                """
            ),
            {"dataset_id": dataset_id},
        )

    first_page = await client.get(
        f"/api/datasets/{dataset_id}/images?offset=0&limit=200"
    )
    assert first_page.status_code == 200
    assert first_page.json()["total"] == IMAGE_COUNT
    assert len(first_page.json()["items"]) == 200
    image_id = first_page.json()["items"][0]["id"]

    statements: list[str] = []

    def capture_statement(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement.lower())

    event.listen(
        app.state.engine.sync_engine,
        "before_cursor_execute",
        capture_statement,
    )
    try:
        dataset_listing = await client.get("/api/datasets?offset=0&limit=200")
    finally:
        event.remove(
            app.state.engine.sync_engine,
            "before_cursor_execute",
            capture_statement,
        )
    assert dataset_listing.status_code == 200
    assert not any("annotations" in statement for statement in statements)
    assert any("image_count" in statement for statement in statements)

    metrics = {
        "images": await p95_ms(
            lambda: client.get(
                f"/api/datasets/{dataset_id}/images?offset=0&limit=200"
            )
        ),
        "images_split": await p95_ms(
            lambda: client.get(
                f"/api/datasets/{dataset_id}/images"
                "?offset=0&limit=200&split=train"
            )
        ),
        "datasets": await p95_ms(
            lambda: client.get("/api/datasets?offset=0&limit=200")
        ),
        "annotations": await p95_ms(
            lambda: client.get(f"/api/images/{image_id}/annotations")
        ),
        "thumbnail": await p95_ms(
            lambda: client.get(f"/api/images/{image_id}/thumb")
        ),
        "annotation_save": await p95_ms(
            lambda: client.put(
                f"/api/images/{image_id}/annotations",
                json={
                    "boxes": [
                        {
                            "class_id": 0,
                            "cx": 0.5,
                            "cy": 0.5,
                            "w": 0.2,
                            "h": 0.2,
                        }
                    ]
                },
            )
        ),
    }
    print(f"scale p95 ms: {metrics}")

    assert metrics["images"] < 300
    assert metrics["images_split"] < 300
    assert metrics["datasets"] < 500
    assert metrics["annotations"] < 100
    assert metrics["annotation_save"] < 150
    assert metrics["thumbnail"] < 100
    assert {
        "Index Scan",
        "Index Only Scan",
        "Bitmap Index Scan",
    } & plan_node_types(image_plan_json[0]["Plan"])
    assert {
        "Index Scan",
        "Index Only Scan",
        "Bitmap Index Scan",
    } & plan_node_types(annotation_plan_json[0]["Plan"])

    async with app.state.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        assert dataset.annotation_count == 999_981
