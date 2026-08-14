"""First-release integration contracts across auth, data, and training boundaries.

The auth service is deployed separately from this backend.  The product flow uses
its public signup/login contract and a real RS256 token accepted by the backend.
An isolated subprocess additionally executes the auth module's real local-auth and
OAuth routes against its dedicated ``auth_test`` database.  OAuth provider network
I/O is replaced only at the transport boundary by that module's own E2E tests.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from dotenv import dotenv_values
from PIL import Image as PillowImage
from sqlalchemy.engine import make_url

from app.models import Dataset, TrainingRun, UserStorage
from app.services import training
from app.services.ingest import run_upload_batch_job
from app.services.storage import contained_storage_path
from app.worker import callbacks, train_worker


pytestmark = pytest.mark.asyncio

AUTH_SERVICE_ROOT = Path(__file__).resolve().parents[4] / "modules" / "auth-service"

AUTH_LOCAL_ROUND_TRIP = r"""
import asyncio
import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db import get_session
from app.main import app
from app.tokens import verify_access


async def main():
    engine = create_async_engine(__import__("os").environ["TEST_DATABASE_URL"])
    connection = await engine.connect()
    outer_transaction = await connection.begin()

    async def isolated_session():
        async with AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        ) as session:
            yield session

    app.dependency_overrides[get_session] = isolated_session
    tag = uuid.uuid4().hex[:12]
    credentials = {
        "username": f"e2e-{tag}",
        "email": f"e2e-{tag}@e.com",
        "password": "S3cret-pass!",
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://auth.test",
        ) as client:
            signup = await client.post("/auth/signup", json=credentials)
            assert signup.status_code == 201, signup.text
            login = await client.post(
                "/auth/login",
                json={
                    "identifier": credentials["username"],
                    "password": credentials["password"],
                },
            )
            assert login.status_code == 200, login.text
            body = login.json()
            assert body["access_token"] and body["refresh_token"]
            assert login.cookies.get("refresh_token") == body["refresh_token"]
            assert "httponly" in login.headers.get("set-cookie", "").lower()
            claims = verify_access(body["access_token"])
            assert claims["sub"] == str(signup.json()["id"])
            assert claims["email"] == credentials["email"]
    finally:
        app.dependency_overrides.clear()
        await outer_transaction.rollback()
        await connection.close()
        await engine.dispose()


asyncio.run(main())
print("AUTH_LOCAL_ROUND_TRIP_OK")
"""


@dataclass
class _Account:
    user_id: int
    username: str
    email: str
    password: str


class _AuthServiceContract:
    """Hermetic auth-service HTTP boundary used by the product-flow test."""

    def __init__(self, auth_issuer) -> None:
        self._issuer = auth_issuer
        self._accounts: dict[str, _Account] = {}
        self._next_user_id = 61_001

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if request.method == "POST" and request.url.path == "/auth/signup":
            account = _Account(
                user_id=self._next_user_id,
                username=body["username"],
                email=body["email"],
                password=body["password"],
            )
            self._next_user_id += 1
            self._accounts[account.username] = account
            self._accounts[account.email] = account
            return httpx.Response(
                201,
                json={
                    "id": account.user_id,
                    "username": account.username,
                    "email": account.email,
                },
            )
        if request.method == "POST" and request.url.path == "/auth/login":
            account = self._accounts.get(body["identifier"])
            if account is None or account.password != body["password"]:
                return httpx.Response(401, json={"detail": "invalid credentials"})
            return httpx.Response(
                200,
                json={
                    "access_token": self._issuer.token(account.user_id),
                    "refresh_token": f"test-refresh-{account.user_id}",
                },
            )
        return httpx.Response(404)


async def _signup_and_login(
    auth_client: httpx.AsyncClient,
    *,
    suffix: str,
) -> tuple[int, str]:
    username = f"e2e-{suffix}-{uuid4().hex[:10]}"
    email = f"{username}@example.test"
    password = "integration-password"
    signup = await auth_client.post(
        "/auth/signup",
        json={"username": username, "email": email, "password": password},
    )
    assert signup.status_code == 201, signup.text
    login = await auth_client.post(
        "/auth/login",
        json={"identifier": username, "password": password},
    )
    assert login.status_code == 200, login.text
    assert login.json()["refresh_token"]
    return int(signup.json()["id"]), str(login.json()["access_token"])


def _jpeg_bytes(index: int) -> bytes:
    payload = io.BytesIO()
    PillowImage.new(
        "RGB",
        (64, 48),
        (20 + index * 10, 40 + index * 5, 80 + index * 3),
    ).save(payload, "JPEG")
    return payload.getvalue()


def _source_yolo_zip() -> tuple[bytes, int, int]:
    members: dict[str, bytes] = {"classes.txt": b"object\n"}
    for index in range(10):
        stem = f"image-{index:02d}"
        members[f"images/train/{stem}.jpg"] = _jpeg_bytes(index)
        members[f"labels/train/{stem}.txt"] = b"0 0.5 0.5 0.2 0.2\n"
    # A valid label with no matching image must survive collection as an issue.
    members["labels/train/orphan.txt"] = b"0 0.5 0.5 0.1 0.1\n"

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return payload.getvalue(), sum(map(len, members.values())), len(members)



async def _upload_zip(
    client: httpx.AsyncClient,
    app,
    dataset_id: int,
    payload: bytes,
    *,
    extracted_bytes: int,
    file_count: int,
) -> int:
    preflight = await client.post(
        f"/api/datasets/{dataset_id}/upload-batches/preflight",
        json={
            "total_size": len(payload),
            "largest_file_size": len(payload),
            "file_count": file_count,
            "expected_extracted_size": extracted_bytes,
        },
    )
    assert preflight.status_code == 204, preflight.text
    created = await client.post(
        f"/api/datasets/{dataset_id}/uploads",
        json={
            "filename": "dataset.zip",
            "size": len(payload),
            "chunk_size": len(payload),
            "kind": "zip",
            "file_count": file_count,
            "expected_extracted_size": extracted_bytes,
        },
    )
    assert created.status_code == 201, created.text
    upload_id = int(created.json()["upload_id"])
    chunk = await client.put(
        f"/api/uploads/{upload_id}/chunks/0",
        content=payload,
    )
    assert chunk.status_code == 204, chunk.text
    completed = await client.post(
        f"/api/datasets/{dataset_id}/upload-batches/complete",
        json={"upload_ids": [upload_id]},
    )
    assert completed.status_code == 202, completed.text
    job_id = int(completed.json()["job_id"])
    await run_upload_batch_job(
        app.state.settings,
        app.state.session_factory,
        job_id,
        [upload_id],
    )
    job = await client.get(f"/api/jobs/{job_id}")
    assert job.status_code == 200
    assert job.json()["state"] == "done", job.text
    return job_id


def _prepare_training_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    weights_dir = tmp_path / "weights"
    weights_dir.mkdir()
    (weights_dir / "yolo26n.pt").write_bytes(b"preset-weight-contract")
    monkeypatch.setattr(training, "WEIGHTS_DIR", weights_dir)
    monkeypatch.setattr(training, "is_container_environment", lambda: False)
    monkeypatch.setattr(training.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(
        training.torch.cuda,
        "mem_get_info",
        lambda: (24 * 1024**3, 24 * 1024**3),
    )
    monkeypatch.setattr(
        training,
        "spawn_worker",
        lambda _run_id, _owner_id, _out_dir, _database_url: training.SpawnedWorker(
            pid=4242,
            pid_started_at="123456",
            boot_id="test-boot-id",
        ),
    )


def _finish_training_through_worker_boundary(
    app,
    *,
    owner_id: int,
    run_id: int,
    out_dir: Path,
) -> bytes:
    dsn = str(app.state.settings.database_url).replace("+asyncpg", "", 1)
    log_path = out_dir / "artifacts" / "log"
    log_path.write_text("epoch 1/2 complete\n", encoding="utf-8")

    trainer_state = SimpleNamespace(
        epoch=0,
        tloss=[0.31, 0.22, 0.13],
        metrics={
            "val/box_loss": 0.2,
            "metrics/mAP50(B)": 0.71,
            "metrics/mAP50-95(B)": 0.53,
        },
        lr={"lr/pg0": 0.01},
        label_loss_items=lambda _loss: {
            "train/box_loss": 0.31,
            "train/cls_loss": 0.22,
            "train/dfl_loss": 0.13,
        },
    )
    callbacks.make_epoch_callback(run_id, owner_id, dsn)(trainer_state)

    train_dir = out_dir / "workdir" / "train"
    weights_dir = train_dir / "weights"
    weights_dir.mkdir(parents=True)
    best_bytes = b"integration-best-weights"
    best_path = weights_dir / "best.pt"
    last_path = weights_dir / "last.pt"
    csv_path = train_dir / "results.csv"
    best_path.write_bytes(best_bytes)
    last_path.write_bytes(b"integration-last-weights")
    csv_path.write_text("epoch,map50\n1,0.71\n", encoding="utf-8")

    completed = train_worker.complete_run(
        run_id,
        owner_id,
        dsn,
        out_dir,
        SimpleNamespace(best=best_path, last=last_path, csv=csv_path),
        storage_dir=app.state.settings.storage_dir,
    )
    assert completed is True
    return best_bytes


def _assert_quota_rejected(response: httpx.Response) -> None:
    assert response.status_code == 413, response.text
    detail = response.json()["detail"]
    assert "잔여" in detail and "필요" in detail


async def test_signup_to_artifact_round_trip_enforces_owner_and_quota_boundaries(
    app,
    auth_issuer,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    auth_transport = httpx.MockTransport(_AuthServiceContract(auth_issuer))
    async with httpx.AsyncClient(
        transport=auth_transport,
        base_url="http://auth.test",
    ) as auth_client:
        owner_id, owner_token = await _signup_and_login(
            auth_client,
            suffix="owner",
        )
        other_id, other_token = await _signup_and_login(
            auth_client,
            suffix="other",
        )
        exhausted_id, exhausted_token = await _signup_and_login(
            auth_client,
            suffix="quota",
        )

    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://platform.test",
            headers={"Authorization": f"Bearer {owner_token}"},
        ) as owner,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://platform.test",
            headers={"Authorization": f"Bearer {other_token}"},
        ) as other,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://platform.test",
            headers={"Authorization": f"Bearer {exhausted_token}"},
        ) as exhausted,
    ):
        project = await owner.post(
            "/api/projects",
            json={"name": f"test-e2e-project-{uuid4().hex}", "classes": []},
        )
        assert project.status_code == 201, project.text
        project_id = int(project.json()["id"])
        dataset = await owner.post(
            "/api/datasets",
            json={
                "name": f"test-e2e-dataset-{uuid4().hex}",
                "project_id": project_id,
            },
        )
        assert dataset.status_code == 201, dataset.text
        dataset_id = int(dataset.json()["id"])

        archive, extracted_bytes, file_count = _source_yolo_zip()
        await _upload_zip(
            owner,
            app,
            dataset_id,
            archive,
            extracted_bytes=extracted_bytes,
            file_count=file_count,
        )
        issues = await owner.get(f"/api/datasets/{dataset_id}/issues")
        assert issues.status_code == 200
        assert issues.json()["total"] == 1
        assert issues.json()["items"][0]["kind"] == "label_without_image"

        images = await owner.get(f"/api/datasets/{dataset_id}/images")
        assert images.status_code == 200
        assert images.json()["total"] == 10
        image_id = int(images.json()["items"][0]["id"])
        saved_box = {
            "class_id": 0,
            "cx": 0.25,
            "cy": 0.35,
            "w": 0.4,
            "h": 0.5,
        }
        saved = await owner.put(
            f"/api/images/{image_id}/annotations",
            json={"boxes": [saved_box]},
        )
        assert saved.status_code == 200
        assert saved.json()["is_modified"] is True
        persisted = await owner.get(f"/api/images/{image_id}/annotations")
        assert persisted.status_code == 200
        assert {
            key: persisted.json()["boxes"][0][key]
            for key in saved_box
        } == saved_box

        _prepare_training_host(monkeypatch, tmp_path)
        train = await owner.post(
            f"/api/datasets/{dataset_id}/train",
            json={
                "weights": "yolo26n.pt",
                "epochs": 2,
                "imgsz": 640,
                "batch": -1,
            },
        )
        assert train.status_code == 201, train.text
        run_id = int(train.json()["run_id"])
        assert isinstance(train.json()["warnings"], list)
        async with app.state.session_factory() as session:
            run = await session.get(TrainingRun, run_id)
            assert run is not None and run.owner_id == owner_id
            out_dir = contained_storage_path(
                app.state.settings.storage_dir,
                run.out_dir,
            )
        best_bytes = _finish_training_through_worker_boundary(
            app,
            owner_id=owner_id,
            run_id=run_id,
            out_dir=out_dir,
        )

        detail = await owner.get(f"/api/runs/{run_id}")
        metrics = await owner.get(f"/api/runs/{run_id}/metrics")
        log = await owner.get(f"/api/runs/{run_id}/log?tail=20")
        best = await owner.get(f"/api/runs/{run_id}/artifacts/best.pt")
        assert detail.status_code == 200
        assert detail.json()["state"] == "done"
        assert detail.json()["epoch"] == 1
        assert metrics.status_code == 200
        assert metrics.json()[0]["map50"] == pytest.approx(0.71)
        assert log.status_code == 200
        assert "epoch 1/2 complete" in log.text
        assert best.status_code == 200 and best.content == best_bytes

        for path in (
            f"/api/datasets/{dataset_id}",
            f"/api/runs/{run_id}",
            f"/api/runs/{run_id}/artifacts/best.pt",
        ):
            hidden = await other.get(path)
            assert hidden.status_code == 404, (path, hidden.text)
        assert other_id != owner_id

        exhausted_project = await exhausted.post(
            "/api/projects",
            json={
                "name": f"test-e2e-quota-project-{uuid4().hex}",
                "classes": [],
            },
        )
        exhausted_dataset = await exhausted.post(
            "/api/datasets",
            json={
                "name": f"test-e2e-quota-dataset-{uuid4().hex}",
                "project_id": exhausted_project.json()["id"],
            },
        )
        assert exhausted_dataset.status_code == 201
        exhausted_dataset_id = int(exhausted_dataset.json()["id"])
        quota_limit = 1_000
        app.state.settings = app.state.settings.model_copy(
            update={"quota_bytes_per_user": quota_limit}
        )
        async with app.state.session_factory() as session:
            quota_dataset = await session.get(Dataset, exhausted_dataset_id)
            assert quota_dataset is not None
            quota_dataset.status = "ready"
            quota_usage = await session.get(UserStorage, exhausted_id)
            if quota_usage is None:
                session.add(
                    UserStorage(owner_id=exhausted_id, bytes_used=quota_limit)
                )
            else:
                quota_usage.bytes_used = quota_limit
            await session.commit()

        upload_rejected = await exhausted.post(
            f"/api/datasets/{exhausted_dataset_id}/upload-batches/preflight",
            json={
                "total_size": 10,
                "largest_file_size": 10,
                "file_count": 1,
                "expected_extracted_size": 10,
            },
        )
        train_rejected = await exhausted.post(
            f"/api/datasets/{exhausted_dataset_id}/train",
            json={
                "weights": "yolo26n.pt",
                "epochs": 1,
                "imgsz": 640,
                "batch": -1,
            },
        )
        for rejected in (upload_rejected, train_rejected):
            _assert_quota_rejected(rejected)


async def test_auth_service_actual_routes_in_isolated_process() -> None:
    """Exercise the read-only auth module without importing its ``app`` package.

    Importing both services in this pytest process would alias two unrelated
    top-level ``app`` packages.  A child interpreter gives the auth module its own
    import graph and explicitly pins it to ``auth_test`` before pytest starts.
    """

    auth_python = AUTH_SERVICE_ROOT / ".venv" / "bin" / "python"
    assert auth_python.is_file(), f"auth test interpreter missing: {auth_python}"
    env_values = dotenv_values(AUTH_SERVICE_ROOT / ".env")
    auth_test_database_url = env_values.get("TEST_DATABASE_URL")
    assert auth_test_database_url, "auth-service TEST_DATABASE_URL is required"
    test_url = make_url(str(auth_test_database_url))
    assert (
        test_url.drivername == "postgresql+asyncpg"
        and test_url.host in {"localhost", "127.0.0.1"}
        and test_url.port == 5435
        and test_url.database == "auth_test"
    ), (
        "auth subprocess may only use the dedicated auth_test database"
    )

    child_env = os.environ.copy()
    child_env.update(
        {
            key: str(value)
            for key, value in env_values.items()
            if value is not None
        }
    )
    child_env.update(
        {
            "DATABASE_URL": str(auth_test_database_url),
            "OAUTH_REDIRECT_BASE": "http://localhost:5176",
            "ALLOWED_REDIRECT_URIS": "http://localhost:5176/auth/callback",
            "CORS_ORIGINS": "http://localhost:5176",
            "COOKIE_SECURE": "false",
            "GOOGLE_CLIENT_ID": "test-google-id",
            "GOOGLE_CLIENT_SECRET": "test-google-secret",
            "GOOGLE_SCOPE": "openid email profile",
            "KAKAO_CLIENT_ID": "test-kakao-id",
            "KAKAO_CLIENT_SECRET": "test-kakao-secret",
            "KAKAO_SCOPE": "",
            "NAVER_CLIENT_ID": "test-naver-id",
            "NAVER_CLIENT_SECRET": "test-naver-secret",
            "NAVER_SCOPE": "name email",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(AUTH_SERVICE_ROOT),
            "PYTEST_ADDOPTS": "",
            "TEST_DATABASE_URL": str(auth_test_database_url),
        }
    )
    common_options = {
        "cwd": AUTH_SERVICE_ROOT,
        "env": child_env,
        "capture_output": True,
        "text": True,
        "timeout": 180,
        "check": False,
    }

    migrated = await asyncio.to_thread(
        subprocess.run,
        [str(auth_python), "-m", "alembic", "upgrade", "head"],
        **common_options,
    )
    assert migrated.returncode == 0, (
        "auth_test additive migration failed\n"
        f"stdout:\n{migrated.stdout}\n"
        f"stderr:\n{migrated.stderr}"
    )

    local_round_trip = await asyncio.to_thread(
        subprocess.run,
        [str(auth_python), "-c", AUTH_LOCAL_ROUND_TRIP],
        **common_options,
    )
    assert local_round_trip.returncode == 0, (
        "actual signup -> same-account login route failed\n"
        f"stdout:\n{local_round_trip.stdout}\n"
        f"stderr:\n{local_round_trip.stderr}"
    )
    assert "AUTH_LOCAL_ROUND_TRIP_OK" in local_round_trip.stdout

    selected_contracts = (
        "tests/test_oauth_e2e.py::test_oauth_full_round_trip_real_provider_shape",
        "tests/test_oauth_e2e.py::test_oauth_state_is_single_use_across_round_trip",
    )
    completed = await asyncio.to_thread(
        subprocess.run,
        [
            str(auth_python),
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            *selected_contracts,
        ],
        **common_options,
    )
    assert completed.returncode == 0, (
        "isolated auth-service route contracts failed\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
