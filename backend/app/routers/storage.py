"""Authenticated storage quota summary."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import CurrentUserDep
from app.services.quota import get_referenced_bytes, quota_status


router = APIRouter(prefix="/api/storage", tags=["storage"])


class StorageQuotaResponse(BaseModel):
    used_bytes: int
    referenced_bytes: int
    limit_bytes: int


@router.get("", response_model=StorageQuotaResponse)
async def storage_quota(
    request: Request,
    current_user: CurrentUserDep,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StorageQuotaResponse:
    status = await quota_status(
        session,
        current_user.id,
        limit_bytes=request.app.state.settings.quota_bytes_per_user,
        required_bytes=0,
    )
    return StorageQuotaResponse(
        used_bytes=status.used_bytes,
        referenced_bytes=await get_referenced_bytes(session, current_user.id),
        limit_bytes=status.limit_bytes,
    )
