from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass

import httpx
import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from app.auth.jwks import JWKSVerifier
from app.deps import CurrentUserDep
from app.main import create_app


@dataclass(frozen=True)
class SigningMaterial:
    private_key: bytes
    public_jwk: dict[str, str]

    @property
    def kid(self) -> str:
        return self.public_jwk["kid"]

    def issue(
        self,
        user_id: int = 42,
        *,
        expires_at: int | None = None,
        kid: str | None = None,
    ) -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "sub": str(user_id),
                "iat": now,
                "exp": expires_at if expires_at is not None else now + 300,
            },
            self.private_key,
            algorithm="RS256",
            headers={"kid": kid or self.kid},
        )


@pytest.fixture(scope="module")
def signing_material() -> SigningMaterial:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update(
        {
            "use": "sig",
            "alg": "RS256",
            "kid": "auth-test-key",
        }
    )
    return SigningMaterial(private_key=private_pem, public_jwk=public_jwk)


@pytest_asyncio.fixture
async def protected_client(test_settings, signing_material: SigningMaterial):
    fetch_count = 0

    def jwks_response(request: httpx.Request) -> httpx.Response:
        nonlocal fetch_count
        fetch_count += 1
        assert request.url.path == "/.well-known/jwks.json"
        return httpx.Response(
            200,
            json={"keys": [signing_material.public_jwk]},
        )

    jwks_http = httpx.AsyncClient(
        transport=httpx.MockTransport(jwks_response),
        base_url="http://auth.test",
    )
    verifier = JWKSVerifier(
        "http://auth.test",
        http_client=jwks_http,
        cache_ttl_seconds=60,
    )
    application = create_app(
        test_settings,
        auto_start_jobs=False,
        jwks_verifier=verifier,
    )

    @application.get("/api/whoami")
    async def whoami(current_user: CurrentUserDep) -> dict[str, int | str]:
        return {
            "id": current_user.id,
            "subject": current_user.subject,
        }

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        yield client, lambda: fetch_count

    await jwks_http.aclose()
    await application.state.engine.dispose()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def tamper_signature(token: str) -> str:
    header, payload, encoded_signature = token.split(".")
    padded = encoded_signature + "=" * (-len(encoded_signature) % 4)
    signature = bytearray(base64.urlsafe_b64decode(padded))
    signature[0] ^= 0x01
    changed = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{header}.{payload}.{changed}"


@pytest.mark.asyncio
async def test_health_is_the_only_unprotected_api_path(protected_client) -> None:
    client, _fetch_count = protected_client

    health = await client.get("/api/health")
    models = await client.get("/api/models")
    datasets = await client.get("/api/datasets")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert models.status_code == 401
    assert datasets.status_code == 401
    assert models.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_valid_rs256_token_exposes_current_user_and_caches_kid(
    protected_client,
    signing_material: SigningMaterial,
) -> None:
    client, fetch_count = protected_client
    headers = bearer(signing_material.issue(user_id=73))

    first = await client.get("/api/whoami", headers=headers)
    second = await client.get("/api/whoami", headers=headers)

    assert first.status_code == 200
    assert first.json() == {"id": 73, "subject": "73"}
    assert second.status_code == 200
    assert fetch_count() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", [None, "Basic abc", "Bearer", "Bearer a b"])
async def test_missing_or_malformed_bearer_is_401(
    protected_client,
    authorization: str | None,
) -> None:
    client, _fetch_count = protected_client
    headers = {"Authorization": authorization} if authorization else {}

    response = await client.get("/api/models", headers=headers)

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_expired_token_is_401(
    protected_client,
    signing_material: SigningMaterial,
) -> None:
    client, _fetch_count = protected_client
    token = signing_material.issue(expires_at=int(time.time()) - 10)

    response = await client.get("/api/whoami", headers=bearer(token))

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_tampered_token_is_401(
    protected_client,
    signing_material: SigningMaterial,
) -> None:
    client, _fetch_count = protected_client
    token = tamper_signature(signing_material.issue())

    response = await client.get("/api/whoami", headers=bearer(token))

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_hs256_confusion_token_is_401(
    protected_client,
    signing_material: SigningMaterial,
) -> None:
    client, fetch_count = protected_client
    now = int(time.time())
    token = jwt.encode(
        {"sub": "42", "iat": now, "exp": now + 300},
        "attacker-controlled-hmac-secret",
        algorithm="HS256",
        headers={"kid": signing_material.kid},
    )

    response = await client.get("/api/whoami", headers=bearer(token))

    assert response.status_code == 401
    assert fetch_count() == 0


@pytest.mark.asyncio
async def test_unknown_kid_refreshes_jwks_then_returns_401(
    protected_client,
    signing_material: SigningMaterial,
) -> None:
    client, fetch_count = protected_client
    valid = signing_material.issue()
    unknown = signing_material.issue(kid="rotated-but-unpublished")

    assert (await client.get("/api/whoami", headers=bearer(valid))).status_code == 200
    response = await client.get("/api/whoami", headers=bearer(unknown))

    assert response.status_code == 401
    assert fetch_count() == 2


@pytest.mark.asyncio
async def test_non_numeric_subject_is_401(
    protected_client,
    signing_material: SigningMaterial,
) -> None:
    client, _fetch_count = protected_client
    now = int(time.time())
    token = jwt.encode(
        {"sub": "not-an-auth-user-id", "iat": now, "exp": now + 300},
        signing_material.private_key,
        algorithm="RS256",
        headers={"kid": signing_material.kid},
    )

    response = await client.get("/api/whoami", headers=bearer(token))

    assert response.status_code == 401
