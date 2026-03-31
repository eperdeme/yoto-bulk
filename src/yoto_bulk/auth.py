"""OAuth helpers for headless CLI authentication."""

from __future__ import annotations

import json
import os
import secrets
import webbrowser
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import urlencode

import httpx

from yoto_bulk.config import Settings
from yoto_bulk.models import OAuthTokenSet


def _write_private_file(path: os.PathLike[str], content: str) -> None:
    """Write *content* to *path* and restrict permissions to owner-only."""
    from pathlib import Path as _Path

    p = _Path(path)
    p.write_text(content)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


@dataclass(slots=True)
class OAuthBootstrap:
    authorize_url: str
    state: str


class TokenStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def load(self) -> OAuthTokenSet | None:
        if not self.settings.token_path.exists():
            return None
        data = json.loads(self.settings.token_path.read_text())
        return OAuthTokenSet.model_validate(data)

    def save(self, tokens: OAuthTokenSet) -> None:
        self.settings.ensure_directories()
        _write_private_file(self.settings.token_path, tokens.model_dump_json(indent=2))


class YotoAuthService:
    """Manage OAuth bootstrap and token exchange."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.token_store = TokenStore(settings)

    @property
    def pending_state_path(self) -> str:
        return str(self.settings.cache_dir / "oauth_state")

    @property
    def pending_pkce_verifier_path(self) -> str:
        return str(self.settings.cache_dir / "oauth_pkce_verifier")

    def save_pending_state(self, state: str) -> None:
        self.settings.ensure_directories()
        _write_private_file(self.settings.cache_dir / "oauth_state", state)

    def save_pending_pkce_verifier(self, verifier: str) -> None:
        self.settings.ensure_directories()
        _write_private_file(self.settings.cache_dir / "oauth_pkce_verifier", verifier)

    def load_pending_state(self) -> str | None:
        pending_state_path = self.settings.cache_dir / "oauth_state"
        if not pending_state_path.exists():
            return None
        return pending_state_path.read_text().strip() or None

    def load_pending_pkce_verifier(self) -> str | None:
        pending_pkce_verifier_path = self.settings.cache_dir / "oauth_pkce_verifier"
        if not pending_pkce_verifier_path.exists():
            return None
        return pending_pkce_verifier_path.read_text().strip() or None

    def clear_pending_state(self) -> None:
        pending_state_path = self.settings.cache_dir / "oauth_state"
        pending_state_path.unlink(missing_ok=True)

    def clear_pending_pkce_verifier(self) -> None:
        pending_pkce_verifier_path = self.settings.cache_dir / "oauth_pkce_verifier"
        pending_pkce_verifier_path.unlink(missing_ok=True)

    def validate_state(self, state: str) -> bool:
        expected_state = self.load_pending_state()
        if expected_state is None:
            return False
        return secrets.compare_digest(state, expected_state)

    def build_pkce_pair(self) -> tuple[str, str]:
        verifier = secrets.token_urlsafe(64)
        challenge = urlsafe_b64encode(sha256(verifier.encode("utf-8")).digest()).rstrip(b"=").decode("ascii")
        return verifier, challenge

    def build_authorize_url(self, redirect_uri: str, scope: str = "profile offline_access") -> OAuthBootstrap:
        if not self.settings.yoto_client_id:
            raise ValueError("Missing YOTO_BULK_YOTO_CLIENT_ID")

        state = secrets.token_urlsafe(24)
        verifier, challenge = self.build_pkce_pair()
        self.save_pending_state(state)
        self.save_pending_pkce_verifier(verifier)
        query = urlencode(
            {
                "client_id": self.settings.yoto_client_id,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": scope,
                "audience": self.settings.yoto_api_audience,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return OAuthBootstrap(
            authorize_url=f"{self.settings.yoto_auth_base_url}/authorize?{query}",
            state=state,
        )

    def open_browser_login(self, redirect_uri: str, scope: str = "profile offline_access") -> OAuthBootstrap:
        bootstrap = self.build_authorize_url(redirect_uri=redirect_uri, scope=scope)
        webbrowser.open(bootstrap.authorize_url)
        return bootstrap

    async def exchange_code(self, code: str, redirect_uri: str) -> OAuthTokenSet:
        if not self.settings.yoto_client_id:
            raise ValueError("Missing YOTO_BULK_YOTO_CLIENT_ID")
        code_verifier = self.load_pending_pkce_verifier()
        if not code_verifier:
            raise ValueError("Missing PKCE verifier. Run auth login again before exchanging the code.")

        data = {
            "grant_type": "authorization_code",
            "client_id": self.settings.yoto_client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "audience": self.settings.yoto_api_audience,
        }
        if self.settings.yoto_client_secret:
            data["client_secret"] = self.settings.yoto_client_secret

        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            response = await client.post(
                f"{self.settings.yoto_auth_base_url}/oauth/token",
                data=data,
            )
            response.raise_for_status()

        tokens = OAuthTokenSet.model_validate(response.json())
        self.token_store.save(tokens)
        self.clear_pending_state()
        self.clear_pending_pkce_verifier()
        return tokens

    async def refresh_access_token(self, refresh_token: str | None = None) -> OAuthTokenSet:
        current = self.token_store.load()
        token = refresh_token or (current.refresh_token if current else None)
        if not token:
            raise ValueError("No refresh token available")
        if not self.settings.yoto_client_id:
            raise ValueError("Missing YOTO_BULK_YOTO_CLIENT_ID")

        data = {
            "grant_type": "refresh_token",
            "client_id": self.settings.yoto_client_id,
            "refresh_token": token,
            "audience": self.settings.yoto_api_audience,
        }
        if self.settings.yoto_client_secret:
            data["client_secret"] = self.settings.yoto_client_secret

        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            response = await client.post(
                f"{self.settings.yoto_auth_base_url}/oauth/token",
                data=data,
            )
            response.raise_for_status()

        payload = response.json()
        if "refresh_token" not in payload and current and current.refresh_token:
            payload["refresh_token"] = current.refresh_token

        tokens = OAuthTokenSet.model_validate(payload)
        self.token_store.save(tokens)
        return tokens
