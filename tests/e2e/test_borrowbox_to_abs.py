from __future__ import annotations

import asyncio
import io
import json
import os
import zipfile
from pathlib import Path

import httpx
import pytest

from borrowbox import BorrowBoxClient
from yoto_bulk.audiobookshelf import ABSClient


pytestmark = pytest.mark.e2e


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.skip(f"{name} is not configured")
    return value


def _normalize_title(value: str) -> str:
    return "".join(ch.lower() for ch in value if ch.isalnum())


def _audio_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    exts = {".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".opus", ".wav", ".flac"}
    return [
        info
        for info in archive.infolist()
        if Path(info.filename).suffix.lower() in exts and not info.is_dir()
    ]


def _match_author(expected: list[str], actual: str) -> bool:
    if not expected:
        return True
    actual_norm = _normalize_title(actual)
    return any(_normalize_title(author) in actual_norm for author in expected if author)


def _borrowbox_advertised_sizes(library: str, product_id: str) -> tuple[int, int]:
    libraries_raw = os.environ.get("BORROWBOX_LIBRARIES", "")
    credentials_raw = os.environ.get("BORROWBOX_CREDENTIALS", "")
    if not libraries_raw or not credentials_raw:
        return 0, 0

    libraries = json.loads(libraries_raw)
    credentials = json.loads(credentials_raw)
    subdomain = libraries.get(library)
    creds = credentials.get(library)
    if not subdomain or not creds:
        return 0, 0

    with BorrowBoxClient(subdomain) as client:
        client.login(creds["username"], creds["password"])
        loans = client.get_current_loans()

    for loan in loans:
        product = loan.get("product", {}) or {}
        if str(product.get("productId") or "") != product_id:
            continue
        specifics = product.get("formatSpecifics") or {}
        tracks = int(specifics.get("totalTrackNumber") or specifics.get("totalTracks") or 0)
        size = int(specifics.get("fileSize") or 0)
        return tracks, size

    return 0, 0


def _find_matching_loan(
    loans: list[dict], product_id: str, *, loan_type: str | None = None
) -> dict | None:
    matches = [
        entry
        for entry in loans
        if str(entry.get("product_id") or "") == product_id
        and (loan_type is None or str(entry.get("loan_type") or "") == loan_type)
    ]
    if not matches:
        return None
    return matches[-1]


def test_borrowbox_import_to_abs_via_running_services() -> None:
    base_url = os.environ.get("YOTO_BULK_E2E_BORROWBOX_BASE_URL", "http://localhost:8000").rstrip("/")
    library = _require_env("YOTO_BULK_E2E_LIBRARY")
    product_id = _require_env("YOTO_BULK_E2E_PRODUCT_ID")
    abs_url = _require_env("YOTO_BULK_ABS_URL")
    abs_token = _require_env("YOTO_BULK_ABS_TOKEN")
    abs_cf_access_client_id = (
        os.environ.get("YOTO_BULK_ABS_CF_ACCESS_CLIENT_ID", "").strip() or None
    )
    abs_cf_access_client_secret = (
        os.environ.get("YOTO_BULK_ABS_CF_ACCESS_CLIENT_SECRET", "").strip() or None
    )
    abs_library_name = os.environ.get("YOTO_BULK_ABS_LIBRARY_NAME", "").strip() or None
    api_key = os.environ.get("BORROWBOX_API_KEY", "").strip()

    headers = {"X-API-Key": api_key} if api_key else {}

    with httpx.Client(timeout=300.0, follow_redirects=True) as client:
        product_res = client.get(f"{base_url}/api/catalog/product/{library}/{product_id}", headers=headers)
        product_res.raise_for_status()
        product = product_res.json()

        expected_title = str(product["title"])
        expected_authors = list(product.get("authors") or [])

        def _fetch_loans() -> list[dict]:
            loans_res = client.get(f"{base_url}/api/auth/loans/{library}", headers=headers)
            loans_res.raise_for_status()
            return loans_res.json()

        loans = _fetch_loans()
        availability = str(product.get("availability") or "").lower()

        loan = _find_matching_loan(loans, product_id, loan_type="loan")
        if loan is None and availability == "available":
            action_res = client.post(
                f"{base_url}/api/auth/borrow",
                json={"product_id": product_id, "library": library},
                headers=headers,
            )
            action_res.raise_for_status()
            action_data = action_res.json()
            loans = _fetch_loans()
            if action_data.get("success") is not True:
                loan = _find_matching_loan(loans, product_id, loan_type="loan")
                if loan is None:
                    # Check if it was actually borrowed but maybe as a different type?
                    reserve = _find_matching_loan(loans, product_id, loan_type="reserve")
                    if reserve:
                        loan = reserve
                    else:
                        pytest.skip(
                            "BorrowBox reported the product as available but refused checkout "
                            f"for {product_id}: {action_data.get('message') or 'unknown error'}"
                        )
            else:
                loan = _find_matching_loan(loans, product_id, loan_type="loan")
        elif loan is None:
            # Not available, and not already borrowed. Try to find reserve.
            reserve = _find_matching_loan(loans, product_id, loan_type="reserve")
            if reserve is None:
                action_res = client.post(
                    f"{base_url}/api/auth/reserve",
                    json={"product_id": product_id, "library": library},
                    headers=headers,
                )
                action_res.raise_for_status()
                action_data = action_res.json()
                assert action_data.get("success") is True, action_data
                loans = _fetch_loans()
                reserve = _find_matching_loan(loans, product_id, loan_type="reserve")
            assert reserve is not None, (
                f"Product {product_id} was not present in {library} loans after reserve step"
            )
            assert str(reserve.get("loan_type") or "") == "reserve"
            return

        assert loan is not None, f"Product {product_id} was not present in {library} loans after borrow step"
        if str(loan.get("loan_type") or "") != "loan":
            assert str(loan.get("loan_type") or "") == "reserve"
            return

        loan_id = str(loan["loan_id"])
        expected_title = str(loan["title"])
        expected_authors = list(loan.get("authors") or [])
        expected_tracks = int(loan.get("total_tracks") or 0)
        expected_file_size = int(loan.get("file_size") or 0)
        if expected_tracks <= 0 or expected_file_size <= 0:
            fallback_tracks, fallback_size = _borrowbox_advertised_sizes(library, product_id)
            if expected_tracks <= 0:
                expected_tracks = fallback_tracks
            if expected_file_size <= 0:
                expected_file_size = fallback_size

        download_res = client.get(f"{base_url}/api/auth/download/{library}/{loan_id}", headers=headers)
        download_res.raise_for_status()
        download_url = str(download_res.json()["download_url"])
        assert download_url.startswith(("http://", "https://"))

        zip_res = client.get(download_url)
        zip_res.raise_for_status()
        zip_bytes = zip_res.content
        downloaded_zip_size = len(zip_bytes)
        assert downloaded_zip_size > 0

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            audio_files = _audio_members(archive)
            zipped_tracks = len(audio_files)
            uncompressed_audio_size = sum(info.file_size for info in audio_files)

        assert zipped_tracks > 0, "Downloaded ZIP did not contain any audio tracks"
        if expected_tracks <= 0:
            expected_tracks = zipped_tracks
        assert zipped_tracks == expected_tracks

        if expected_file_size > 0:
            size_ratio = downloaded_zip_size / expected_file_size
            assert 0.5 <= size_ratio <= 2.5, (
                f"BorrowBox file size sanity mismatch: expected {expected_file_size}, "
                f"got {downloaded_zip_size} (ratio={size_ratio:.2f})"
            )

        import_res = client.post(
            f"{base_url}/api/auth/import/{library}/{loan_id}",
            params={"product_id": product_id},
            headers=headers,
        )
        import_res.raise_for_status()
        import_data = import_res.json()
        assert import_data["status"] == "imported", import_data
        item_id = str(import_data["item_id"])

    async def _verify_abs() -> None:
        abs_client = ABSClient(
            abs_url,
            abs_token,
            cf_access_client_id=abs_cf_access_client_id,
            cf_access_client_secret=abs_cf_access_client_secret,
        )
        try:
            if abs_library_name:
                library_obj = await abs_client.get_book_library(abs_library_name)
                assert library_obj["name"] == abs_library_name

            item = await abs_client.get_item(item_id)
            playback = await abs_client.get_playback_session(item_id)

            metadata = (item.get("media") or {}).get("metadata") or {}
            title = str(metadata.get("title") or item.get("title") or "")
            author_values = []
            if metadata.get("author"):
                author_values.append(str(metadata["author"]))
            if metadata.get("authorName"):
                author_values.append(str(metadata["authorName"]))
            for name in metadata.get("authors") or []:
                if isinstance(name, dict):
                    value = str(name.get("name") or "").strip()
                else:
                    value = str(name or "").strip()
                if value:
                    author_values.append(value)
            author = ", ".join(dict.fromkeys(author_values))
            audio_tracks = playback.get("audioTracks") or []

            try:
                assert _normalize_title(expected_title) in _normalize_title(title)
                if not _match_author(expected_authors, author):
                    print(f"Warning: Author mismatch for item {item_id}. Expected {expected_authors}, got {author!r}")
                assert len(audio_tracks) == expected_tracks
            except AssertionError:
                print(f"ABS verification failed for item {item_id}")
                print(f"Expected: title={expected_title}, authors={expected_authors}, tracks={expected_tracks}")
                print(f"Actual: title={title}, author={author}, tracks={len(audio_tracks)}")
                print(f"Full item metadata: {json.dumps(metadata, indent=2)}")
                raise
        finally:
            await abs_client.aclose()

    asyncio.run(_verify_abs())
