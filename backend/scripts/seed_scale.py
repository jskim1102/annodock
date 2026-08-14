"""Seed the 50k-image/1m-annotation performance acceptance dataset."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import func, select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import create_engine, create_session_factory  # noqa: E402
from app.models import Dataset, DatasetClass, Image  # noqa: E402
from app.services.storage import (  # noqa: E402
    contained_storage_path,
    create_dataset_storage,
)


DEFAULT_IMAGE_COUNT = 50_000
DEFAULT_BOXES_PER_IMAGE = 20


async def seed_scale_dataset(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    dataset_id: int,
    *,
    image_count: int = DEFAULT_IMAGE_COUNT,
    boxes_per_image: int = DEFAULT_BOXES_PER_IMAGE,
) -> None:
    if image_count <= 0 or boxes_per_image <= 0:
        raise ValueError("image and annotation counts must be positive")

    scale_dir: Path | None = None
    committed = False
    try:
        async with session_factory() as session:
            dataset = await session.scalar(
                select(Dataset)
                .where(Dataset.id == dataset_id)
                .with_for_update()
            )
            if dataset is None:
                raise LookupError(f"dataset {dataset_id} does not exist")
            existing = await session.scalar(
                select(func.count(Image.id)).where(
                    Image.dataset_id == dataset_id
                )
            )
            if existing:
                raise ValueError("scale seed requires an empty dataset")

            dataset_path = contained_storage_path(
                settings.storage_dir,
                dataset.storage_path,
            )
            scale_dir = dataset_path / "scale-seed"
            scale_dir.mkdir(parents=True, exist_ok=False)
            original_path = scale_dir / "shared.jpg"
            thumb_path = scale_dir / "shared-thumb.jpg"
            original_path.write_bytes(b"scale-image")
            thumb_path.write_bytes(b"scale-thumbnail")

            session.add_all(
                [
                    DatasetClass(
                        dataset_id=dataset_id,
                        class_id=class_id,
                        name=name,
                    )
                    for class_id, name in enumerate(
                        ("person", "vehicle", "object")
                    )
                ]
            )
            await session.execute(
                text(
                    """
                    INSERT INTO images (
                        dataset_id,
                        stem,
                        filename,
                        rel_path,
                        split,
                        width,
                        height,
                        file_path,
                        display_path,
                        thumb_path,
                        box_count,
                        is_modified
                    )
                    SELECT
                        :dataset_id,
                        'image-' || lpad(series_no::text, 6, '0'),
                        'image-' || lpad(series_no::text, 6, '0') || '.jpg',
                        'images/' ||
                            CASE series_no % 3
                                WHEN 1 THEN 'train'
                                WHEN 2 THEN 'val'
                                ELSE 'test'
                            END ||
                            '/image-' || lpad(series_no::text, 6, '0') ||
                            '.jpg',
                        CASE series_no % 3
                            WHEN 1 THEN 'train'
                            WHEN 2 THEN 'val'
                            ELSE 'test'
                        END,
                        1920,
                        1080,
                        :file_path,
                        NULL,
                        :thumb_path,
                        :boxes_per_image,
                        false
                    FROM generate_series(1, :image_count) AS series_no
                    """
                ),
                {
                    "dataset_id": dataset_id,
                    "file_path": str(original_path),
                    "thumb_path": str(thumb_path),
                    "boxes_per_image": boxes_per_image,
                    "image_count": image_count,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO annotations (
                        image_id,
                        class_id,
                        cx,
                        cy,
                        w,
                        h
                    )
                    SELECT
                        images.id,
                        (box_no - 1) % 3,
                        0.5,
                        0.5,
                        0.2,
                        0.2
                    FROM images
                    CROSS JOIN generate_series(
                        1,
                        :boxes_per_image
                    ) AS box_no
                    WHERE images.dataset_id = :dataset_id
                    """
                ),
                {
                    "dataset_id": dataset_id,
                    "boxes_per_image": boxes_per_image,
                },
            )
            dataset.status = "ready"
            dataset.image_count = image_count
            dataset.annotation_count = image_count * boxes_per_image
            dataset.class_count = 3
            await session.commit()
            committed = True

        async with session_factory() as session:
            await session.execute(text("ANALYZE images"))
            await session.execute(text("ANALYZE annotations"))
            await session.commit()
    except Exception:
        if scale_dir is not None and not committed:
            shutil.rmtree(scale_dir, ignore_errors=True)
        raise


async def create_and_seed(
    settings: Settings,
    *,
    database_url: str,
    name: str,
    image_count: int,
    boxes_per_image: int,
) -> int:
    engine = create_engine(database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            dataset = Dataset(
                name=name,
                status="pending",
                storage_path="",
            )
            session.add(dataset)
            await session.flush()
            dataset.storage_path = str(
                create_dataset_storage(settings.storage_dir, dataset.id)
            )
            await session.commit()
            dataset_id = dataset.id
        await seed_scale_dataset(
            settings,
            session_factory,
            dataset_id,
            image_count=image_count,
            boxes_per_image=boxes_per_image,
        )
        return dataset_id
    finally:
        await engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url")
    parser.add_argument(
        "--name",
        default=(
            "scale-benchmark-"
            + datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        ),
    )
    parser.add_argument("--images", type=int, default=DEFAULT_IMAGE_COUNT)
    parser.add_argument(
        "--boxes-per-image",
        type=int,
        default=DEFAULT_BOXES_PER_IMAGE,
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = Settings()
    dataset_id = asyncio.run(
        create_and_seed(
            settings,
            database_url=args.database_url or settings.database_url,
            name=args.name,
            image_count=args.images,
            boxes_per_image=args.boxes_per_image,
        )
    )
    print(
        f"seeded dataset_id={dataset_id} "
        f"images={args.images} "
        f"annotations={args.images * args.boxes_per_image}"
    )


if __name__ == "__main__":
    main()
