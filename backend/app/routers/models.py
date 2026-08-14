"""Trusted preset model discovery endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from app.inference.models_dir import list_all_models


router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("")
async def list_models() -> list[dict[str, str | float | None]]:
    return list_all_models()
