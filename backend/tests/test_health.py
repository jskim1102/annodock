from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.config import BACKEND_ROOT, Settings
from app.main import create_app


REQUIRED_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://user:pass@db:5432/dataset",
    "AUTH_BASE_URL": "http://auth.test:8000",
    "STORAGE_DIR": "./test-storage",
    "CORS_ORIGINS": "http://localhost:5183,https://viewer.example",
    "MAX_ZIP_BYTES": "21474836480",
    "MAX_EXTRACTED_BYTES": "107374182400",
    "MAX_FILE_COUNT": "200000",
    "MAX_COMPRESSION_RATIO": "100",
    "DISK_HEADROOM_FACTOR": "1.2",
    "ALLOWED_IMAGE_EXTS": (
        "avif,bmp,dng,heic,heif,jp2,jpeg,jpeg2000,jpg,mpo,png,tif,tiff,webp"
    ),
}


def build_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_settings_reads_required_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build_settings(monkeypatch)

    assert settings.database_url == REQUIRED_ENV["DATABASE_URL"]
    assert str(settings.auth_base_url) == REQUIRED_ENV["AUTH_BASE_URL"] + "/"
    assert settings.storage_dir == (BACKEND_ROOT / "test-storage").resolve()
    assert settings.cors_origins == (
        "http://localhost:5183",
        "https://viewer.example",
    )
    assert settings.max_zip_bytes == 20 * 1024**3
    assert settings.max_extracted_bytes == 100 * 1024**3
    assert settings.max_file_count == 200_000
    assert settings.max_compression_ratio == 100
    assert settings.disk_headroom_factor == 1.2
    assert len(settings.allowed_image_exts) == 14


def test_settings_fail_closed_when_any_required_value_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in REQUIRED_ENV.items():
        if key != "DATABASE_URL":
            monkeypatch.setenv(key, value)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.asyncio
async def test_health_and_cors_use_the_configured_whitelist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(build_settings(monkeypatch))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        health = await client.get(
            "/api/health",
            headers={"Origin": "https://viewer.example"},
        )
        rejected_origin = await client.get(
            "/api/health",
            headers={"Origin": "https://not-allowed.example"},
        )

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert (
        health.headers["access-control-allow-origin"]
        == "https://viewer.example"
    )
    assert "access-control-allow-origin" not in rejected_origin.headers
