"""Dataset training submission endpoint."""

from __future__ import annotations

import math
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import CurrentUserDep
from app.inference.models_dir import is_preset
from app.models import Annotation, Dataset, Image
from app.services.cancel import (
    ProcessGroupTerminationError,
    TrainingRunNotCancelableError,
    TrainingRunNotFoundError,
    cancel_training_run,
)
from app.services.training import (
    TrainingConfig,
    TrainingRequestError,
    start_training,
)
from app.training_params import TRAINING_ARGUMENT_KEYS
from app.training_recommendation import (
    TrainingDatasetProfile,
    recommend_training,
)


router = APIRouter(prefix="/api/datasets", tags=["training"])
runs_router = APIRouter(prefix="/api/runs", tags=["training"])
Session = Annotated[AsyncSession, Depends(get_session)]


class TrainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weights: str
    epochs: int = Field(ge=1)
    imgsz: int = Field(ge=1)
    batch: int
    split_mode: Literal["2way", "3way"] = "2way"
    ratios: dict[str, float] | None = None
    seed: int | None = None
    exclude_unlabeled_images: bool = False
    include_unlabeled_images_in_test: bool = False
    device: Literal[0] = 0
    optimizer: Literal[
        "Adam",
        "Adamax",
        "AdamW",
        "NAdam",
        "RAdam",
        "RMSProp",
        "SGD",
        "MuSGD",
        "auto",
    ] = "auto"
    lr0: float = Field(default=0.01, gt=0, le=1)
    lrf: float = Field(default=0.01, ge=0, le=1)
    warmup_epochs: float = Field(default=3.0, ge=0)
    cos_lr: bool = True
    patience: int = Field(default=30, ge=0)
    augment: bool = True
    mosaic: float = Field(default=1.0, ge=0, le=1)
    mixup: float = Field(default=0.0, ge=0, le=1)
    copy_paste: Literal[0.0] = 0.0
    close_mosaic: int = Field(default=10, ge=0)
    hsv_h: float = Field(default=0.015, ge=0, le=1)
    hsv_s: float = Field(default=0.7, ge=0, le=1)
    hsv_v: float = Field(default=0.4, ge=0, le=1)
    fliplr: float = Field(default=0.5, ge=0, le=1)
    scale: float = Field(default=0.5, ge=0, le=1)
    translate: float = Field(default=0.1, ge=0, le=1)
    workers: int = Field(default=8, ge=0, le=128)
    cache: Literal["none", "ram", "disk"] = "ram"
    amp: bool = True
    compile: bool = True
    deterministic: bool = False
    save_period: int = 25
    multi_scale: float = Field(default=0.0, ge=0, le=1)

    @field_validator("weights")
    @classmethod
    def validate_weights(cls, value: str) -> str:
        if not is_preset(value):
            raise ValueError("weights must be an allowed preset")
        return value

    @field_validator("batch")
    @classmethod
    def validate_batch(cls, value: int) -> int:
        if value != -1 and value < 1:
            raise ValueError("batch must be -1 or at least 1")
        return value

    @field_validator("save_period")
    @classmethod
    def validate_save_period(cls, value: int) -> int:
        if value != -1 and value < 1:
            raise ValueError("save_period must be -1 or at least 1")
        return value

    @model_validator(mode="after")
    def validate_ratios(self) -> TrainRequest:
        if self.compile and self.multi_scale > 0:
            raise ValueError("compile and multi_scale cannot be enabled together")
        if self.include_unlabeled_images_in_test and self.split_mode != "3way":
            raise ValueError(
                "include_unlabeled_images_in_test requires 3way split"
            )
        if (
            self.include_unlabeled_images_in_test
            and not self.exclude_unlabeled_images
        ):
            raise ValueError(
                "include_unlabeled_images_in_test requires exclude_unlabeled_images"
            )
        ratios = self.ratios
        if ratios is None:
            ratios = (
                {"train": 0.8, "valid": 0.2}
                if self.split_mode == "2way"
                else {"train": 0.7, "valid": 0.2, "test": 0.1}
            )
        expected = (
            {"train", "valid"}
            if self.split_mode == "2way"
            else {"train", "valid", "test"}
        )
        if set(ratios) != expected:
            raise ValueError("ratio keys must match split_mode")
        if any(not math.isfinite(value) or value < 0 for value in ratios.values()):
            raise ValueError("ratios must be finite non-negative values")
        if not math.isclose(sum(ratios.values()), 1.0, abs_tol=1e-9):
            raise ValueError("ratios must sum to one")
        self.ratios = {
            split: ratios[split]
            for split in ("train", "valid", "test")
            if split in ratios
        }
        return self

    def training_args(self) -> dict[str, object]:
        values = self.model_dump(include=set(TRAINING_ARGUMENT_KEYS))
        return {key: values[key] for key in TRAINING_ARGUMENT_KEYS}


class TrainResponse(BaseModel):
    run_id: int
    warnings: list[str]


class TrainingRecommendationResponse(BaseModel):
    policy_version: str
    total_images: int
    labeled_images: int
    unlabeled_images: int
    train_images: int
    total_instances: int
    instances_per_image: float
    small_object_ratio: float
    epochs: int
    imgsz: int
    batch: int
    optimizer: Literal["auto"]
    lr0: float
    warmup_epochs: float
    patience: int
    mosaic: float
    mixup: float
    scale: float
    amp: bool
    close_mosaic: int
    copy_paste: Literal[0.0]
    compile: bool
    effective_max_imgsz: int
    reasons: list[str]


class CancelResponse(BaseModel):
    run_id: int
    state: Literal["canceled"]


@router.get(
    "/{dataset_id}/training-recommendation",
    response_model=TrainingRecommendationResponse,
)
async def training_recommendation(
    dataset_id: int,
    current_user: CurrentUserDep,
    session: Session,
    weights: str = Query(),
    imgsz: int = Query(default=640, ge=1),
    multi_scale: float = Query(default=0.0, ge=0, le=1),
    train_ratio: float = Query(default=0.7, gt=0, le=1),
    exclude_unlabeled_images: bool = Query(default=False),
    include_unlabeled_images_in_test: bool = Query(default=False),
) -> TrainingRecommendationResponse:
    if not is_preset(weights):
        raise HTTPException(status_code=422, detail="허용된 preset이 아닙니다.")
    dataset = await session.scalar(
        select(Dataset).where(
            Dataset.id == dataset_id,
            Dataset.owner_id == current_user.id,
        )
    )
    if dataset is None:
        raise HTTPException(status_code=404, detail="데이터셋을 찾을 수 없습니다.")

    annotation_count, small_count, labeled_image_count = (
        await session.execute(
            select(
                func.count(Annotation.id),
                func.coalesce(
                    func.sum(
                        case(
                            (Annotation.w * Annotation.h <= 0.01, 1),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.count(
                    func.distinct(
                        case(
                            (
                                Image.has_label_source.is_(True)
                                | Annotation.id.is_not(None),
                                Image.id,
                            ),
                            else_=None,
                        )
                    )
                ),
            )
            .select_from(Image)
            .outerjoin(Annotation, Annotation.image_id == Image.id)
            .where(Image.dataset_id == dataset_id)
        )
    ).one()
    count = int(annotation_count or 0)
    labeled_images = int(labeled_image_count or 0)
    eligible_images = (
        labeled_images
        if exclude_unlabeled_images or include_unlabeled_images_in_test
        else dataset.image_count
    )
    profile = TrainingDatasetProfile(
        image_count=eligible_images,
        annotation_count=count,
        small_object_ratio=(int(small_count or 0) / count if count else 0.0),
    )
    recommendation = recommend_training(
        profile=profile,
        weights=weights,
        requested_imgsz=imgsz,
        multi_scale=multi_scale,
        train_ratio=train_ratio,
    )
    return TrainingRecommendationResponse(
        **{
            **recommendation.__dict__,
            "total_images": dataset.image_count,
            "labeled_images": labeled_images,
            "unlabeled_images": dataset.image_count - labeled_images,
            "reasons": list(recommendation.reasons),
        }
    )


@router.post("/{dataset_id}/train", status_code=201, response_model=TrainResponse)
async def submit_training(
    dataset_id: int,
    body: TrainRequest,
    request: Request,
    session: Session,
    current_user: CurrentUserDep,
) -> TrainResponse:
    assert body.ratios is not None
    try:
        result = await start_training(
            session,
            request.app.state.settings,
            dataset_id,
            current_user.id,
            TrainingConfig(
                weights=body.weights,
                epochs=body.epochs,
                imgsz=body.imgsz,
                batch=body.batch,
                split_mode=body.split_mode,
                ratios=body.ratios,
                seed=body.seed,
                exclude_unlabeled_images=body.exclude_unlabeled_images,
                include_unlabeled_images_in_test=(
                    body.include_unlabeled_images_in_test
                ),
                training_args=body.training_args(),
            ),
        )
    except TrainingRequestError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=error.detail,
        ) from error
    return TrainResponse(run_id=result.run_id, warnings=list(result.warnings))


@runs_router.post(
    "/{run_id}/cancel",
    status_code=202,
    response_model=CancelResponse,
)
async def cancel_training(
    run_id: int,
    request: Request,
    current_user: CurrentUserDep,
) -> CancelResponse:
    try:
        result = await cancel_training_run(
            request.app.state.session_factory,
            run_id,
            current_user.id,
            storage_dir=request.app.state.settings.storage_dir,
        )
    except TrainingRunNotFoundError as error:
        raise HTTPException(
            status_code=404,
            detail="학습 run을 찾을 수 없습니다.",
        ) from error
    except TrainingRunNotCancelableError as error:
        raise HTTPException(
            status_code=409,
            detail="진행 중인 학습만 취소할 수 있습니다.",
        ) from error
    except ProcessGroupTerminationError as error:
        raise HTTPException(
            status_code=500,
            detail="학습 프로세스 종료를 확인하지 못했습니다.",
        ) from error
    return CancelResponse(run_id=result.run_id, state="canceled")
