"""Auth and loan management API routes."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
import logging
from datetime import UTC, datetime
from typing import Any, Callable

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from borrowbox import BorrowBoxCatalog, BorrowBoxClient
from borrowbox.auth import _normalize_download_url
from yoto_bulk.abs_sync import sync_abs_item_to_redis
from yoto_bulk.workflows import report_workflow
from yoto_bulk.audiobookshelf import ABSClient, _abs_sanitize_filename, _normalize_match_text, _split_people, _titles_loosely_match
from borrowbox_server.credentials import CredentialError, vault_for
from borrowbox_server.importer import stage_borrowbox_zip_import
from borrowbox_server.zip_archive import get_archived_zip, list_archives, save_zip
from borrowbox_server.schemas import (
    BorrowRequest,
    BorrowResponse,
    LoanInfo,
)
from borrowbox_server.store import Store

logger = logging.getLogger(__name__)

router = APIRouter()


def _epoch_to_str(ts: int | float | str | None) -> str | None:
    """Convert epoch millis to a human-readable date string."""
    if ts is None:
        return None
    if isinstance(ts, str):
        return ts
    return datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%d %B %Y")


def _get_store(request: Request) -> Store:
    return request.app.state.store


def _get_settings(request: Request):
    return request.app.state.settings


async def _get_credentials(library: str, settings, store=None) -> tuple[str, str]:
    """Resolve credentials for a library.

    Prefers the encrypted Redis store, falling back to the
    ``BORROWBOX_CREDENTIALS`` env seed, so existing deployments keep working
    while newly saved credentials take effect immediately.
    """
    try:
        creds = await vault_for(store, settings).get(library)
    except CredentialError as exc:
        # e.g. the secret key changed since the credentials were sealed.
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if not creds:
        raise HTTPException(
            status_code=400,
            detail=f"No credentials configured for library '{library}'",
        )
    return creds


def _validate_library(library: str, settings) -> str:
    if library not in settings.libraries:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown library '{library}'",
        )
    return settings.libraries[library]


async def _with_client(
    library: str, settings, fn: Callable[[BorrowBoxClient], Any],
    store: Store | None = None,
) -> Any:
    """Run a sync BorrowBoxClient operation in a thread.

    Handles library validation, credential lookup, client lifecycle,
    and wraps exceptions as HTTP 502.  When *store* is provided, session
    cookies are cached in Redis so subsequent calls can skip the login
    round-trip (the biggest WAF trigger).
    """
    subdomain = _validate_library(library, settings)
    username, password = await _get_credentials(library, settings, store)

    # Try to restore a cached session first
    cached_session: dict | None = None
    if store:
        cached_session = await store.get_session("default", library)

    def _run():
        client = BorrowBoxClient(subdomain)
        try:
            restored = False
            if cached_session:
                restored = client.restore_session(cached_session)
                if restored:
                    logger.debug("Using cached session for %s", library)

            if not restored:
                client.login(username, password)

            try:
                result = fn(client)
            except httpx.HTTPStatusError as exc:
                # If we restored a stale session and it 401'd, re-login once
                if restored and exc.response.status_code == 401:
                    logger.info("Cached session for %s expired (401), re-logging in", library)
                    client.login(username, password)
                    result = fn(client)
                else:
                    raise

            # Heuristic: if we restored a session and got an empty list back,
            # the session may be stale (SSR returns 200 with no loans for
            # unauthenticated users). Re-login and retry once.
            if restored and isinstance(result, list) and len(result) == 0:
                logger.info(
                    "Cached session for %s returned empty results, re-logging in to verify",
                    library,
                )
                client.login(username, password)
                result = fn(client)

            return result, client.export_session()
        finally:
            client.close()

    try:
        result, session_data = await asyncio.to_thread(_run)
        # Persist the (possibly refreshed) session cookies
        if store and session_data:
            await store.set_session("default", library, session_data)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _get_abs_client(settings) -> ABSClient:
    if not settings.abs_token:
        raise HTTPException(
            status_code=503,
            detail="Audiobookshelf is not configured. Set YOTO_BULK_ABS_URL and YOTO_BULK_ABS_TOKEN.",
        )
    return ABSClient(
        settings.abs_url,
        settings.abs_token,
        cf_access_client_id=settings.abs_cf_access_client_id,
        cf_access_client_secret=settings.abs_cf_access_client_secret,
    )


async def _get_bb_product(
    library: str,
    product_id: str,
    store: Store,
    settings,
) -> dict[str, Any] | None:
    cached = await store.get_product(library, product_id)
    if cached:
        return cached

    subdomain = _validate_library(library, settings)

    def _fetch() -> dict[str, Any] | None:
        with BorrowBoxCatalog(subdomain) as cat:
            detail = cat.get_product(product_id)
            if detail is None:
                return None
            if is_dataclass(detail):
                return asdict(detail)
            return dict(detail)

    product = await asyncio.to_thread(_fetch)
    if product and hasattr(store, "set_product"):
        await store.set_product(library, product_id, product)
    return product


def _bb_full_title(product: dict) -> str:
    """Compose the full title from BorrowBox's split title/series fields.

    BorrowBox stores series books with the series name separate:
      title="Holiday Heatwave", seriesName="Hotel Flamingo"
    We need the canonical form: "Hotel Flamingo: Holiday Heatwave"
    """
    title = (product.get("title") or "").strip()
    # Cached products are dataclass-asdict (snake_case); raw API responses are
    # camelCase. Tolerate both.
    series = (product.get("series_name") or product.get("seriesName") or "").strip()
    if not series or not title:
        return title or series or ""
    # Don't double-prefix if the title already starts with the series name
    if title.lower().startswith(series.lower()):
        return title
    return f"{series}: {title}"


def _normalize_loan(ln: dict, product: dict) -> LoanInfo:
    """Normalize a raw BorrowBox loan dict into a LoanInfo."""
    fmt_specifics = product.get("formatSpecifics") or {}
    fmt = fmt_specifics.get("loanFormat") or product.get("format")
    pid = product.get("productId", "")
    cover_url = product.get("coverUrl")
    if not cover_url and pid:
        cover_url = f"https://covers.borrowbox.com/v3/covers/{pid}"
    return LoanInfo(
        product_id=pid,
        title=_bb_full_title(product),
        authors=[a["name"] for a in (product.get("authors") or []) if a.get("name")],
        start_date=_epoch_to_str(ln.get("startDate")),
        end_date=_epoch_to_str(ln.get("endDate")),
        format=fmt,
        cover_url=cover_url,
        loan_type=ln.get("loanType"),
        loan_id=str(ln["id"]) if "id" in ln else None,
        total_tracks=fmt_specifics.get("totalTracks") or fmt_specifics.get("totalTrackNumber"),
        file_size=fmt_specifics.get("fileSize"),
    )


def _abs_search_match(
    loan: LoanInfo,
    library_item: dict[str, Any],
) -> bool:
    metadata = ((library_item.get("media") or {}).get("metadata") or {})
    candidate_title = _normalize_match_text(str(metadata.get("title") or library_item.get("title") or ""))

    # A series loan title ("Goosebumps: Welcome to Dead House") is usually stored
    # in ABS under the bare book title, so also compare against the text after a
    # leading "Series: " prefix.
    expected_titles = [_normalize_match_text(loan.title)]
    if ": " in loan.title:
        tail = _normalize_match_text(loan.title.split(": ", 1)[1])
        if tail:
            expected_titles.append(tail)
    if not any(_titles_loosely_match(expected, candidate_title) for expected in expected_titles if expected):
        return False

    expected_authors = [_normalize_match_text(a) for a in loan.authors if a]
    if not expected_authors:
        return True

    candidate_author = _normalize_match_text(str(metadata.get("author") or ""))
    candidate_authors = [
        _normalize_match_text(author)
        for author in _split_people(metadata.get("authors"))
        if author
    ]
    candidate_people = {p for p in [candidate_author, *candidate_authors] if p}
    return any(author in candidate_people for author in expected_authors)


async def _mark_loans_present_in_abs(
    loans: list[LoanInfo],
    settings,
    store: Store | None = None,
    library: str | None = None,
) -> list[LoanInfo]:
    if not loans or not settings.abs_token:
        return loans

    abs_client = _get_abs_client(settings)
    try:
        abs_library = await abs_client.get_book_library(settings.abs_library_name)
        library_id = abs_library["id"]
        all_items = await abs_client.list_library_items(library_id)
        live_ids = {item.get("id") for item in all_items if item.get("id")}

        for loan in loans:
            if not loan.title or loan.loan_type != "loan":
                continue

            # 1. Stable ID link recorded at import time. This survives ABS
            #    metadata/title edits (e.g. provider auto-match), unlike the
            #    title-based matching below. Validate the mapped item still
            #    exists in the live library before trusting it.
            if store is not None and library and loan.product_id:
                linked_id = await store.get_abs_link(library, loan.product_id)
                if linked_id and linked_id in live_ids:
                    loan.in_abs = True
                    loan.abs_item_id = linked_id
                    continue

            # 2. Fall back to title+author fuzzy match, then path suffix.
            matched_id: str | None = None
            for item in all_items:
                if _abs_search_match(loan, item):
                    matched_id = item.get("id")
                    break
            else:
                # Path-based fallback: check if ABS item path ends with sanitized title
                sanitized_title = _abs_sanitize_filename(loan.title)
                if sanitized_title:
                    for item in all_items:
                        item_path = item.get("path") or ""
                        if item_path.endswith("/" + sanitized_title) or item_path.endswith("\\" + sanitized_title):
                            matched_id = item.get("id")
                            break

            if matched_id:
                loan.in_abs = True
                loan.abs_item_id = matched_id
                # Back-fill the stable link so later refreshes survive edits even
                # for books imported before the link was recorded.
                if store is not None and library and loan.product_id:
                    try:
                        await store.set_abs_link(library, loan.product_id, matched_id)
                    except Exception:
                        pass

        return loans
    finally:
        await abs_client.aclose()


def _bb_author_names(bb_product: dict | None) -> list[str]:
    """Extract author display names from a BorrowBox product doc.

    Tolerates both the cached snake_case ``list[str]`` shape and the raw
    BorrowBox ``list[{"name": ...}]`` (camelCase) shape.
    """
    if not bb_product:
        return []
    raw = bb_product.get("authors") or []
    names: list[str] = []
    if isinstance(raw, list):
        for entry in raw:
            if not entry:
                continue
            name = entry.get("name") if isinstance(entry, dict) else str(entry)
            if name:
                names.append(str(name))
    elif isinstance(raw, str):
        names.append(raw)
    return [cleaned for n in names if (cleaned := n.strip().rstrip("-").strip())]


async def _find_existing_abs_item(
    settings,
    store: Store,
    library: str,
    product_id: str | None,
    bb_product: dict | None,
) -> str | None:
    """Return the ABS item id this loan was already imported to, or None.

    Prefers the recorded ``product_id -> item id`` link, then falls back to
    title+author matching so books already in ABS are detected even without a
    link. Used to avoid creating duplicate ABS items on re-import.
    """
    if not settings.abs_token or not product_id:
        return None

    abs_client = _get_abs_client(settings)
    try:
        abs_library = await abs_client.get_book_library(settings.abs_library_name)
        all_items = await abs_client.list_library_items(abs_library["id"])
        live_ids = {item.get("id") for item in all_items if item.get("id")}

        linked_id = await store.get_abs_link(library, product_id)
        if linked_id and linked_id in live_ids:
            return linked_id

        if bb_product:
            probe = LoanInfo(
                product_id=product_id,
                title=_bb_full_title(bb_product),
                authors=_bb_author_names(bb_product),
                loan_type="loan",
                loan_id="probe",
            )
            if probe.title:
                for item in all_items:
                    if _abs_search_match(probe, item):
                        matched_id = item.get("id")
                        if matched_id:
                            # Repair/record the link for future lookups.
                            try:
                                await store.set_abs_link(library, product_id, matched_id)
                            except Exception:
                                pass
                            return matched_id
        return None
    finally:
        await abs_client.aclose()


# ------------------------------------------------------------------
# Login (creates server-side session)
# ------------------------------------------------------------------


@router.post("/login/{library}")
async def login(
    library: str,
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
) -> dict[str, str]:
    """Login to a BorrowBox library using configured credentials."""
    subdomain = _validate_library(library, settings)
    username, password = await _get_credentials(library, settings, store)

    def _do_login():
        client = BorrowBoxClient(subdomain)
        result = client.login(username, password)
        # Extract session state to cache
        cookies = dict(client._client.cookies)
        session_data = {
            "site_id": client._site_id,
            "build_id": client._build_id,
            "cookies": cookies,
        }
        client.close()
        return result, session_data

    try:
        result, session_data = await asyncio.to_thread(_do_login)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    await store.set_session("default", library, session_data)
    return {"status": "ok", "library": library}


# ------------------------------------------------------------------
# Current loans
# ------------------------------------------------------------------


@router.get("/loans/{library}")
async def get_loans(
    library: str,
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
) -> list[LoanInfo]:
    """Get current loans for a library."""
    # Check cache
    cached = await store.get_current_loans("default", library)
    if cached is not None:
        return [LoanInfo(**loan) for loan in cached]

    raw_loans = await _with_client(
        library, settings, lambda c: c.get_current_loans(), store=store
    )

    # Normalize to LoanInfo shape
    loans = []
    for ln in raw_loans:
        product = ln.get("product", ln)
        loans.append(_normalize_loan(ln, product))

    loans = await _mark_loans_present_in_abs(loans, settings, store=store, library=library)
    await store.set_current_loans("default", library, [l.model_dump() for l in loans])
    return loans


# ------------------------------------------------------------------
# Loan history
# ------------------------------------------------------------------


@router.get("/history/{library}")
async def get_history(
    library: str,
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
) -> list[LoanInfo]:
    """Get loan history for a library."""
    cached = await store.get_loan_history("default", library)
    if cached is not None:
        return [LoanInfo(**loan) for loan in cached]

    raw_loans = await _with_client(
        library, settings, lambda c: c.get_loan_history(), store=store
    )

    loans = []
    for ln in raw_loans:
        product = ln.get("product", ln)
        loans.append(_normalize_loan(ln, product))

    await store.set_loan_history("default", library, [l.model_dump() for l in loans])
    return loans


# ------------------------------------------------------------------
# Borrow
# ------------------------------------------------------------------


@router.post("/borrow")
async def borrow(
    req: BorrowRequest,
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
) -> BorrowResponse:
    """Borrow an item from a library."""
    try:
        await _with_client(
            req.library, settings, lambda c: c.borrow(req.product_id), store=store
        )
    except HTTPException as exc:
        # Invalidate anyway in case we are out of sync
        await store._r.delete(f"bb:loans:default:{req.library}")
        return BorrowResponse(
            success=False,
            message=exc.detail,
            product_id=req.product_id,
            library=req.library,
        )

    # Invalidate loans + credits cache so next fetch is fresh
    await store._r.delete(f"bb:loans:default:{req.library}")
    await store._r.delete(f"bb:credits:default:{req.library}")

    return BorrowResponse(
        success=True,
        message="Borrowed successfully",
        product_id=req.product_id,
        library=req.library,
    )


# ------------------------------------------------------------------
# Reserve
# ------------------------------------------------------------------


@router.post("/reserve")
async def reserve(
    req: BorrowRequest,
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
) -> BorrowResponse:
    """Place a reservation on an unavailable item."""
    try:
        await _with_client(
            req.library, settings, lambda c: c.reserve(req.product_id), store=store
        )
    except HTTPException as exc:
        await store._r.delete(f"bb:loans:default:{req.library}")
        return BorrowResponse(
            success=False,
            message=exc.detail,
            product_id=req.product_id,
            library=req.library,
        )

    await store._r.delete(f"bb:loans:default:{req.library}")

    return BorrowResponse(
        success=True,
        message="Reserved successfully",
        product_id=req.product_id,
        library=req.library,
    )


# ------------------------------------------------------------------
# Return
# ------------------------------------------------------------------


@router.post("/return")
async def return_loan(
    req: BorrowRequest,
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
) -> BorrowResponse:
    """Return a borrowed item early."""
    if not req.loan_id:
        return BorrowResponse(
            success=False,
            message="loan_id is required to return a loan",
            product_id=req.product_id,
            library=req.library,
        )
    try:
        await _with_client(
            req.library, settings, lambda c: c.return_loan(req.loan_id), store=store
        )
    except HTTPException as exc:
        await store._r.delete(f"bb:loans:default:{req.library}")
        return BorrowResponse(
            success=False,
            message=exc.detail,
            product_id=req.product_id,
            library=req.library,
        )

    await store._r.delete(f"bb:loans:default:{req.library}")

    return BorrowResponse(
        success=True,
        message="Returned successfully",
        product_id=req.product_id,
        library=req.library,
    )


# ------------------------------------------------------------------
# Credits
# ------------------------------------------------------------------


@router.get("/credits/{library}")
async def get_credits(
    library: str,
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
) -> dict:
    """Get remaining borrow credits for a library."""
    _validate_library(library, settings)

    cached = await store.get_credits("default", library)
    if cached is not None:
        return {"library": library, "credits": cached}

    credits = await _with_client(
        library, settings, lambda c: c.get_credits(), store=store
    )

    await store.set_credits("default", library, credits)
    return {"library": library, "credits": credits}


# ------------------------------------------------------------------
# Download
# ------------------------------------------------------------------


@router.get("/download/{library}/{loan_id}")
async def download(
    library: str,
    loan_id: str,
    total_tracks: int | None = None,
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
) -> dict:
    """Get a signed download URL for an audiobook loan."""
    _validate_library(library, settings)

    url = await _with_client(
        library, settings, lambda c: c.get_download_url(loan_id, total_tracks), store=store
    )

    # Cache the resolved URL so import can reuse it (avoids MDP rate limits)
    await store._r.setex(f"bb:download_url:{library}:{loan_id}", 600, url)

    return {"library": library, "loan_id": loan_id, "download_url": url}


# ------------------------------------------------------------------
# Import (download from BorrowBox -> upload to Audiobookshelf)
# ------------------------------------------------------------------


@router.post("/import/{library}/{loan_id}")
async def import_to_yoto(
    library: str,
    loan_id: str,
    product_id: str | None = None,
    force: bool = False,
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
) -> dict:
    """Download an audiobook and import it into Audiobookshelf.

    Skips the download/upload when the loan is already present in ABS to avoid
    creating duplicate items. Pass ``force=true`` to import anyway.
    """
    _validate_library(library, settings)
    await report_workflow("Checking Audiobookshelf")

    # Look up BorrowBox product metadata for enrichment
    bb_product: dict | None = None
    total_tracks: int | None = None
    if product_id:
        bb_product = await _get_bb_product(library, product_id, store, settings)
    if not bb_product:
        # Try to find product_id from cached loans
        cached_loans = await store.get_current_loans("default", library)
        if cached_loans:
            for loan in cached_loans:
                if loan.get("loan_id") == loan_id:
                    if loan.get("total_tracks") is not None:
                        total_tracks = int(loan["total_tracks"])
                    pid = loan.get("product_id")
                    if pid:
                        bb_product = await _get_bb_product(library, pid, store, settings)
                        product_id = pid
                    break
    elif (cached_loans := await store.get_current_loans("default", library)):
        for loan in cached_loans:
            if loan.get("loan_id") == loan_id and loan.get("total_tracks") is not None:
                total_tracks = int(loan["total_tracks"])
                break

    # Skip re-import if this loan is already in Audiobookshelf, so repeated
    # imports don't create duplicate ABS items. Pass ?force=true to override.
    if not force:
        existing_id = await _find_existing_abs_item(settings, store, library, product_id, bb_product)
        if existing_id:
            return {
                "status": "already_imported",
                "item_id": existing_id,
                "source": "audiobookshelf",
                "title": _bb_full_title(bb_product) if bb_product else None,
            }

    # Step 1: check cache first, then resolve download URL
    cache_key = f"bb:download_url:{library}:{loan_id}"
    download_url = await store._r.get(cache_key)

    if download_url:
        logger.info("Using cached download URL for %s/%s", library, loan_id)
        download_url = _normalize_download_url(str(download_url))
        if not download_url.startswith(("http://", "https://")):
            logger.warning(
                "Discarding malformed cached download URL for %s/%s: %r",
                library,
                loan_id,
                download_url,
            )
            download_url = None
    else:
        download_url = None

    if not download_url:
        download_url = await _with_client(
            library, settings, lambda c: c.get_download_url(loan_id, total_tracks=total_tracks), store=store
        )
        download_url = _normalize_download_url(str(download_url))
        if not download_url.startswith(("http://", "https://")):
            raise HTTPException(
                status_code=502,
                detail=f"BorrowBox returned an invalid download URL: {download_url!r}",
            )
        await store._r.setex(cache_key, 600, download_url)

    # Step 2: check local ZIP archive, then download if needed
    from_archive = False
    title = _bb_full_title(bb_product) if bb_product else None
    zip_bytes = get_archived_zip(settings.zip_archive_dir, library, loan_id)
    if zip_bytes:
        from_archive = True
    else:
        await report_workflow("Downloading audiobook")
        try:
            async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
                resp = await client.get(download_url)
                resp.raise_for_status()
                zip_bytes = resp.content
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to download zip: {exc}") from exc
        save_zip(settings.zip_archive_dir, library, loan_id, zip_bytes, title=title)

    try:
        await report_workflow("Preparing audiobook")
        staged = await asyncio.to_thread(
            stage_borrowbox_zip_import,
            zip_bytes,
            title=title,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Import failed: {exc}") from exc

    try:
        result = staged.result
        if result.get("status") != "imported":
            return result

        card_id = result["card_id"]
        try:
            payload = staged.load_payload()
            if not payload:
                raise HTTPException(status_code=500, detail="Imported book payload is missing")

            audio_files = staged.audio_files()
            cover_file = staged.cover_file()

            # Prefer BorrowBox catalog author over ID3 tags (often "Unknown Author").
            # Tolerates cached snake_case list[str] and raw camelCase list[{name}].
            author = ", ".join(_bb_author_names(bb_product))
            if not author:
                meta = payload.get("metadata") or {}
                author = (meta.get("author") or "").strip()
                if author == "Unknown Author":
                    author = ""

            upload_title = payload.get("title") or title or "Untitled"

            abs_client = _get_abs_client(settings)
            try:
                abs_library = await abs_client.get_book_library(settings.abs_library_name)
                folders = abs_library.get("folders") or abs_library.get("libraryFolders") or []
                if not folders:
                    raise HTTPException(status_code=500, detail="Audiobookshelf library has no folders")
                # Pass series metadata so ABS can group books correctly.
                # Tolerate both snake_case (cached dataclass asdict) and
                # camelCase (raw BorrowBox JSON) shapes.
                series_name = None
                series_num = None
                if bb_product:
                    series_name = bb_product.get("series_name") or bb_product.get("seriesName")
                    series_num = bb_product.get("series_number")
                    if series_num is None:
                        series_num = bb_product.get("seriesNumber")
                sequence = int(series_num) if series_num is not None else None

                # When a series is passed, prefer the bare book title (without
                # the series prefix) so ABS doesn't store "Tom Gates: Top of
                # the Class (nearly)" inside a "Tom Gates" folder.
                if series_name and bb_product:
                    bare_title = (bb_product.get("title") or "").strip()
                    if bare_title:
                        upload_title = bare_title
                await report_workflow("Uploading to Audiobookshelf", tracks_total=len(audio_files))
                uploaded_item = await abs_client.upload_local_book(
                    library_id=abs_library["id"],
                    folder_id=folders[0]["id"],
                    title=upload_title,
                    author=author,
                    audio_files=audio_files,
                    cover_file=cover_file,
                    series=series_name,
                    sequence=sequence,
                    description=None,
                    auto_match=settings.abs_auto_match,
                    match_min_confidence=settings.abs_match_min_confidence,
                    match_providers=settings.abs_match_providers,
                )
                await report_workflow("Updating shelf")
                await sync_abs_item_to_redis(store, uploaded_item, abs_client)
                # Record a stable loan->ABS link so the loans view can still
                # detect this book after metadata edits change its title/author.
                if product_id and uploaded_item.get("id"):
                    try:
                        await store.set_abs_link(library, product_id, uploaded_item["id"])
                    except Exception:
                        logger.warning("Failed to record ABS link for %s", product_id, exc_info=True)
            finally:
                await abs_client.aclose()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Audiobookshelf upload failed: {exc}") from exc

        # Invalidate the loans cache so the next fetch re-checks ABS presence
        await store._r.delete(f"bb:loans:default:{library}")

        return {
            "status": "imported",
            "title": upload_title,
            "item_id": uploaded_item["id"],
            "source": "audiobookshelf",
            "from_archive": from_archive,
        }
    finally:
        staged.cleanup()

# ------------------------------------------------------------------
# Debug / refresh
# ------------------------------------------------------------------


@router.delete("/cache/loans/{library}")
async def clear_loans_cache(
    library: str,
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
) -> dict:
    """Clear cached loan data for a library so next fetch is fresh."""
    _validate_library(library, settings)
    await store._r.delete(f"bb:loans:default:{library}")
    await store._r.delete(f"bb:loan_history:default:{library}")
    return {"cleared": library}


@router.get("/debug/loans/{library}")
async def debug_loans(
    library: str,
    settings=Depends(_get_settings),
) -> list[dict]:
    """Return raw loan data (for debugging field structure)."""
    raw = await _with_client(
        library, settings, lambda c: c.get_current_loans()
    )
    results = []
    for ln in raw[:3]:
        product = ln.get("product", {})
        results.append({
            "top_keys": list(ln.keys()),
            "id": ln.get("id"),
            "loanType": ln.get("loanType"),
            "productId": product.get("productId"),
            "title": product.get("title"),
            "format": (product.get("formatSpecifics") or {}).get("loanFormat"),
            "totalTracks": (product.get("formatSpecifics") or {}).get("totalTracks"),
        })
    return results


# ------------------------------------------------------------------
# All-library loans (merged)
# ------------------------------------------------------------------


# Limit concurrent BorrowBox logins to avoid WAF rate-limiting.
# Each login creates multiple requests (GET /login + POST /api/v1/auth/login)
# and firing them all in parallel from one IP triggers AWS WAF Bot Control.
_bb_login_sem = asyncio.Semaphore(2)


@router.get("/all-loans")
async def get_all_loans(
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
) -> dict[str, list[LoanInfo]]:
    """Get current loans from ALL configured libraries, keyed by library name."""
    result: dict[str, list[LoanInfo]] = {}

    async def _fetch_library(lib_name: str) -> None:
        try:
            cached = await store.get_current_loans("default", lib_name)
            if cached is not None:
                result[lib_name] = [LoanInfo(**loan) for loan in cached]
                return

            async with _bb_login_sem:
                raw_loans = await _with_client(
                    lib_name, settings, lambda c: c.get_current_loans(), store=store
                )
            loans = []
            for ln in raw_loans:
                product = ln.get("product", ln)
                loans.append(_normalize_loan(ln, product))

            loans = await _mark_loans_present_in_abs(loans, settings, store=store, library=lib_name)
            await store.set_current_loans("default", lib_name, [l.model_dump() for l in loans])
            result[lib_name] = loans
        except Exception as exc:
            logger.warning("Failed to fetch loans for %s: %s", lib_name, exc)
            result[lib_name] = []

    await asyncio.gather(*[_fetch_library(lib) for lib in settings.libraries])
    return result


@router.get("/all-history")
async def get_all_history(
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
) -> dict[str, list[LoanInfo]]:
    """Get loan history from ALL configured libraries, keyed by library name."""
    result: dict[str, list[LoanInfo]] = {}

    async def _fetch_library(lib_name: str) -> None:
        try:
            cached = await store.get_loan_history("default", lib_name)
            if cached is not None:
                result[lib_name] = [LoanInfo(**loan) for loan in cached]
                return

            async with _bb_login_sem:
                raw_loans = await _with_client(
                    lib_name, settings, lambda c: c.get_loan_history(), store=store
                )
            loans = []
            for ln in raw_loans:
                product = ln.get("product", ln)
                loans.append(_normalize_loan(ln, product))

            await store.set_loan_history("default", lib_name, [l.model_dump() for l in loans])
            result[lib_name] = loans
        except Exception as exc:
            logger.warning("Failed to fetch history for %s: %s", lib_name, exc)
            result[lib_name] = []

    await asyncio.gather(*[_fetch_library(lib) for lib in settings.libraries])
    return result


# ------------------------------------------------------------------
# ZIP Archives
# ------------------------------------------------------------------


@router.get("/archives")
async def get_archives(
    settings=Depends(_get_settings),
) -> list[dict]:
    """List all archived ZIP files across all libraries."""
    archives = list_archives(settings.zip_archive_dir)

    # Check ABS presence for archives
    if settings.abs_token:
        abs_client = _get_abs_client(settings)
        try:
            abs_library = await abs_client.get_book_library(settings.abs_library_name)
            all_items = await abs_client.list_library_items(abs_library["id"])
            for arc in archives:
                title = arc.get("title") or ""
                if not title:
                    continue
                sanitized_title = _abs_sanitize_filename(title)
                for item in all_items:
                    item_path = item.get("path") or ""
                    metadata = ((item.get("media") or {}).get("metadata") or {})
                    item_title = _normalize_match_text(str(metadata.get("title") or ""))
                    arc_title = _normalize_match_text(title)
                    if (
                        (item_title and arc_title and _titles_loosely_match(arc_title, item_title))
                        or (sanitized_title and (item_path.endswith("/" + sanitized_title) or item_path.endswith("\\" + sanitized_title)))
                    ):
                        arc["in_abs"] = True
                        arc["abs_item_id"] = item.get("id")
                        break
                else:
                    arc["in_abs"] = False
        finally:
            await abs_client.aclose()

    return archives
