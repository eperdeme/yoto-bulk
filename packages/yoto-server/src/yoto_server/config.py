"""Configuration for the Yoto server."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings


def _default_data_dir() -> Path:
    return Path.home() / ".config" / "yoto-bulk"


class Settings(BaseSettings):
    """Server configuration loaded from environment / .env file.

    Reads YOTO_BULK_* env vars so the service can share credentials and
    paths with the web UI stack.
    """

    model_config = {
        "env_prefix": "YOTO_BULK_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    # Server
    yoto_server_host: str = "0.0.0.0"
    yoto_server_port: int = 8001

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Yoto OAuth
    yoto_client_id: str | None = None
    yoto_client_secret: str | None = None
    yoto_auth_base_url: str = "https://login.yotoplay.com"
    yoto_api_base_url: str = "https://api.yotoplay.com"
    yoto_api_audience: str = "https://api.yotoplay.com"

    # Paths
    data_dir: Path = _default_data_dir()
    request_timeout_seconds: float = 30.0
    yoto_transcode_timeout_seconds: float = 600.0
    yoto_transcode_poll_interval_seconds: float = 2.0
    log_level: str = "INFO"
    abs_url: str = "https://audiobookshelf.eper.net"
    abs_token: str = ""
    abs_cf_access_client_id: str = ""
    abs_cf_access_client_secret: str = ""
    abs_library_name: str | None = None

    # OpenAI (for series discovery)
    openai_api_key: str | None = None
    openai_model: str = "gpt-5.4"

    # BorrowBox (unused by us but present in shared .env — ignored via extra=ignore)

    @property
    def token_path(self) -> Path:
        return self.data_dir / "tokens.json"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
