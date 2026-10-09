"""Application settings and filesystem paths."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> Path:
    return Path.home() / ".config" / "yoto-bulk"


class Settings(BaseSettings):
    """Shared runtime settings for Yoto service helpers."""

    model_config = SettingsConfigDict(
        env_prefix="YOTO_BULK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    yoto_client_id: str | None = Field(default=None)
    yoto_client_secret: str | None = Field(default=None)
    yoto_auth_base_url: str = Field(default="https://login.yotoplay.com")
    yoto_api_base_url: str = Field(default="https://api.yotoplay.com")
    yoto_api_audience: str = Field(default="https://api.yotoplay.com")
    openai_api_key: str | None = Field(default=None)
    openai_model: str = Field(default="gpt-5.4")
    audiobook_dir: Path | None = Field(default=None)
    data_dir: Path = Field(default_factory=default_data_dir)
    request_timeout_seconds: float = Field(default=30.0)
    yoto_transcode_timeout_seconds: float = Field(default=600.0)
    yoto_transcode_poll_interval_seconds: float = Field(default=2.0)
    max_family_groups: int = Field(default=20)
    log_level: str = Field(default="INFO")

    @property
    def token_path(self) -> Path:
        return self.data_dir / "tokens.json"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def library_dir(self) -> Path:
        return self.data_dir / "library"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
