"""Configuration for the BorrowBox server."""

from __future__ import annotations

import os

from pydantic import Field
from pydantic_settings import BaseSettings


def _shared_abs_url() -> str:
    return os.environ.get("YOTO_BULK_ABS_URL", "https://audiobookshelf.eper.net")


def _shared_abs_token() -> str:
    return os.environ.get("YOTO_BULK_ABS_TOKEN", "")


def _shared_abs_cf_access_client_id() -> str:
    return os.environ.get("YOTO_BULK_ABS_CF_ACCESS_CLIENT_ID", "")


def _shared_abs_cf_access_client_secret() -> str:
    return os.environ.get("YOTO_BULK_ABS_CF_ACCESS_CLIENT_SECRET", "")


def _shared_abs_library_name() -> str | None:
    return os.environ.get("YOTO_BULK_ABS_LIBRARY_NAME") or None


class Settings(BaseSettings):
    """Server configuration loaded from environment / .env file."""

    model_config = {"env_prefix": "BORROWBOX_", "env_file": ".env", "extra": "ignore"}

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # BorrowBox libraries: JSON dict like {"hounslow": "hounslow"}
    libraries: dict[str, str] = {}

    # Credentials per library: {"hounslow": {"username": "...", "password": "..."}}
    credentials: dict[str, dict[str, str]] = {}

    # Background refresh interval in seconds (0 = disabled)
    refresh_interval: int = 3600

    # Downstream service URLs
    yoto_api_url: str = "http://localhost:8001"
    abs_url: str = Field(default_factory=_shared_abs_url)
    abs_token: str = Field(default_factory=_shared_abs_token)
    abs_cf_access_client_id: str = Field(default_factory=_shared_abs_cf_access_client_id)
    abs_cf_access_client_secret: str = Field(default_factory=_shared_abs_cf_access_client_secret)
    abs_library_name: str | None = Field(default_factory=_shared_abs_library_name)

    # ABS metadata auto-match on import. Uploaded audio often only carries a
    # generic ID3 blurb, so after upload we ask ABS to match against an online
    # provider and adopt the richer metadata when confidence is high enough.
    abs_auto_match: bool = True
    abs_match_min_confidence: float = 0.7
    abs_match_providers: list[str] = ["audible.uk", "audible"]

    # API key for authentication (empty = no auth required)
    api_key: str = ""

    # Encrypts library credentials at rest in Redis. Without it, credentials
    # cannot be saved from the UI and only BORROWBOX_CREDENTIALS is used.
    # Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # A passphrase is also accepted and stretched with PBKDF2.
    secret_key: str = ""

    # ZIP archive directory — downloaded BorrowBox ZIPs are kept here
    # permanently so retries don't re-download and as a long-term backup.
    zip_archive_dir: str = "/zip-archive"

    # Cache TTLs (seconds)
    ttl_availability: int = 3600       # 1 hour
    ttl_credits: int = 300             # 5 minutes
    ttl_discovery: int = 604800        # 7 days
    ttl_product: int = 2592000         # 30 days
    ttl_session: int = 86400           # 24 hours
