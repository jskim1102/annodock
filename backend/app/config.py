"""Validated runtime configuration loaded from the project environment."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import AnyHttpUrl, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


PROJECT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _comma_separated(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    raise ValueError("expected a comma-separated string")


class Settings(BaseSettings):
    """Fail-closed settings shared by HTTP and background worker code."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = Field(min_length=1)
    auth_base_url: AnyHttpUrl
    storage_dir: Path
    cors_origins: Annotated[tuple[str, ...], NoDecode]
    max_zip_bytes: int = Field(gt=0)
    max_extracted_bytes: int = Field(gt=0)
    max_file_count: int = Field(gt=0)
    max_compression_ratio: float = Field(gt=0)
    disk_headroom_factor: float = Field(gt=0)
    allowed_image_exts: Annotated[tuple[str, ...], NoDecode]
    quota_bytes_per_user: int = Field(default=100 * 1024**3, gt=0)
    run_artifact_keep_count: int = Field(default=10, ge=0)
    run_artifact_keep_days: int = Field(default=30, ge=0)

    @field_validator("storage_dir", mode="after")
    @classmethod
    def resolve_storage_dir(cls, value: Path) -> Path:
        """Make STORAGE_DIR independent from the caller's working directory."""
        expanded = value.expanduser()
        if not expanded.is_absolute():
            expanded = BACKEND_ROOT / expanded
        return expanded.resolve()

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: Any) -> tuple[str, ...]:
        origins = _comma_separated(value)
        if not origins:
            raise ValueError("CORS_ORIGINS must contain at least one origin")
        return origins

    @field_validator("allowed_image_exts", mode="before")
    @classmethod
    def parse_allowed_image_exts(cls, value: Any) -> tuple[str, ...]:
        extensions = tuple(
            extension.removeprefix(".").lower()
            for extension in _comma_separated(value)
        )
        if not extensions:
            raise ValueError("ALLOWED_IMAGE_EXTS must not be empty")
        if len(extensions) != len(set(extensions)):
            raise ValueError("ALLOWED_IMAGE_EXTS must not contain duplicates")
        return extensions


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
