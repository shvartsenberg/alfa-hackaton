"""Application settings loaded from environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime configuration for the service."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"

    restoration_ttl_seconds: int = 3600
    restoration_max_entries: int = 100_000

    consumers_dir: Path = PROJECT_ROOT / "configs" / "consumers"
    default_consumer_id: str = "default"

    host: str = "0.0.0.0"
    port: int = 8000

    rate_limit_max_requests: int = 1000
    rate_limit_window_seconds: float = 1.0


def get_settings() -> Settings:
    """Return a cached settings instance."""
    return Settings()