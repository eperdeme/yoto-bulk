from __future__ import annotations

from types import SimpleNamespace

import httpx

from borrowbox.auth import BorrowBoxClient, _normalize_download_url


def test_normalize_download_url_handles_protocol_relative_urls() -> None:
    assert (
        _normalize_download_url("//download-eu.borrowbox.com/file.zip")
        == "https://download-eu.borrowbox.com/file.zip"
    )


def test_normalize_download_url_handles_root_relative_urls() -> None:
    assert (
        _normalize_download_url(
            "/signed/file.zip",
            base_url="https://mdp.bolindadigital.com/v1/audiobook/abc/tracks/1-6",
        )
        == "https://mdp.bolindadigital.com/signed/file.zip"
    )


def test_get_download_url_prefers_location_header() -> None:
    client = BorrowBoxClient("example")
    client._logged_in = True
    client._api_request = lambda method, path: {"downloadUrl": "https://mdp.example/resolve"}  # type: ignore[method-assign]
    client._client = SimpleNamespace(  # type: ignore[assignment]
        get=lambda url: httpx.Response(
            202,
            headers={"Location": "//download-eu.borrowbox.com/final.zip"},
            request=httpx.Request("GET", url),
        )
    )

    final_url = client.get_download_url("291194018", total_tracks=6)

    assert final_url == "https://download-eu.borrowbox.com/final.zip"


def test_get_download_url_normalizes_json_url_without_scheme() -> None:
    client = BorrowBoxClient("example")
    client._logged_in = True
    client._api_request = lambda method, path: {"downloadUrl": "https://mdp.example/resolve"}  # type: ignore[method-assign]
    client._client = SimpleNamespace(  # type: ignore[assignment]
        get=lambda url: httpx.Response(
            200,
            json={"url": "download-eu.borrowbox.com/final.zip"},
            request=httpx.Request("GET", url),
        )
    )

    final_url = client.get_download_url("291194018", total_tracks=6)

    assert final_url == "https://download-eu.borrowbox.com/final.zip"


def test_get_download_url_follows_progress_url_until_final_download() -> None:
    client = BorrowBoxClient("example")
    client._logged_in = True
    client._api_request = lambda method, path: {"downloadUrl": "https://mdp.example/resolve"}  # type: ignore[method-assign]

    responses = iter(
        [
            httpx.Response(
                202,
                json={"progressUrl": "/v1/audiobook/abc/progress?token=1"},
                request=httpx.Request("GET", "https://mdp.example/resolve"),
            ),
            httpx.Response(
                200,
                json={"url": "https://download-eu.borrowbox.com/final.zip"},
                request=httpx.Request("GET", "https://mdp.example/v1/audiobook/abc/progress?token=1"),
            ),
        ]
    )
    client._client = SimpleNamespace(get=lambda url: next(responses))  # type: ignore[assignment]

    final_url = client.get_download_url("291194018", total_tracks=6)

    assert final_url == "https://download-eu.borrowbox.com/final.zip"


def test_return_loan_uses_delete_on_loan_resource() -> None:
    client = BorrowBoxClient("example")
    client._logged_in = True
    calls: list[tuple[str, str]] = []

    def fake_api_request(method: str, path: str):
        calls.append((method, path))
        return {"ok": True}

    client._api_request = fake_api_request  # type: ignore[method-assign]

    result = client.return_loan("291171938")

    assert result == {"ok": True}
    assert calls == [("DELETE", "/api/v1/loans/291171938")]


def test_reserve_uses_checkout_endpoint_with_reserve_loan_type() -> None:
    client = BorrowBoxClient("example")
    client._logged_in = True
    calls: list[tuple[str, str, dict[str, str] | None]] = []

    def fake_api_request(method: str, path: str, params=None):
        calls.append((method, path, params))
        return {"ok": True}

    client._api_request = fake_api_request  # type: ignore[method-assign]

    result = client.reserve("BOL_6748614")

    assert result == {"ok": True}
    assert calls == [
        ("PUT", "/api/v1/loans/checkout", {"productId": "BOL_6748614", "loanType": "reserve"})
    ]
