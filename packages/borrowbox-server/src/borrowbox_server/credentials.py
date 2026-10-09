"""Encrypted-at-rest storage for BorrowBox library credentials.

Why this exists
---------------
Credentials used to live only in ``BORROWBOX_CREDENTIALS`` as env JSON, so
rotating one library's password meant editing ``.env`` and restarting both
containers. A library whose password had expired stayed broken until someone
noticed it in the logs.

Moving them into Redis makes them manageable from the UI, but Redis snapshots to
``dump.rdb`` on disk, so they must never be stored in the clear. Everything is
sealed with Fernet (AES-128-CBC + HMAC-SHA256) using a key derived from
``BORROWBOX_SECRET_KEY``.

Precedence is store-first, env-fallback: the env var acts as a seed for a fresh
deployment, and anything saved through the API takes over from then on.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

# Fixed salt. The secret is supplied by the operator and never rotates per
# record, so a stored random salt would buy nothing while adding a bootstrap
# problem (the salt would have to be readable before anything can be decrypted).
_KDF_SALT = b"borrowbox-credential-store-v1"
_KDF_ITERATIONS = 600_000


class CredentialError(RuntimeError):
    """Raised when credentials cannot be sealed or opened."""


def _looks_like_fernet_key(value: str) -> bool:
    """True when *value* is already a valid 32-byte urlsafe-base64 Fernet key."""
    try:
        return len(base64.urlsafe_b64decode(value.encode())) == 32
    except (binascii.Error, ValueError):
        return False


@lru_cache(maxsize=8)
def derive_key(secret: str) -> bytes:
    """Turn an operator-supplied secret into a Fernet key.

    A key produced by ``Fernet.generate_key()`` is used directly. Anything else
    is treated as a passphrase and stretched with PBKDF2, so a short secret does
    not become the encryption key verbatim.

    Cached because PBKDF2 runs 600k iterations: request-scoped vaults are built
    on every outbound library call, and re-deriving each time would add hundreds
    of milliseconds per request.
    """
    if not secret:
        raise CredentialError("BORROWBOX_SECRET_KEY is not set")
    if _looks_like_fernet_key(secret):
        return secret.encode()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_KDF_SALT,
        iterations=_KDF_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(secret.encode()))


def generate_secret_key() -> str:
    """A fresh key suitable for ``BORROWBOX_SECRET_KEY``."""
    return Fernet.generate_key().decode()


@dataclass(frozen=True)
class CredentialStatus:
    """What the UI is allowed to know about a library's credentials.

    Deliberately contains no password. ``source`` distinguishes credentials
    managed through the API from ones still coming from the env seed.
    """

    library: str
    has_credentials: bool = False
    source: str | None = None  # "store" | "env" | None
    username: str | None = None  # shown so the operator can confirm the account
    verified: bool | None = None
    verified_at: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "library": self.library,
            "has_credentials": self.has_credentials,
            "source": self.source,
            "username": self.username,
            "verified": self.verified,
            "verified_at": self.verified_at,
            "error": self.error,
        }


class CredentialVault:
    """Seals credentials into Redis and reports their health.

    Constructed once at startup and reachable via ``app.state.vault``. When no
    secret key is configured the vault runs in read-only mode: existing env
    credentials keep working, but nothing can be written, so credentials are
    never persisted in the clear.
    """

    def __init__(
        self,
        store,
        secret_key: str = "",
        env_credentials: dict | None = None,
        *,
        quiet: bool = False,
    ) -> None:
        self._store = store
        self._env = env_credentials or {}
        self._fernet: Fernet | None = None
        if secret_key:
            try:
                self._fernet = Fernet(derive_key(secret_key))
            except CredentialError:
                raise
            except Exception as exc:  # noqa: BLE001 - surfaced as a clear startup error
                raise CredentialError(f"Invalid BORROWBOX_SECRET_KEY: {exc}") from exc
            if not quiet:
                logger.info(
                    "Credential vault enabled (%s)",
                    "supplied Fernet key"
                    if _looks_like_fernet_key(secret_key)
                    else "PBKDF2-derived key",
                )
        elif not quiet:
            logger.warning(
                "BORROWBOX_SECRET_KEY is not set — credentials cannot be saved from the UI. "
                "Falling back to BORROWBOX_CREDENTIALS from the environment."
            )

    # ── capability ───────────────────────────────────────────────────────
    @property
    def writable(self) -> bool:
        """Whether credentials can be persisted (i.e. a secret key exists)."""
        return self._fernet is not None

    def _require_writable(self) -> Fernet:
        if self._fernet is None:
            raise CredentialError(
                "Cannot store credentials: BORROWBOX_SECRET_KEY is not set. "
                "Generate one with `python -c \"from cryptography.fernet import Fernet; "
                'print(Fernet.generate_key().decode())"` and set it in the environment.'
            )
        return self._fernet

    # ── read ─────────────────────────────────────────────────────────────
    async def get(self, library: str) -> tuple[str, str] | None:
        """Return ``(username, password)`` for *library*, or None.

        The encrypted store wins over the env seed.
        """
        if self._fernet is not None and self._store is not None:
            sealed = await self._store.get_credentials_blob(library)
            if sealed:
                try:
                    data = json.loads(self._fernet.decrypt(sealed.encode()))
                    return data["username"], data["password"]
                except InvalidToken:
                    # Almost always a changed secret key. Say so plainly rather
                    # than silently falling back to a stale env password.
                    logger.error(
                        "Stored credentials for %s could not be decrypted — "
                        "BORROWBOX_SECRET_KEY has probably changed since they were saved",
                        library,
                    )
                    raise CredentialError(
                        f"Stored credentials for '{library}' cannot be decrypted. "
                        "The secret key has changed; re-enter them."
                    ) from None
                except (KeyError, json.JSONDecodeError) as exc:
                    raise CredentialError(f"Corrupt credential record for '{library}'") from exc

        env = self._env.get(library)
        if env and env.get("username"):
            return env["username"], env["password"]
        return None

    async def status(self, library: str) -> CredentialStatus:
        """Health of one library's credentials, without exposing the password."""
        stored_username = None
        if self._fernet is not None and self._store is not None:
            sealed = await self._store.get_credentials_blob(library)
            if sealed:
                try:
                    stored_username = json.loads(self._fernet.decrypt(sealed.encode()))["username"]
                except Exception:  # noqa: BLE001
                    # Report as present-but-broken rather than failing the listing.
                    health = (await self._store.get_credential_status(library)) or {}
                    return CredentialStatus(
                        library=library,
                        has_credentials=True,
                        source="store",
                        verified=False,
                        verified_at=health.get("verified_at"),
                        error="cannot decrypt — secret key changed",
                    )

        health = (await self._store.get_credential_status(library) or {}) if self._store else {}
        if stored_username is not None:
            return CredentialStatus(
                library=library,
                has_credentials=True,
                source="store",
                username=stored_username,
                verified=health.get("verified"),
                verified_at=health.get("verified_at"),
                error=health.get("error"),
            )

        env = self._env.get(library)
        if env and env.get("username"):
            return CredentialStatus(
                library=library,
                has_credentials=True,
                source="env",
                username=env["username"],
                verified=health.get("verified"),
                verified_at=health.get("verified_at"),
                error=health.get("error"),
            )
        return CredentialStatus(library=library, has_credentials=False)

    # ── write ────────────────────────────────────────────────────────────
    async def put(self, library: str, username: str, password: str) -> None:
        """Seal and persist credentials for *library*."""
        fernet = self._require_writable()
        if not username or not password:
            raise CredentialError("Both username and password are required")
        blob = fernet.encrypt(json.dumps({"username": username, "password": password}).encode())
        await self._store.set_credentials_blob(library, blob.decode())

    async def delete(self, library: str) -> bool:
        """Remove stored credentials. The env seed, if any, becomes active again."""
        removed = await self._store.delete_credentials_blob(library)
        await self._store.delete_credential_status(library)
        return removed

    async def record_verification(
        self, library: str, *, ok: bool, error: str | None = None
    ) -> None:
        """Note the outcome of a login attempt so the UI can show health.

        Without this, a rejected credential is only visible in container logs.
        """
        if self._store is None:
            return
        await self._store.set_credential_status(
            library,
            {
                "verified": ok,
                "verified_at": time.time(),
                "error": None if ok else (error or "sign-in failed"),
            },
        )


def vault_for(store, settings) -> CredentialVault:
    """Build a request-scoped vault.

    Used by call paths that hold a ``store`` but not the FastAPI ``Request``
    (and therefore cannot reach ``app.state.vault``). Cheap because the
    expensive key derivation is cached.
    """
    return CredentialVault(
        store,
        secret_key=getattr(settings, "secret_key", "") or "",
        env_credentials=getattr(settings, "credentials", None),
        quiet=True,
    )
