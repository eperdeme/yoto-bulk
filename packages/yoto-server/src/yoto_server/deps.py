"""Shared dependency helpers for yoto-server route modules."""

from __future__ import annotations

from fastapi import HTTPException, Request

from yoto_bulk.audiobookshelf import ABSClient
from yoto_bulk.auth import YotoAuthService
from yoto_server.store import YotoStore, extract_user_id_from_jwt


def get_settings(request: Request):
    return request.app.state.settings


def get_store(request: Request) -> YotoStore:
    return request.app.state.store


def get_abs_client(request: Request) -> ABSClient:
    settings = request.app.state.settings
    if not settings.abs_token:
        raise HTTPException(
            status_code=503,
            detail="Audiobookshelf is not configured. Set YOTO_BULK_ABS_URL and YOTO_BULK_ABS_TOKEN.",
        )
    return ABSClient(
        settings.abs_url,
        settings.abs_token,
        timeout=settings.request_timeout_seconds * 4,
        cf_access_client_id=settings.abs_cf_access_client_id,
        cf_access_client_secret=settings.abs_cf_access_client_secret,
    )


async def get_user_id(request: Request) -> str:
    """Resolve the current user_id from stored OAuth tokens."""
    from yoto_bulk.config import Settings as YotoBulkSettings

    yoto_settings = YotoBulkSettings()
    auth_service = YotoAuthService(yoto_settings)
    tokens = auth_service.token_store.load()
    if tokens is None:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated. Configure saved Yoto OAuth tokens before using Yoto sync.",
        )
    return extract_user_id_from_jwt(tokens.access_token)
