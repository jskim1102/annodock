"""Bearer authentication middleware for the host application's API surface."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth.jwks import JWKSVerificationError, JWKSVerifier
from app.deps import CurrentUser


def _is_protected_api_path(path: str) -> bool:
    is_api_path = path == "/api" or path.startswith("/api/")
    return is_api_path and path != "/api/health"


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": detail},
        headers={"WWW-Authenticate": "Bearer"},
    )


class AuthenticationMiddleware(BaseHTTPMiddleware):
    """Verify RS256 bearer tokens before protected API handlers execute."""

    def __init__(self, app: Any, *, verifier: JWKSVerifier) -> None:
        super().__init__(app)
        self._verifier = verifier

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not _is_protected_api_path(request.url.path):
            return await call_next(request)

        authorization = request.headers.get("Authorization")
        if not authorization:
            return _unauthorized("인증이 필요합니다.")
        scheme, separator, token = authorization.partition(" ")
        if (
            not separator
            or scheme.lower() != "bearer"
            or not token
            or any(character.isspace() for character in token)
        ):
            return _unauthorized("유효한 Bearer 토큰이 필요합니다.")

        try:
            claims = await self._verifier.verify(token)
        except JWKSVerificationError:
            return _unauthorized("유효하지 않은 인증 토큰입니다.")

        subject = claims.get("sub")
        if (
            not isinstance(subject, str)
            or not subject.isascii()
            or not subject.isdigit()
        ):
            return _unauthorized("유효하지 않은 인증 토큰입니다.")
        user_id = int(subject)
        if user_id <= 0:
            return _unauthorized("유효하지 않은 인증 토큰입니다.")

        request.state.current_user = CurrentUser(
            id=user_id,
            subject=subject,
            claims=dict(claims),
        )
        return await call_next(request)

