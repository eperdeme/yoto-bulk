"""Credential management endpoints.

Kept out of ``routes_auth.py`` deliberately: that module is already over a
thousand lines, and credential management is a distinct, stable concern.

Security rules enforced here:

* a password is never returned, in any response, ever;
* a credential is verified against the library before it is persisted, so a
  typo cannot be saved;
* the verification outcome is recorded so the UI can show which library needs
  attention without re-attempting a login.
"""

from __future__ import annotations

import asyncio
import logging

from borrowbox import BorrowBoxClient
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from borrowbox_server.credentials import CredentialError, CredentialVault
from borrowbox_server.store import Store

router = APIRouter()
logger = logging.getLogger(__name__)


def _get_vault(request: Request) -> CredentialVault:
    return request.app.state.vault


def _get_store(request: Request) -> Store:
    return request.app.state.store


def _get_settings(request: Request):
    return request.app.state.settings


def _validate_library(library: str, settings) -> str:
    if library not in settings.libraries:
        raise HTTPException(status_code=404, detail=f"Unknown library '{library}'")
    return settings.libraries[library]


class CredentialInput(BaseModel):
    """Incoming credentials. Write-only: never echoed back."""

    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    # Skipping verification allows saving credentials while a library is down.
    verify: bool = True


def _login_blocking(subdomain: str, username: str, password: str) -> dict:
    """Log in and return cacheable session state. Runs in a worker thread."""
    client = BorrowBoxClient(subdomain)
    try:
        client.login(username, password)
        return {
            "site_id": client._site_id,
            "build_id": client._build_id,
            "cookies": dict(client._client.cookies),
        }
    finally:
        client.close()


@router.get("")
async def list_credentials(
    vault: CredentialVault = Depends(_get_vault),
    settings=Depends(_get_settings),
) -> dict:
    """Credential health for every configured library.

    Drives the Settings screen. Contains usernames but no passwords.
    """
    # list() snapshots the registry, which can change while this awaits.
    statuses = [(await vault.status(name)).to_dict() for name in list(settings.libraries)]
    return {
        "writable": vault.writable,
        "libraries": statuses,
    }


@router.put("/{library}")
async def put_credentials(
    library: str,
    payload: CredentialInput,
    vault: CredentialVault = Depends(_get_vault),
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
) -> dict:
    """Verify, then store, credentials for one library."""
    subdomain = _validate_library(library, settings)

    if not vault.writable:
        raise HTTPException(
            status_code=409,
            detail=(
                "Credentials cannot be saved because BORROWBOX_SECRET_KEY is not set. "
                "Set it and restart so credentials can be encrypted at rest."
            ),
        )

    session_data: dict | None = None
    if payload.verify:
        # Prove the credentials work before persisting them, so a typo cannot be
        # saved and silently break the library later.
        try:
            session_data = await asyncio.to_thread(
                _login_blocking, subdomain, payload.username, payload.password
            )
        except Exception as exc:  # noqa: BLE001 - upstream raises many types
            await vault.record_verification(library, ok=False, error=str(exc)[:200])
            raise HTTPException(
                status_code=400,
                detail=f"Sign-in was rejected by {library}; credentials not saved.",
            ) from exc

    try:
        await vault.put(library, payload.username, payload.password)
    except CredentialError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if payload.verify:
        await vault.record_verification(library, ok=True)
        if session_data:
            # Reuse the session we just proved works, skipping a second login.
            await store.set_session("default", library, session_data)
    logger.info("Stored credentials for %s (verified=%s)", library, payload.verify)

    return {"status": "ok", **(await vault.status(library)).to_dict()}


@router.post("/{library}/verify")
async def verify_credentials(
    library: str,
    vault: CredentialVault = Depends(_get_vault),
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
) -> dict:
    """Re-check the credentials already held for a library."""
    subdomain = _validate_library(library, settings)

    try:
        creds = await vault.get(library)
    except CredentialError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not creds:
        raise HTTPException(status_code=404, detail=f"No credentials stored for '{library}'")

    username, password = creds
    try:
        session_data = await asyncio.to_thread(_login_blocking, subdomain, username, password)
    except Exception as exc:  # noqa: BLE001
        await vault.record_verification(library, ok=False, error=str(exc)[:200])
        return {"status": "failed", **(await vault.status(library)).to_dict()}

    await vault.record_verification(library, ok=True)
    await store.set_session("default", library, session_data)
    return {"status": "ok", **(await vault.status(library)).to_dict()}


@router.delete("/{library}")
async def delete_credentials(
    library: str,
    vault: CredentialVault = Depends(_get_vault),
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
) -> dict:
    """Forget stored credentials for a library.

    Any cached session is dropped too, otherwise the library would keep working
    until the session expired and appear not to have been removed.
    """
    _validate_library(library, settings)
    removed = await vault.delete(library)
    await store.delete_session("default", library)
    return {
        "status": "deleted" if removed else "not_found",
        **(await vault.status(library)).to_dict(),
    }
