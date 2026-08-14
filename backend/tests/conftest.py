from __future__ import annotations

import os
import json
import time
from collections.abc import AsyncIterator
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import jwt
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from dotenv import dotenv_values
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import delete


ROOT_DIR = Path(__file__).resolve().parents[2]
ENV_VALUES = dotenv_values(ROOT_DIR / ".env")
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL") or ENV_VALUES.get(
    "TEST_DATABASE_URL"
)
if not TEST_DATABASE_URL:
    raise RuntimeError("TEST_DATABASE_URL is required for backend tests")

# Every test process is forced onto the dedicated test database before app imports.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

from app.auth.jwks import JWKSVerifier  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Dataset, Project, TrainingRun, UserStorage  # noqa: E402


@dataclass(frozen=True)
class TestAuthIssuer:
    """Issue real RS256 access tokens for authenticated API tests."""

    private_key: bytes
    public_jwk: dict[str, str]

    def token(self, user_id: int) -> str:
        now = int(time.time())
        return jwt.encode(
            {"sub": str(user_id), "iat": now, "exp": now + 300},
            self.private_key,
            algorithm="RS256",
            headers={"kid": self.public_jwk["kid"]},
        )


@pytest.fixture(scope="session", autouse=True)
def migrate_test_database() -> None:
    """Bring the dedicated test database to the current Alembic head once."""

    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    alembic_config = Config(str(ROOT_DIR / "backend" / "alembic.ini"))
    command.upgrade(alembic_config, "head")


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    return Settings().model_copy(update={"storage_dir": tmp_path / "storage"})


@pytest.fixture(scope="session")
def auth_issuer() -> TestAuthIssuer:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update(
        {"use": "sig", "alg": "RS256", "kid": "backend-test-key"}
    )
    return TestAuthIssuer(private_key=private_pem, public_jwk=public_jwk)


@pytest.fixture
def auth_headers(
    auth_issuer: TestAuthIssuer,
) -> Callable[[int], dict[str, str]]:
    """Build an Authorization header for any auth-service user id."""

    def build(user_id: int = 1) -> dict[str, str]:
        return {"Authorization": f"Bearer {auth_issuer.token(user_id)}"}

    return build


@pytest_asyncio.fixture
async def test_jwks_verifier(auth_issuer: TestAuthIssuer):
    def jwks_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"keys": [auth_issuer.public_jwk]})

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(jwks_response),
        base_url="http://auth.test",
    )
    verifier = JWKSVerifier("http://auth.test", http_client=http_client)
    yield verifier
    await http_client.aclose()


@pytest_asyncio.fixture
async def app(test_settings: Settings, test_jwks_verifier: JWKSVerifier):
    application = create_app(
        test_settings,
        auto_start_jobs=False,
        jwks_verifier=test_jwks_verifier,
    )
    yield application
    await application.state.engine.dispose()


@pytest_asyncio.fixture
async def client(
    app,
    auth_headers: Callable[[int], dict[str, str]],
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers=auth_headers(1),
    ) as http_client:
        yield http_client


@pytest_asyncio.fixture(autouse=True)
async def cleanup_test_datasets() -> AsyncIterator[None]:
    from app.db import create_engine, create_session_factory

    engine = create_engine(TEST_DATABASE_URL)
    session_factory = create_session_factory(engine)

    async def cleanup() -> None:
        async with session_factory() as session:
            await session.execute(
                delete(TrainingRun).where(
                    TrainingRun.dataset_name.like("test-%")
                )
            )
            await session.execute(
                delete(Dataset).where(Dataset.name.like("test-%"))
            )
            await session.execute(
                delete(Project).where(Project.name.like("test-%"))
            )
            await session.execute(delete(UserStorage))
            await session.commit()

    await cleanup()
    yield
    await cleanup()
    await engine.dispose()
