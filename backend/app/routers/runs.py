"""Training run monitoring, metrics, logs, and artifact downloads."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import CurrentUserDep
from app.models import Dataset, Image, RunImage, RunMetric, TrainingRun
from app.services.cleanup import (
    stage_run_artifacts_async,
    stage_training_run_deletion_async,
)
from app.services.inference import render_prediction_file
from app.services.quota import decrease_bytes_used, path_tree_bytes
from app.services.storage import (
    StorageBoundaryError,
    contained_storage_path,
    finalize_staged_deletion,
    restore_staged_deletion,
)
from app.training_params import normalize_training_args


router = APIRouter(prefix="/api/runs", tags=["training-runs"])
logger = logging.getLogger(__name__)
Session = Annotated[AsyncSession, Depends(get_session)]
RunState = Literal[
    "queued",
    "running",
    "canceling",
    "done",
    "failed",
    "canceled",
]
ARTIFACT_NAMES = frozenset({"best.pt", "last.pt", "results.csv"})
LOG_CHUNK_BYTES = 64 * 1024
ACTIVE_ARTIFACT_DELETE_DETAIL = (
    "진행 중인 학습의 산출물은 삭제할 수 없습니다. "
    "학습이 끝나거나 취소된 뒤 다시 시도하세요."
)
ACTIVE_RUN_DELETE_STATES = frozenset({"queued", "running", "canceling"})


class RunSummary(BaseModel):
    id: int
    dataset_id: int | None
    dataset_name: str
    weights: str
    state: RunState
    epochs: int
    epoch: int
    started_at: datetime | None
    finished_at: datetime | None
    artifacts_deleted_at: datetime | None


class RunPage(BaseModel):
    items: list[RunSummary]
    total: int


class RunDetail(RunSummary):
    imgsz: int
    batch: int
    split_mode: Literal["2way", "3way"]
    ratios: dict[str, float]
    seed: int
    training_args: dict[str, object]
    error: str | None
    image_counts: dict[Literal["train", "valid", "test"], int]


class MetricRow(BaseModel):
    epoch: int
    box_loss: float | None
    cls_loss: float | None
    dfl_loss: float | None
    map50: float | None
    map5095: float | None
    lr: dict[str, float] | None


class InferenceImageRow(BaseModel):
    id: int
    image_id: int
    filename: str


class InferenceImagePage(BaseModel):
    split: Literal["valid", "test"]
    items: list[InferenceImageRow]
    next_cursor: int | None
    total: int | None


def _epoch_subquery():
    return (
        select(
            RunMetric.run_id.label("run_id"),
            func.max(RunMetric.epoch).label("epoch"),
        )
        .group_by(RunMetric.run_id)
        .subquery()
    )


def _image_count_subquery():
    return (
        select(
            RunImage.run_id.label("run_id"),
            func.count().filter(RunImage.split == "train").label("train"),
            func.count().filter(RunImage.split == "valid").label("valid"),
            func.count().filter(RunImage.split == "test").label("test"),
        )
        .group_by(RunImage.run_id)
        .subquery()
    )


def _summary(run: TrainingRun, epoch: int) -> RunSummary:
    return RunSummary(
        id=run.id,
        dataset_id=run.dataset_id,
        dataset_name=run.dataset_name,
        weights=run.weights,
        state=run.state,
        epochs=run.epochs,
        epoch=epoch,
        started_at=run.started_at,
        finished_at=run.finished_at,
        artifacts_deleted_at=run.artifacts_deleted_at,
    )


async def _run_with_epoch(
    session: AsyncSession,
    run_id: int,
    owner_id: int,
) -> tuple[TrainingRun, int, dict[str, int]]:
    latest_epoch = _epoch_subquery()
    image_counts = _image_count_subquery()
    row = (
        await session.execute(
            select(
                TrainingRun,
                func.coalesce(latest_epoch.c.epoch, 0).label("epoch"),
                func.coalesce(image_counts.c.train, 0).label("train_count"),
                func.coalesce(image_counts.c.valid, 0).label("valid_count"),
                func.coalesce(image_counts.c.test, 0).label("test_count"),
            )
            .outerjoin(latest_epoch, latest_epoch.c.run_id == TrainingRun.id)
            .outerjoin(image_counts, image_counts.c.run_id == TrainingRun.id)
            .where(
                TrainingRun.id == run_id,
                TrainingRun.owner_id == owner_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="학습 run을 찾을 수 없습니다.")
    run, epoch, train_count, valid_count, test_count = row
    return run, int(epoch), {
        "train": int(train_count),
        "valid": int(valid_count),
        "test": int(test_count),
    }


async def _owned_run_or_404(
    session: AsyncSession,
    run_id: int,
    owner_id: int,
    *,
    for_update: bool = False,
) -> TrainingRun:
    query = select(TrainingRun).where(
        TrainingRun.id == run_id,
        TrainingRun.owner_id == owner_id,
    )
    if for_update:
        query = query.with_for_update()
    run = await session.scalar(query)
    if run is None:
        raise HTTPException(status_code=404, detail="학습 run을 찾을 수 없습니다.")
    return run


async def _owned_dataset_or_404(
    session: AsyncSession,
    dataset_id: int,
    owner_id: int,
) -> None:
    exists = await session.scalar(
        select(Dataset.id).where(
            Dataset.id == dataset_id,
            Dataset.owner_id == owner_id,
        )
    )
    if exists is None:
        raise HTTPException(status_code=404, detail="데이터셋을 찾을 수 없습니다.")


def _read_log_tail(path: Path, line_count: int) -> str:
    """Read only enough bytes from the end to return the requested lines."""
    try:
        with path.open("rb") as log_file:
            log_file.seek(0, 2)
            position = log_file.tell()
            chunks: list[bytes] = []
            newline_count = 0
            while position > 0 and newline_count <= line_count:
                chunk_size = min(LOG_CHUNK_BYTES, position)
                position -= chunk_size
                log_file.seek(position)
                chunk = log_file.read(chunk_size)
                chunks.append(chunk)
                newline_count += chunk.count(b"\n")
    except OSError:
        return ""
    text = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-line_count:])


def _artifact_file_or_404(
    storage_dir: Path,
    run: TrainingRun,
    name: str,
) -> Path:
    try:
        artifacts_dir = contained_storage_path(storage_dir, run.out_dir) / "artifacts"
    except StorageBoundaryError as error:
        raise HTTPException(
            status_code=404,
            detail="산출물을 찾을 수 없습니다.",
        ) from error
    candidate = artifacts_dir / name
    if artifacts_dir.is_symlink() or candidate.is_symlink():
        raise HTTPException(status_code=404, detail="산출물을 찾을 수 없습니다.")
    try:
        resolved_dir = artifacts_dir.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise HTTPException(
            status_code=404,
            detail="산출물을 찾을 수 없습니다.",
        ) from error
    if (
        resolved_candidate.parent != resolved_dir
        or not resolved_candidate.is_file()
    ):
        raise HTTPException(status_code=404, detail="산출물을 찾을 수 없습니다.")
    return resolved_candidate


def _inference_split(run: TrainingRun) -> Literal["valid", "test"]:
    return "test" if run.split_mode == "3way" else "valid"


def _ensure_run_can_infer(run: TrainingRun) -> None:
    if run.state != "done":
        raise HTTPException(
            status_code=409,
            detail="학습이 완료된 run만 추론할 수 있습니다.",
        )
    if run.artifacts_deleted_at is not None:
        raise HTTPException(status_code=410, detail="산출물이 삭제되었습니다.")


@router.get("", response_model=RunPage)
async def list_runs(
    session: Session,
    current_user: CurrentUserDep,
    dataset_id: int | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> RunPage:
    latest_epoch = _epoch_subquery()
    owner_predicate = TrainingRun.owner_id == current_user.id
    page_query = select(
        TrainingRun,
        func.coalesce(latest_epoch.c.epoch, 0).label("epoch"),
    ).outerjoin(latest_epoch, latest_epoch.c.run_id == TrainingRun.id).where(
        owner_predicate
    )
    count_query = (
        select(func.count())
        .select_from(TrainingRun)
        .where(owner_predicate)
    )
    if dataset_id is not None:
        await _owned_dataset_or_404(
            session,
            dataset_id,
            current_user.id,
        )
        predicate = TrainingRun.dataset_id == dataset_id
        page_query = page_query.where(predicate)
        count_query = count_query.where(predicate)
    rows = (
        await session.execute(
            page_query.order_by(
                TrainingRun.created_at.desc(),
                TrainingRun.id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
    ).all()
    total = await session.scalar(count_query)
    return RunPage(
        items=[_summary(run, int(epoch)) for run, epoch in rows],
        total=total or 0,
    )


@router.get("/{run_id}", response_model=RunDetail)
async def get_run(
    run_id: int,
    session: Session,
    current_user: CurrentUserDep,
) -> RunDetail:
    run, epoch, image_counts = await _run_with_epoch(
        session,
        run_id,
        current_user.id,
    )
    return RunDetail(
        **_summary(run, epoch).model_dump(),
        imgsz=run.imgsz,
        batch=run.batch,
        split_mode=run.split_mode,
        ratios=run.ratios,
        seed=run.seed,
        training_args=normalize_training_args(run.training_args),
        error=run.error,
        image_counts=image_counts,
    )


@router.get("/{run_id}/metrics", response_model=list[MetricRow])
async def get_run_metrics(
    run_id: int,
    session: Session,
    current_user: CurrentUserDep,
) -> list[MetricRow]:
    await _owned_run_or_404(session, run_id, current_user.id)
    metrics = (
        await session.scalars(
            select(RunMetric)
            .where(RunMetric.run_id == run_id)
            .order_by(RunMetric.epoch.asc())
        )
    ).all()
    return [
        MetricRow(
            epoch=metric.epoch,
            box_loss=metric.box_loss,
            cls_loss=metric.cls_loss,
            dfl_loss=metric.dfl_loss,
            map50=metric.map50,
            map5095=metric.map5095,
            lr=metric.lr,
        )
        for metric in metrics
    ]


@router.get("/{run_id}/log", response_class=Response)
async def get_run_log(
    run_id: int,
    request: Request,
    session: Session,
    current_user: CurrentUserDep,
    tail: Annotated[int, Query(ge=1, le=5_000)] = 200,
) -> Response:
    run = await _owned_run_or_404(session, run_id, current_user.id)
    if run.artifacts_deleted_at is not None:
        content = ""
    else:
        try:
            run_dir = contained_storage_path(
                request.app.state.settings.storage_dir,
                run.out_dir,
            )
        except StorageBoundaryError:
            content = ""
        else:
            content = await asyncio.to_thread(
                _read_log_tail,
                run_dir / "artifacts" / "log",
                tail,
            )
    return Response(content=content, media_type="text/plain")


@router.delete("/{run_id}/artifacts", status_code=204)
async def delete_run_artifacts(
    run_id: int,
    request: Request,
    session: Session,
    current_user: CurrentUserDep,
) -> Response:
    run = await _owned_run_or_404(
        session,
        run_id,
        current_user.id,
        for_update=True,
    )
    if run.state in {"running", "canceling"}:
        raise HTTPException(
            status_code=409,
            detail=ACTIVE_ARTIFACT_DELETE_DETAIL,
        )
    if run.artifacts_deleted_at is not None:
        return Response(status_code=204)

    artifact_bytes = run.artifact_bytes
    if artifact_bytes is None:
        try:
            run_dir = contained_storage_path(
                request.app.state.settings.storage_dir,
                run.out_dir,
            )
        except StorageBoundaryError:
            artifact_bytes = 0
        else:
            artifact_bytes = await asyncio.to_thread(
                path_tree_bytes,
                run_dir / "artifacts",
            )
    staged = await stage_run_artifacts_async(
        request.app.state.settings.storage_dir,
        run.out_dir,
    )
    run.artifacts_deleted_at = datetime.now(timezone.utc)
    run.artifact_bytes = 0
    try:
        await session.commit()
    except BaseException as error:
        restore_staged_deletion(staged)
        try:
            await session.rollback()
        except BaseException as rollback_error:
            error.add_note(
                "run artifact cleanup rollback also failed: "
                f"{type(rollback_error).__name__}"
            )
        raise
    await asyncio.to_thread(finalize_staged_deletion, staged)
    await decrease_bytes_used(session, run.owner_id, artifact_bytes)
    await session.commit()
    return Response(status_code=204)


@router.delete("/{run_id}", status_code=204)
async def delete_run(
    run_id: int,
    request: Request,
    session: Session,
    current_user: CurrentUserDep,
    confirm: Annotated[bool, Query()] = False,
) -> Response:
    run = await _owned_run_or_404(
        session,
        run_id,
        current_user.id,
        for_update=True,
    )
    if run.state in ACTIVE_RUN_DELETE_STATES:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "run-active",
                "message": (
                    "대기 중이거나 진행 중인 학습 run은 삭제할 수 없습니다. "
                    "학습이 끝난 뒤 다시 시도하세요."
                ),
                "run": {"id": run.id, "state": run.state},
            },
        )
    if not confirm:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "run-delete-confirmation-required",
                "requires_confirmation": True,
                "warning": (
                    "run 기록과 산출물이 삭제되며 되돌릴 수 없습니다."
                ),
                "run": {
                    "id": run.id,
                    "dataset_name": run.dataset_name,
                },
            },
        )

    artifact_bytes = 0
    if run.artifacts_deleted_at is None:
        if run.artifact_bytes is not None:
            artifact_bytes = int(run.artifact_bytes)
        else:
            try:
                run_dir = contained_storage_path(
                    request.app.state.settings.storage_dir,
                    run.out_dir,
                )
            except StorageBoundaryError:
                artifact_bytes = 0
            else:
                artifact_bytes = await asyncio.to_thread(
                    path_tree_bytes,
                    run_dir / "artifacts",
                )

    owner_id = run.owner_id
    staged = await stage_training_run_deletion_async(
        request.app.state.settings.storage_dir,
        run.out_dir,
    )
    try:
        await session.delete(run)
        await session.commit()
    except BaseException as error:
        restore_staged_deletion(staged)
        try:
            await session.rollback()
        except BaseException as rollback_error:
            error.add_note(
                "run delete rollback also failed: "
                f"{type(rollback_error).__name__}"
            )
        raise

    await asyncio.to_thread(finalize_staged_deletion, staged)
    if artifact_bytes:
        await decrease_bytes_used(session, owner_id, artifact_bytes)
        await session.commit()
    return Response(status_code=204)


@router.get("/{run_id}/artifacts/", include_in_schema=False)
async def reject_artifact_directory_download(run_id: int) -> None:
    # Keep the pre-existing download contract: the directory itself is never
    # downloadable, even though DELETE now exists at the slashless path.
    raise HTTPException(status_code=404, detail="산출물을 찾을 수 없습니다.")


@router.get("/{run_id}/inference-images", response_model=InferenceImagePage)
async def list_run_inference_images(
    run_id: int,
    session: Session,
    current_user: CurrentUserDep,
    cursor: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 48,
) -> InferenceImagePage:
    run = await _owned_run_or_404(session, run_id, current_user.id)
    _ensure_run_can_infer(run)
    split = _inference_split(run)

    filters = [
        RunImage.run_id == run_id,
        RunImage.split == split,
        RunImage.image_id.is_not(None),
    ]
    if cursor is not None:
        filters.append(RunImage.image_id > cursor)
    rows = (
        await session.execute(
            select(RunImage, Image)
            .join(Image, Image.id == RunImage.image_id)
            .where(*filters)
            .order_by(RunImage.image_id.asc())
            .limit(limit + 1)
        )
    ).all()
    total = None
    if cursor is None:
        total = await session.scalar(
            select(func.count(RunImage.id)).where(
                RunImage.run_id == run_id,
                RunImage.split == split,
                RunImage.image_id.is_not(None),
            )
        )
    visible_rows = rows[:limit]
    return InferenceImagePage(
        split=split,
        items=[
            InferenceImageRow(
                id=run_image.id,
                image_id=image.id,
                filename=run_image.filename,
            )
            for run_image, image in visible_rows
        ],
        next_cursor=(
            visible_rows[-1][0].image_id if len(rows) > limit else None
        ),
        total=(total or 0) if cursor is None else None,
    )


@router.post(
    "/{run_id}/inference-images/{run_image_id}",
    response_class=Response,
)
async def infer_run_image(
    run_id: int,
    run_image_id: int,
    request: Request,
    session: Session,
    current_user: CurrentUserDep,
) -> Response:
    run = await _owned_run_or_404(session, run_id, current_user.id)
    _ensure_run_can_infer(run)
    split = _inference_split(run)

    row = (
        await session.execute(
            select(RunImage, Image)
            .join(Image, Image.id == RunImage.image_id)
            .where(
                RunImage.id == run_image_id,
                RunImage.run_id == run_id,
                RunImage.split == split,
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"이 run의 {split} 이미지를 찾을 수 없습니다.",
        )
    _run_image, image = row

    try:
        image_path = contained_storage_path(
            request.app.state.settings.storage_dir,
            image.display_path or image.file_path,
        )
    except StorageBoundaryError as error:
        raise HTTPException(
            status_code=410,
            detail="원본 데이터셋 이미지가 삭제되었습니다.",
        ) from error
    if not image_path.is_file():
        raise HTTPException(
            status_code=410,
            detail="원본 데이터셋 이미지가 삭제되었습니다.",
        )

    best_path = _artifact_file_or_404(
        request.app.state.settings.storage_dir,
        run,
        "best.pt",
    )
    try:
        rendered = await asyncio.to_thread(
            render_prediction_file,
            best_path,
            image_path,
            run.imgsz,
        )
    except Exception as error:
        logger.exception(
            "run %s image %s inference failed",
            run_id,
            run_image_id,
        )
        raise HTTPException(
            status_code=500,
            detail="추론 중 오류가 발생했습니다.",
        ) from error
    return Response(
        content=rendered,
        media_type="image/png",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{run_id}/artifacts/{name}", response_class=FileResponse)
async def download_run_artifact(
    run_id: int,
    name: str,
    request: Request,
    session: Session,
    current_user: CurrentUserDep,
) -> FileResponse:
    if name not in ARTIFACT_NAMES:
        raise HTTPException(status_code=404, detail="산출물을 찾을 수 없습니다.")
    run = await _owned_run_or_404(session, run_id, current_user.id)
    if run.artifacts_deleted_at is not None:
        raise HTTPException(status_code=410, detail="산출물이 삭제되었습니다.")

    resolved_candidate = _artifact_file_or_404(
        request.app.state.settings.storage_dir,
        run,
        name,
    )
    return FileResponse(
        resolved_candidate,
        filename=name,
        media_type="application/octet-stream",
    )
