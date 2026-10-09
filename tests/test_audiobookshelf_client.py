from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from borrowbox_server import routes_auth
from yoto_server import deps

from yoto_bulk.audiobookshelf import ABSClient


def test_abs_client_adds_cloudflare_access_service_token_headers() -> None:
    client = ABSClient(
        "https://abs.example",
        "abs-token",
        cf_access_client_id="access-client-id",
        cf_access_client_secret="access-client-secret",
    )

    try:
        assert client._client.headers["Authorization"] == "Bearer abs-token"
        assert client._client.headers["CF-Access-Client-Id"] == "access-client-id"
        assert client._client.headers["CF-Access-Client-Secret"] == "access-client-secret"
    finally:
        asyncio.run(client.aclose())


def test_abs_client_requires_both_cloudflare_access_credentials() -> None:
    with pytest.raises(ValueError, match="must be configured together"):
        ABSClient(
            "https://abs.example",
            "abs-token",
            cf_access_client_id="access-client-id",
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda settings: routes_auth._get_abs_client(settings),
        lambda settings: deps.get_abs_client(
            SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))
        ),
    ],
)
def test_service_abs_client_factories_forward_cloudflare_access_credentials(factory) -> None:
    settings = SimpleNamespace(
        abs_url="https://abs.example",
        abs_token="abs-token",
        abs_cf_access_client_id="access-client-id",
        abs_cf_access_client_secret="access-client-secret",
        request_timeout_seconds=30.0,
    )

    client = factory(settings)
    try:
        assert client._client.headers["CF-Access-Client-Id"] == "access-client-id"
        assert client._client.headers["CF-Access-Client-Secret"] == "access-client-secret"
    finally:
        asyncio.run(client.aclose())