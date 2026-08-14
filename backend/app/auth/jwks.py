"""RS256 access-token verification backed by the auth-service JWKS."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm


ALGORITHM = "RS256"
JWKS_PATH = "/.well-known/jwks.json"
DEFAULT_CACHE_TTL_SECONDS = 300.0
DEFAULT_HTTP_TIMEOUT_SECONDS = 5.0


class JWKSVerificationError(Exception):
    """Raised when a bearer token cannot be verified against trusted keys."""


class JWKSVerifier:
    """Resolve signing keys by ``kid`` and verify auth-service access tokens.

    The cache is refreshed when it expires or when a token presents an unknown
    ``kid``. The latter permits key rotation without accepting a key that was
    not published by the auth service.
    """

    def __init__(
        self,
        auth_base_url: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        base_url = auth_base_url.rstrip("/")
        if not base_url:
            raise ValueError("auth_base_url must not be empty")
        if cache_ttl_seconds <= 0:
            raise ValueError("cache_ttl_seconds must be positive")

        self._jwks_url = f"{base_url}{JWKS_PATH}"
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._cache_ttl_seconds = cache_ttl_seconds
        self._keys: dict[str, Any] = {}
        self._expires_at = 0.0
        self._refresh_lock = asyncio.Lock()

    async def aclose(self) -> None:
        """Close the lazily-created HTTP client owned by this verifier."""

        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def verify(self, token: str) -> Mapping[str, Any]:
        """Return verified claims or raise ``JWKSVerificationError``."""

        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise JWKSVerificationError("malformed token header") from exc

        # Reject algorithm confusion before doing any network lookup.
        if header.get("alg") != ALGORITHM:
            raise JWKSVerificationError("unsupported signing algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise JWKSVerificationError("missing signing key id")

        key = await self._key_for(kid)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                options={"require": ["exp", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise JWKSVerificationError("invalid access token") from exc
        if not isinstance(claims, dict):
            raise JWKSVerificationError("invalid token claims")
        return claims

    async def _key_for(self, kid: str) -> Any:
        now = time.monotonic()
        if now < self._expires_at and kid in self._keys:
            return self._keys[kid]

        async with self._refresh_lock:
            now = time.monotonic()
            if now < self._expires_at and kid in self._keys:
                return self._keys[kid]
            await self._refresh()
            key = self._keys.get(kid)
            if key is None:
                raise JWKSVerificationError("unknown signing key id")
            return key

    async def _refresh(self) -> None:
        try:
            if self._http_client is None:
                self._http_client = httpx.AsyncClient(
                    timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
                    follow_redirects=False,
                )
            response = await self._http_client.get(self._jwks_url)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise JWKSVerificationError("unable to load signing keys") from exc

        raw_keys = payload.get("keys") if isinstance(payload, dict) else None
        if not isinstance(raw_keys, list):
            raise JWKSVerificationError("invalid JWKS payload")

        parsed: dict[str, Any] = {}
        for raw_key in raw_keys:
            if not isinstance(raw_key, dict):
                continue
            kid = raw_key.get("kid")
            if not isinstance(kid, str) or not kid:
                continue
            if kid in parsed:
                raise JWKSVerificationError("duplicate signing key id")
            if (
                raw_key.get("kty") != "RSA"
                or raw_key.get("alg") != ALGORITHM
                or raw_key.get("use") != "sig"
            ):
                continue
            try:
                parsed[kid] = RSAAlgorithm.from_jwk(raw_key)
            except (KeyError, TypeError, ValueError) as exc:
                raise JWKSVerificationError("invalid RSA signing key") from exc

        if not parsed:
            raise JWKSVerificationError("JWKS contains no trusted signing keys")
        self._keys = parsed
        self._expires_at = time.monotonic() + self._cache_ttl_seconds

