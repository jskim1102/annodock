"""Fast unit coverage for the real-boundary training smoke helpers."""

import json
from pathlib import Path

import httpx
import pytest

from scripts import smoke_train


def test_literal_env_keeps_values_literal(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# ignored\nBACKEND_PORT=8015\nSCOPE=openid email profile\n",
        encoding="utf-8",
    )

    assert smoke_train._literal_env(env_file) == {
        "BACKEND_PORT": "8015",
        "SCOPE": "openid email profile",
    }


def test_api_base_url_requires_explicit_valid_port() -> None:
    with pytest.raises(AssertionError, match="BACKEND_PORT is required"):
        smoke_train._api_base_url({})
    with pytest.raises(AssertionError, match="integer"):
        smoke_train._api_base_url({"BACKEND_PORT": "not-a-port"})


def test_auth_base_url_requires_explicit_valid_port() -> None:
    with pytest.raises(AssertionError, match="AUTH_PORT is required"):
        smoke_train._auth_base_url({})
    assert smoke_train._auth_base_url({"AUTH_PORT": "9015"}) == (
        "http://127.0.0.1:9015"
    )


def _auth_handler(
    observed: list[tuple[str, str]], *, account_exists: bool
) -> callable:
    state = {"registered": account_exists}

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append((request.method, request.url.path))
        if request.url.path == "/auth/signup":
            payload = json.loads(request.content)
            assert payload["email"] == smoke_train.SMOKE_EMAIL
            assert payload["username"] == smoke_train.SMOKE_USERNAME
            assert len(payload["password"]) >= 8
            state["registered"] = True
            return httpx.Response(
                201,
                json={"id": 77, "email": payload["email"], "username": payload["username"]},
            )
        if request.url.path == "/auth/login":
            payload = json.loads(request.content)
            assert payload["identifier"] == smoke_train.SMOKE_EMAIL
            if not state["registered"]:
                return httpx.Response(401, json={"detail": "no such account"})
            return httpx.Response(
                200,
                json={"access_token": "access-token", "refresh_token": "refresh-token"},
            )
        if request.url.path == "/auth/me":
            assert request.headers["authorization"] == "Bearer access-token"
            return httpx.Response(
                200,
                json={
                    "id": 77,
                    "email": smoke_train.SMOKE_EMAIL,
                    "username": smoke_train.SMOKE_USERNAME,
                },
            )
        if request.url.path == "/auth/logout":
            assert json.loads(request.content) == {"refresh_token": "refresh-token"}
            return httpx.Response(204)
        raise AssertionError(f"unexpected auth request: {request.url.path}")

    return handler


def test_smoke_auth_reuses_the_dedicated_account() -> None:
    # Normal run: the dedicated account already exists — login only, no signup.
    observed: list[tuple[str, str]] = []
    transport = httpx.MockTransport(_auth_handler(observed, account_exists=True))
    auth = smoke_train._create_smoke_auth("http://auth.test", transport=transport)
    assert auth.access_token == "access-token"
    assert auth.user_id == 77
    smoke_train._logout_smoke_auth("http://auth.test", auth, transport=transport)
    assert observed == [
        ("POST", "/auth/login"),
        ("GET", "/auth/me"),
        ("POST", "/auth/logout"),
    ]


def test_smoke_auth_registers_once_on_a_fresh_environment() -> None:
    # First run ever: login fails, the dedicated account is registered once,
    # then login proceeds normally.
    observed: list[tuple[str, str]] = []
    transport = httpx.MockTransport(_auth_handler(observed, account_exists=False))
    auth = smoke_train._create_smoke_auth("http://auth.test", transport=transport)
    assert auth.user_id == 77
    assert observed == [
        ("POST", "/auth/login"),
        ("POST", "/auth/signup"),
        ("POST", "/auth/login"),
        ("GET", "/auth/me"),
    ]


def test_training_imgsz_defaults_and_rejects_invalid_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPLABEL_SMOKE_IMGSZ", raising=False)
    assert smoke_train._training_imgsz() == 640
    monkeypatch.setenv("DEEPLABEL_SMOKE_IMGSZ", "not-a-size")
    with pytest.raises(AssertionError, match="must be an integer"):
        smoke_train._training_imgsz()


def test_training_epochs_defaults_and_rejects_out_of_range_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPLABEL_SMOKE_EPOCHS", raising=False)
    assert smoke_train._training_epochs() == 2
    monkeypatch.setenv("DEEPLABEL_SMOKE_EPOCHS", "1")
    with pytest.raises(AssertionError, match="between 2 and 100"):
        smoke_train._training_epochs()


def test_host_database_url_rewrites_only_shared_container_endpoint() -> None:
    container_url = (
        "postgresql+asyncpg://postgres:secret@"
        "harness-shared-postgres:5432/deeplabel"
    )

    assert smoke_train._host_database_url({"DATABASE_URL": container_url}) == (
        "postgresql+asyncpg://postgres:secret@localhost:5435/deeplabel"
    )


def test_storage_dir_resolves_relative_to_backend_root() -> None:
    assert smoke_train._storage_dir({"STORAGE_DIR": "./storage"}) == (
        smoke_train.BACKEND_ROOT / "storage"
    ).resolve()


def test_expect_rejects_the_wrong_success_status() -> None:
    response = httpx.Response(
        202,
        request=httpx.Request("POST", "http://example.test/train"),
    )

    with pytest.raises(AssertionError, match="expected HTTP 201, received 202"):
        smoke_train._expect(response, 201)


def test_expect_preserves_structured_korean_error_detail() -> None:
    response = httpx.Response(
        507,
        json={
            "detail": {
                "message": "디스크 여유가 부족합니다.",
                "required_bytes": 10,
                "available_bytes": 5,
            }
        },
        request=httpx.Request("POST", "http://example.test/preflight"),
    )

    with pytest.raises(smoke_train.ApiFailure) as caught:
        smoke_train._expect(response, 204)

    assert caught.value.status_code == 507
    assert "디스크 여유가 부족합니다." in caught.value.detail
    assert "required_bytes" in caught.value.detail
