"""Library registry endpoints.

Adding or removing a BorrowBox library used to require editing
``BORROWBOX_LIBRARIES`` and restarting. These endpoints make it a UI operation;
changes take effect immediately without a restart.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from borrowbox_server import libraries as registry
from borrowbox_server.credentials import CredentialVault
from borrowbox_server.store import Store

router = APIRouter()
logger = logging.getLogger(__name__)


def _get_store(request: Request) -> Store:
    return request.app.state.store


def _get_settings(request: Request):
    return request.app.state.settings


def _get_vault(request: Request) -> CredentialVault:
    return request.app.state.vault


class LibraryInput(BaseModel):
    """A library to register."""

    name: str = Field(min_length=1, max_length=49, description="Short identifier, e.g. 'hounslow'")
    subdomain: str = Field(
        min_length=1,
        max_length=63,
        description="BorrowBox subdomain, e.g. 'hounslow' for hounslow.borrowbox.com",
    )


@router.get("")
async def list_registered(
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
    vault: CredentialVault = Depends(_get_vault),
) -> dict:
    """Registered libraries with their credential state."""
    mapping = await registry.refresh(store, settings)
    out = []
    for name, subdomain in sorted(mapping.items()):
        status = await vault.status(name)
        out.append({"name": name, "subdomain": subdomain, **status.to_dict()})
    return {"libraries": out, "writable": vault.writable}


@router.post("", status_code=201)
async def add_library(
    payload: LibraryInput,
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
) -> dict:
    """Register a new library."""
    try:
        name = registry.normalise_name(payload.name)
        subdomain = registry.normalise_subdomain(payload.subdomain)
    except registry.LibraryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if name in settings.libraries:
        raise HTTPException(status_code=409, detail=f"Library '{name}' already exists")

    await registry.add(store, settings, name, subdomain)
    logger.info("Registered library %s -> %s", name, subdomain)
    return {"status": "created", "name": name, "subdomain": subdomain}


@router.delete("/{name}")
async def remove_library(
    name: str,
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
    vault: CredentialVault = Depends(_get_vault),
) -> dict:
    """Deregister a library and forget everything tied to it.

    Its credentials are deleted too, so removing a library never leaves an
    orphaned secret behind in Redis.
    """
    try:
        clean = registry.normalise_name(name)
    except registry.LibraryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if clean not in settings.libraries:
        raise HTTPException(status_code=404, detail=f"Unknown library '{clean}'")

    await vault.delete(clean)
    await store.delete_session("default", clean)
    existed = await registry.remove(store, settings, clean)
    logger.info("Deregistered library %s (credentials removed)", clean)
    return {"status": "deleted" if existed else "not_found", "name": clean}
