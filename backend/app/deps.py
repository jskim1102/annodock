"""FastAPI dependencies shared by authenticated API routers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """The auth-service user established for one request."""

    id: int
    subject: str
    claims: Mapping[str, Any]


def get_current_user(request: Request) -> CurrentUser:
    """Return middleware-verified identity, failing closed if it is absent."""

    current_user = getattr(request.state, "current_user", None)
    if not isinstance(current_user, CurrentUser):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="인증이 필요합니다.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return current_user


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]

