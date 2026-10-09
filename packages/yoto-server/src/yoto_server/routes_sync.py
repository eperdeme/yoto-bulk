"""Sync, import, and maintenance routes for yoto-server."""

from __future__ import annotations

import hashlib
import logging
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from yoto_bulk.workflows import report_workflow

from yoto_bulk.abs_series import (
    apply_llm_grouping,
    build_series_grouping_prompt,
    group_cards_by_series,
)
from yoto_bulk.audiobookshelf import (
    build_yoto_payload_from_abs,
    build_yoto_payload_from_abs_podcast,
)
from yoto_bulk.auth import YotoAuthService
from yoto_bulk.card_utils import (
    ensure_metadata,
    resolve_sequence_number,
    resolve_title,
    truncate_for_api,
)
from yoto_bulk.config import Settings as YotoBulkSettings
from yoto_bulk.llm import LlmService
from yoto_bulk.yoto_client import YotoClient
from yoto_server.deps import get_abs_client, get_store, get_user_id
from yoto_server.reconcile import reconcile_abs_yoto_links
from yoto_server.schemas import (
    BulkSeriesApplyResponse,
    BulkSeriesUpdate,
    CardMetadataUpdate,
    ManualSeriesRequest,
    SeriesCatalogInfo,
    SeriesCatalogItem,
    SeriesDiscoveryResponse,
    SeriesSuggestion,
)
from yoto_server.store import YotoStore

router = APIRouter()
logger = logging.getLogger(__name__)

_ABS_SERIES_CACHE_TTL_SECONDS = 3600
_PROVIDER_SERIES_CACHE_HIT_TTL_SECONDS = 604800
# Keep the negative-cache window short: the ABS -> Audible provider search
# (proxied via Cloudflare) is intermittently flaky and can return an empty
# result for a series that actually has matches. A long miss TTL turns one bad
# moment into a library-wide "no match" that persists for a day, so cap empties
# at an hour and let them self-heal on the next load.
_PROVIDER_SERIES_CACHE_MISS_TTL_SECONDS = 3600
_DEFAULT_SERIES_PROVIDERS = ("audible.uk", "audible")


def _podcast_item_version(item: dict[str, Any]) -> str:
    media = item.get("media") or {}
    snapshot = {
        "updatedAt": item.get("updatedAt"),
        "metadata": media.get("metadata") or {},
        "episodes": media.get("episodes") or [],
    }
    raw = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _normalize_series_lookup(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _split_abs_people(value: Any) -> list[str]:
    if isinstance(value, list):
        people: list[str] = []
        for item in value:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
            else:
                name = str(item or "").strip()
            if name:
                people.append(name)
        return people
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _normalize_abs_sequence(value: Any) -> float | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if isinstance(value, str):
        cleaned = value.strip()
        match = re.search(r"\.\d+|\d+(?:\.\d+)?", cleaned)
        if match:
            value = float(match.group(0))
            return int(value) if value.is_integer() else value
    return None


def _abs_book_payload(book: dict[str, Any]) -> dict[str, Any]:
    candidate = book.get("libraryItem") or book.get("book") or book.get("item") or book
    return candidate if isinstance(candidate, dict) else book


def _abs_series_item_from_book(book: dict[str, Any], *, series_id: str, series_name: str) -> SeriesCatalogItem | None:
    item = _abs_book_payload(book)
    media = item.get("media") or book.get("media") or {}
    metadata = media.get("metadata") or item.get("metadata") or book.get("metadata") or {}

    title = str(
        metadata.get("title")
        or media.get("title")
        or item.get("title")
        or book.get("title")
        or ""
    ).strip()
    if not title:
        return None

    series_entries = metadata.get("series") or []
    first_series = series_entries[0] if series_entries else {}
    if not isinstance(first_series, dict):
        first_series = {}

    item_series_name = str(first_series.get("name") or series_name).strip() or series_name
    sequence = _normalize_abs_sequence(
        first_series.get("sequence")
        or book.get("sequence")
        or item.get("sequence")
        or metadata.get("seriesSequence")
    )
    authors = _split_abs_people(metadata.get("authors")) or _split_abs_people(metadata.get("author"))
    item_id = str(item.get("id") or book.get("id") or book.get("libraryItemId") or "").strip() or None

    return SeriesCatalogItem(
        title=title,
        authors=authors,
        series_name=item_series_name,
        series_number=sequence,
        series_id=series_id,
        source="abs",
        abs_item_id=item_id,
        cover_url=metadata.get("coverPath") or media.get("coverPath") or item.get("coverPath"),
    )


def _abs_series_books(entry: dict[str, Any]) -> list[dict[str, Any]]:
    raw = entry.get("books") or entry.get("items") or entry.get("libraryItems") or []
    if isinstance(raw, dict):
        raw = raw.get("results") or raw.get("books") or raw.get("items") or []
    return [item for item in raw if isinstance(item, dict)]


def _abs_series_info_from_entry(entry: dict[str, Any]) -> SeriesCatalogInfo | None:
    name = str(entry.get("name") or "").strip()
    if not name:
        return None
    series_id = str(entry.get("id") or entry.get("seriesId") or name).strip() or name
    items = [
        item
        for item in (
            _abs_series_item_from_book(book, series_id=series_id, series_name=name)
            for book in _abs_series_books(entry)
        )
        if item is not None
    ]
    return SeriesCatalogInfo(series_id=series_id, name=name, source="abs", items=items)


def _find_abs_series(catalog: list[SeriesCatalogInfo], name: str) -> SeriesCatalogInfo | None:
    needle = _normalize_series_lookup(name)
    if not needle:
        return None

    exact = [entry for entry in catalog if _normalize_series_lookup(entry.name) == needle]
    if exact:
        return exact[0]

    partial = [
        entry for entry in catalog
        if needle in _normalize_series_lookup(entry.name) or _normalize_series_lookup(entry.name) in needle
    ]
    return partial[0] if partial else None


def _provider_series_entries(result: dict[str, Any]) -> list[dict[str, Any]]:
    raw = result.get("series") or []
    if isinstance(raw, dict):
        raw = [raw]
    return [entry for entry in raw if isinstance(entry, dict)]


def _provider_series_name(entry: dict[str, Any]) -> str:
    return str(entry.get("series") or entry.get("name") or entry.get("seriesName") or "").strip()


def _matching_provider_series_entry(result: dict[str, Any], name: str) -> dict[str, Any] | None:
    entries = _provider_series_entries(result)
    needle = _normalize_series_lookup(name)
    if not needle:
        return None

    for entry in entries:
        if _normalize_series_lookup(_provider_series_name(entry)) == needle:
            return entry
    for entry in entries:
        candidate = _normalize_series_lookup(_provider_series_name(entry))
        if candidate and (needle in candidate or candidate in needle):
            return entry
    return None


def _provider_series_item_from_result(
    result: dict[str, Any],
    *,
    requested_name: str,
    provider: str,
) -> SeriesCatalogItem | None:
    entry = _matching_provider_series_entry(result, requested_name)
    if entry is None:
        return None

    title = str(result.get("title") or "").strip()
    if not title:
        return None

    series_name = _provider_series_name(entry) or requested_name
    provider_id = str(result.get("asin") or result.get("id") or result.get("isbn") or "").strip() or None
    return SeriesCatalogItem(
        title=title,
        authors=_split_abs_people(result.get("author")),
        series_name=series_name,
        series_number=_normalize_abs_sequence(entry.get("sequence") or entry.get("position")),
        series_id=f"provider:{_normalize_series_lookup(series_name)}",
        source="abs_provider",
        provider=provider,
        provider_id=provider_id,
        format="audiobook" if provider.startswith("audible") else None,
        cover_url=result.get("cover"),
    )


def _dedupe_series_catalog_items(items: list[SeriesCatalogItem]) -> list[SeriesCatalogItem]:
    by_title: dict[str, SeriesCatalogItem] = {}
    sequence_to_title: dict[float, str] = {}
    for item in items:
        key = _normalize_series_lookup(item.title)
        if not key:
            continue
        if item.series_number is not None:
            sequence_key = float(item.series_number)
            key = sequence_to_title.setdefault(sequence_key, key)
        existing = by_title.get(key)
        if existing is None:
            by_title[key] = item
            continue
        if existing.series_number is None and item.series_number is not None:
            existing.series_number = item.series_number
        if not existing.cover_url and item.cover_url:
            existing.cover_url = item.cover_url
        if not existing.provider_id and item.provider_id:
            existing.provider_id = item.provider_id
        if not existing.provider and item.provider:
            existing.provider = item.provider
        if not existing.authors and item.authors:
            existing.authors = item.authors
    return sorted(
        by_title.values(),
        key=lambda item: (
            item.series_number if item.series_number is not None else 9999,
            item.title.lower(),
        ),
    )


def _series_catalog_from_provider_results(
    results: list[dict[str, Any]],
    *,
    requested_name: str,
    provider: str,
) -> SeriesCatalogInfo:
    items = _dedupe_series_catalog_items([
        item
        for item in (
            _provider_series_item_from_result(result, requested_name=requested_name, provider=provider)
            for result in results
        )
        if item is not None
    ])
    name = items[0].series_name if items and items[0].series_name else requested_name
    return SeriesCatalogInfo(
        series_id=f"provider:{_normalize_series_lookup(name)}" if items else "unknown",
        name=name,
        source="abs_provider",
        items=items,
    )


async def _load_abs_series_catalog(request: Request, store: YotoStore) -> list[SeriesCatalogInfo]:
    settings = request.app.state.settings
    abs_client = get_abs_client(request)
    try:
        library = await abs_client.get_book_library(settings.abs_library_name)
        library_id = library["id"]
        cache_key = f"abs:series_catalog:v1:{library_id}"
        cached_raw = await store._r.get(cache_key)
        if cached_raw:
            return [SeriesCatalogInfo(**entry) for entry in json.loads(cached_raw)]

        raw_series = await abs_client.list_library_series(library_id)
        catalog = [
            info
            for info in (_abs_series_info_from_entry(entry) for entry in raw_series)
            if info is not None
        ]
        await store._r.set(
            cache_key,
            json.dumps([entry.model_dump() for entry in catalog], ensure_ascii=False),
            ex=_ABS_SERIES_CACHE_TTL_SECONDS,
        )
        return catalog
    finally:
        await abs_client.aclose()


async def _clear_abs_series_catalog_cache(store: YotoStore) -> None:
    scan_iter = getattr(store._r, "scan_iter", None)
    delete = getattr(store._r, "delete", None)
    if not callable(scan_iter) or not callable(delete):
        return
    keys = [key async for key in scan_iter("abs:series_catalog:v1:*")]
    if keys:
        await delete(*keys)


async def _load_provider_series_catalog(
    request: Request,
    store: YotoStore,
    *,
    name: str,
    author: str | None = None,
    provider: str | None = None,
) -> SeriesCatalogInfo:
    normalized_name = _normalize_series_lookup(name)
    normalized_author = _normalize_series_lookup(author)
    provider_key = provider or "+".join(_DEFAULT_SERIES_PROVIDERS)
    cache_key = f"abs:provider_series:v1:{provider_key}:{normalized_name}:{normalized_author}"
    cached_raw = await store._r.get(cache_key)
    if cached_raw:
        return SeriesCatalogInfo(**json.loads(cached_raw))

    providers = (provider,) if provider else _DEFAULT_SERIES_PROVIDERS
    all_items: list[SeriesCatalogItem] = []
    all_providers_succeeded = True
    abs_client = get_abs_client(request)
    try:
        for provider_name in providers:
            try:
                raw_results = await abs_client.search_metadata_books(
                    title=name,
                    author=author,
                    provider=provider_name,
                )
            except Exception as exc:  # noqa: BLE001 — provider errors should not break the Library tab
                logger.warning("ABS provider search failed for %s via %s: %s", name, provider_name, exc)
                all_providers_succeeded = False
                continue
            provider_catalog = _series_catalog_from_provider_results(
                raw_results,
                requested_name=name,
                provider=provider_name,
            )
            all_items.extend(provider_catalog.items)
    finally:
        await abs_client.aclose()

    items = _dedupe_series_catalog_items(all_items)
    resolved_name = items[0].series_name if items and items[0].series_name else name
    result = SeriesCatalogInfo(
        series_id=f"provider:{_normalize_series_lookup(resolved_name)}" if items else "unknown",
        name=resolved_name,
        source="abs_provider",
        items=items,
    )
    # Only persist an empty result when every provider search actually
    # succeeded (a genuine "no match"). If a provider call failed — e.g. a
    # transient ABS/Cloudflare 403 or network blip — skip caching so one bad
    # moment doesn't poison the Library view with a "no match" for a full day.
    if items:
        await store._r.set(cache_key, result.model_dump_json(), ex=_PROVIDER_SERIES_CACHE_HIT_TTL_SECONDS)
    elif all_providers_succeeded:
        await store._r.set(cache_key, result.model_dump_json(), ex=_PROVIDER_SERIES_CACHE_MISS_TTL_SECONDS)
    return result


def _is_linked_yoto_shadow(
    card_id: str,
    meta: dict[str, str] | None,
    known_card_ids: set[str] | None = None,
) -> bool:
    meta = meta or {}
    if meta.get("source") != "yoto":
        return False
    linked_abs_card_id = meta.get("linked_abs_card_id")
    if not linked_abs_card_id or linked_abs_card_id == card_id:
        return False
    return known_card_ids is None or linked_abs_card_id in known_card_ids


@router.get("/series/catalog")
async def abs_series_catalog(
    request: Request,
    store: YotoStore = Depends(get_store),
) -> list[SeriesCatalogInfo]:
    """Return the ABS-known series catalog for the configured book library."""
    return await _load_abs_series_catalog(request, store)


@router.get("/series/lookup")
async def abs_series_lookup(
    name: str,
    request: Request,
    store: YotoStore = Depends(get_store),
) -> SeriesCatalogInfo:
    """Look up one ABS-known series by name."""
    catalog = await _load_abs_series_catalog(request, store)
    match = _find_abs_series(catalog, name)
    if match is not None:
        return match
    return SeriesCatalogInfo(series_id="unknown", name=name, source="abs", items=[])


@router.get("/series/completeness")
async def series_completeness(
    name: str,
    request: Request,
    author: str | None = None,
    provider: str | None = None,
    store: YotoStore = Depends(get_store),
) -> SeriesCatalogInfo:
    """Return expected series titles from ABS metadata providers, with ABS-library fallback."""
    provider_catalog = await _load_provider_series_catalog(
        request,
        store,
        name=name,
        author=author,
        provider=provider,
    )
    if provider_catalog.items:
        return provider_catalog

    catalog = await _load_abs_series_catalog(request, store)
    match = _find_abs_series(catalog, name)
    if match is not None:
        return match
    return provider_catalog


@router.post("/migrate")
async def sync_from_audiobookshelf(
    request: Request,
    store: YotoStore = Depends(get_store),
    user_id: str = Depends(get_user_id),
) -> dict[str, Any]:
    """Sync ABS library items into Redis for the Yoto UI."""
    abs_client = get_abs_client(request)
    try:
        await report_workflow("Reading Audiobookshelf")
        library = await abs_client.get_book_library(request.app.state.settings.abs_library_name)
        book_items = await abs_client.list_library_items(library["id"])
        podcast_libraries = await abs_client.get_podcast_libraries()
        podcast_items: list[dict[str, Any]] = []
        for podcast_library in podcast_libraries:
            podcast_items.extend(await abs_client.list_library_items(podcast_library["id"]))
        items = book_items + podcast_items
        podcast_item_ids = {
            str(item.get("id")) for item in podcast_items if item.get("id")
        }

        all_meta = await store.get_all_meta(user_id)
        abs_meta = {
            meta["external_id"]: meta
            for meta in all_meta.values()
            if meta.get("source") == "abs" and "external_id" in meta
        }

        # ABS is the source of truth: drop cached ABS cards whose library item
        # no longer exists (e.g. deleted duplicates) so the UI stops showing
        # orphans that aren't in Audiobookshelf. Guard against an empty listing
        # (likely a transient error) so we never wipe the whole cache.
        live_ids = {item.get("id") for item in items if item.get("id")}
        pruned_ids: list[str] = []
        if items:
            for card_id, meta in all_meta.items():
                if meta.get("source") != "abs":
                    continue
                external_id = meta.get("external_id")
                if external_id and external_id not in live_ids:
                    pruned_ids.append(card_id)
            for card_id in pruned_ids:
                await store.remove_card(user_id, card_id)
            if pruned_ids:
                logger.info("Pruned %d orphaned ABS cards for user %s", len(pruned_ids), user_id)

        payloads: list[tuple[str, dict[str, Any], str | int | None]] = []
        unchanged_count = 0
        fetch_failed_count = 0
        no_audio_count = 0

        for item in items:
            item_id = str(item.get("id") or "").strip()
            if not item_id:
                continue

            updated_at = item.get("updatedAt")
            existing = abs_meta.get(item_id)
            is_podcast = item_id in podcast_item_ids
            if not is_podcast and existing and str(existing.get("external_updated_at")) == str(updated_at):
                unchanged_count += 1
                continue

            try:
                detail = await abs_client.get_item(item_id)
                if is_podcast:
                    updated_at = _podcast_item_version(detail)
                    if existing and str(existing.get("external_updated_at")) == updated_at:
                        unchanged_count += 1
                        continue
                    payload = build_yoto_payload_from_abs_podcast(detail)
                    if not payload["content"]["chapters"]:
                        no_audio_count += 1
                        continue
                else:
                    playback = await abs_client.get_playback_session(item_id)
                    payload = build_yoto_payload_from_abs(detail, playback)
                payloads.append((item_id, payload, updated_at))
            except Exception as exc:
                fetch_failed_count += 1
                logger.error("Failed to fetch details for ABS item %s: %s", item_id, exc)

        result = (
            await store.import_from_audiobookshelf(user_id, payloads)
            if payloads
            else {
                "user_id": user_id,
                "imported": 0,
                "updated": 0,
                "skipped": 0,
                "conflicted": 0,
                "total": 0,
            }
        )
        await report_workflow("Reconciling library")

        # Reconcile unconditionally. Linking ABS cards to pulled Yoto cards has
        # nothing to do with whether ABS content changed, but this used to sit
        # behind an early return taken whenever there was nothing to import — so
        # on a steady-state library (the common case) duplicates were never
        # linked, no matter how many times you synced.
        reconcile_result = await reconcile_abs_yoto_links(store, user_id)
        result["skipped"] += unchanged_count + fetch_failed_count + no_audio_count
        result["total"] = len(items)
        result["unchanged"] = unchanged_count
        result["fetch_failed"] = fetch_failed_count
        result["no_audio"] = no_audio_count
        result["linked"] = reconcile_result["linked"]
        result["ambiguous"] = reconcile_result["ambiguous"]
        result["pruned"] = len(pruned_ids)

        from datetime import UTC, datetime

        completed_at = datetime.now(UTC).isoformat()
        result["status"] = "partial" if fetch_failed_count or result["conflicted"] else "synced"
        result["completed_at"] = completed_at
        timestamps = {
            "last_abs_attempt": completed_at,
            "last_abs_sync_status": result["status"],
        }
        if result["status"] == "synced":
            timestamps["last_abs_sync"] = completed_at
        await store._r.hset(f"ui:sync:{user_id}", mapping=timestamps)

        # Log the whole picture. The previous line showed only four of these
        # counters, so a sync that fetched several items and then discarded them
        # all as unchanged looked identical to one that did nothing, with no way
        # to tell which from the logs.
        logger.info(
            "Audiobookshelf sync complete for user %s: %d imported, %d updated, "
            "%d conflicted, %d pruned, %d linked, %d ambiguous "
            "(%d items: %d unchanged, %d fetched, %d unchanged after fetch, "
            "%d without downloaded audio, %d fetch failed)",
            user_id,
            result["imported"],
            result["updated"],
            result["conflicted"],
            result["pruned"],
            result["linked"],
            result["ambiguous"],
            result["total"],
            unchanged_count,
            len(payloads),
            result["skipped"] - unchanged_count - fetch_failed_count - no_audio_count,
            no_audio_count,
            fetch_failed_count,
        )
        return result
    finally:
        await abs_client.aclose()


@router.post("/pull")
async def pull_from_yoto(
    store: YotoStore = Depends(get_store),
    user_id: str = Depends(get_user_id),
) -> dict[str, Any]:
    """Fetch the live library from Yoto API and sync into Redis."""
    yoto_settings = YotoBulkSettings()
    auth_service = YotoAuthService(yoto_settings)
    tokens = auth_service.token_store.load()
    if tokens is None:
        raise HTTPException(status_code=401, detail="Not authenticated with Yoto")

    yoto_client = YotoClient(yoto_settings, auth_service)
    books = await yoto_client.list_library_books()

    imported = 0
    updated = 0
    skipped = 0

    for book in books:
        card_id = book.id
        try:
            payload = await yoto_client.get_content_card(card_id)
        except Exception as exc:
            logger.error("Failed to fetch card %s from Yoto: %s", card_id, exc)
            continue

        existing_meta = await store.get_meta(user_id, card_id)
        if existing_meta.get("source") == "abs":
            skipped += 1
            continue

        await store.save_card(
            user_id=user_id,
            card_id=card_id,
            payload=payload,
            update_pull_hash=True,
            source="yoto",
        )

        if existing_meta:
            updated += 1
        else:
            imported += 1

    reconcile_result = await reconcile_abs_yoto_links(store, user_id)
    return {
        "imported": imported,
        "updated": updated,
        "skipped": skipped,
        "total": len(books),
        "linked": reconcile_result["linked"],
        "ambiguous": reconcile_result["ambiguous"],
    }


@router.post("/cards/{card_id}/import-to-abs")
async def import_yoto_to_abs(
    card_id: str,
    request: Request,
    store: YotoStore = Depends(get_store),
    user_id: str = Depends(get_user_id),
) -> dict[str, Any]:
    """Download a card from Yoto and upload it to Audiobookshelf."""
    payload = await store.load_card(user_id, card_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Card not found")

    yoto_settings = YotoBulkSettings()
    auth_service = YotoAuthService(yoto_settings)
    yoto_client = YotoClient(yoto_settings, auth_service)

    playable = await yoto_client.get_content_card(card_id, playable=True)
    chapters = (playable.get("content") or {}).get("chapters") or []
    if not chapters:
        raise HTTPException(status_code=400, detail="Card has no audio tracks")

    temp_dir = Path(tempfile.mkdtemp(prefix=f"yoto-to-abs-{card_id}-"))
    audio_files: list[Path] = []

    try:
        for idx, chapter in enumerate(chapters):
            track = chapter.get("tracks", [{}])[0]
            url = track.get("trackUrl")
            if not url:
                continue

            title = chapter.get("title") or f"Track {idx + 1}"
            safe_title = "".join(c for c in title if c.isalnum() or c in (" ", ".", "_")).strip()
            dest = temp_dir / f"{idx + 1:02d} - {safe_title}.mp3"

            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    with open(dest, "wb") as f:
                        async for chunk in resp.aiter_bytes():
                            f.write(chunk)
            audio_files.append(dest)

        cover_url = ((playable.get("metadata") or {}).get("cover") or {}).get("imageL")
        cover_file = None
        if cover_url:
            cover_dest = temp_dir / "cover.jpg"
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(cover_url)
                if resp.status_code == 200:
                    cover_dest.write_bytes(resp.content)
                    cover_file = cover_dest

        abs_client = get_abs_client(request)
        try:
            settings = request.app.state.settings
            abs_library = await abs_client.get_book_library(settings.abs_library_name)
            folders = abs_library.get("folders") or abs_library.get("libraryFolders") or []
            if not folders:
                raise HTTPException(status_code=500, detail="Audiobookshelf library has no folders")

            meta = playable.get("metadata") or {}
            uploaded_item = await abs_client.upload_local_book(
                library_id=abs_library["id"],
                folder_id=folders[0]["id"],
                title=resolve_title(playable),
                author=meta.get("author") or "Unknown Author",
                audio_files=audio_files,
                cover_file=cover_file,
                series=meta.get("series"),
                sequence=meta.get("sequence_number"),
                description=meta.get("description"),
            )

            refreshed_payload = build_yoto_payload_from_abs(
                uploaded_item,
                await abs_client.get_playback_session(uploaded_item["id"]),
            )
            await store.save_card(
                user_id=user_id,
                card_id=card_id,
                payload=refreshed_payload,
                update_pull_hash=True,
                source="abs",
                external_id=uploaded_item["id"],
            )
            await store._r.hset(
                store._meta_key(user_id, card_id),
                mapping={"yoto_card_id": card_id},
            )

            return {
                "status": "imported",
                "abs_id": uploaded_item["id"],
                "title": resolve_title(playable),
            }
        finally:
            await abs_client.aclose()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@router.patch("/cards/{card_id}/metadata")
async def update_card_metadata(
    card_id: str,
    update: CardMetadataUpdate,
    request: Request,
    store: YotoStore = Depends(get_store),
    user_id: str = Depends(get_user_id),
) -> dict[str, Any]:
    """Patch metadata on a card.

    For ABS-backed cards, routes the change through Audiobookshelf (source
    of truth) and refreshes the Redis cache. For native Yoto cards,
    pushes the update directly to the Yoto API to ensure it persists.
    """
    meta = await store.get_meta(user_id, card_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Card not found")

    if meta.get("source") != "abs":
        if _is_linked_yoto_shadow(card_id, meta):
            raise HTTPException(
                status_code=409,
                detail="This Yoto card is linked to an ABS-backed card. Edit the ABS-backed card instead.",
            )

        # Handle native Yoto card update
        try:
            yoto_settings = YotoBulkSettings()
            yoto_client = YotoClient(yoto_settings, YotoAuthService(yoto_settings))

            payload = await store.load_card(user_id, card_id)
            if not payload:
                raise HTTPException(status_code=404, detail="Card payload not found")

            metadata = ensure_metadata(payload)
            if update.title is not None:
                metadata["title"] = update.title
            if update.author is not None:
                metadata["author"] = update.author
            if update.description is not None:
                metadata["description"] = update.description
            elif "description" in update.model_fields_set:
                metadata.pop("description", None)
            if update.series is not None and update.series.strip():
                metadata["series"] = update.series
            elif "series" in update.model_fields_set:
                metadata.pop("series", None)
            if update.sequence_number is not None:
                metadata["sequence_number"] = update.sequence_number
            elif "sequence_number" in update.model_fields_set:
                metadata.pop("sequence_number", None)

            truncate_for_api(payload)
            await yoto_client.save_content(payload)
            await store.save_card(user_id, card_id, payload, update_pull_hash=True)
            return {"status": "updated", "card_id": card_id}
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Failed to update Yoto card %s: %s", card_id, exc)
            raise HTTPException(status_code=500, detail=f"Update failed: {exc}")

    external_id = meta.get("external_id")
    if not external_id:
        raise HTTPException(status_code=400, detail="ABS-backed card is missing external_id")

    abs_client = get_abs_client(request)
    try:
        item = await abs_client.update_item_metadata(
            external_id,
            title=update.title,
            author=update.author,
            description=update.description,
            series=update.series,
            sequence=update.sequence_number,
        )
        playback = await abs_client.get_playback_session(external_id)
        refreshed = build_yoto_payload_from_abs(item, playback)
        await store.save_card(user_id, card_id, refreshed, update_pull_hash=True)
        await _clear_abs_series_catalog_cache(store)
        return {"status": "updated", "card_id": card_id}
    finally:
        await abs_client.aclose()


@router.post("/series/apply")
async def apply_series_suggestions(
    request: Request,
    update: BulkSeriesUpdate,
    store: YotoStore = Depends(get_store),
    user_id: str = Depends(get_user_id),
) -> BulkSeriesApplyResponse:
    """Apply discovered series metadata and refresh Redis.

    ABS-backed cards are updated through Audiobookshelf, which remains the
    canonical metadata source. Native Yoto cards have no ABS authority, so
    their metadata is written directly to Yoto and then cached.
    """
    abs_client = None
    applied_count = 0
    skipped_yoto: list[str] = []
    skipped_missing_external: list[str] = []
    failed: list[str] = []

    try:
        all_meta = await store.get_all_meta(user_id)
        known_card_ids = set(all_meta)

        for suggestion in update.suggestions:
            for card_id in suggestion.card_ids:
                meta = all_meta.get(card_id)
                if not meta:
                    failed.append(card_id)
                    continue
                if _is_linked_yoto_shadow(card_id, meta, known_card_ids):
                    skipped_yoto.append(card_id)
                    continue
                sequence = suggestion.sequence_numbers.get(card_id)

                if meta.get("source") != "abs":
                    # Native Yoto card — push to Yoto to persist change
                    try:
                        yoto_settings = YotoBulkSettings()
                        yoto_client = YotoClient(yoto_settings, YotoAuthService(yoto_settings))

                        payload = await store.load_card(user_id, card_id)
                        if not payload:
                            failed.append(card_id)
                            continue

                        metadata = ensure_metadata(payload)
                        metadata["series"] = suggestion.series_name
                        if sequence is None:
                            metadata.pop("sequence_number", None)
                        else:
                            metadata["sequence_number"] = sequence

                        truncate_for_api(payload)
                        await yoto_client.save_content(payload)
                        await store.save_card(user_id, card_id, payload, update_pull_hash=True)
                        applied_count += 1
                        continue
                    except Exception as exc:
                        logger.error("Failed to apply series to Yoto card %s: %s", card_id, exc)
                        failed.append(card_id)
                        continue

                external_id = meta.get("external_id")
                if not external_id:
                    skipped_missing_external.append(card_id)
                    continue

                try:
                    if abs_client is None:
                        abs_client = get_abs_client(request)
                    item = await abs_client.update_item_metadata(
                        external_id,
                        series=suggestion.series_name,
                        sequence=sequence,
                    )
                    playback = await abs_client.get_playback_session(external_id)
                    refreshed = build_yoto_payload_from_abs(item, playback)
                    await store.save_card(user_id, card_id, refreshed, update_pull_hash=True)
                    applied_count += 1
                except Exception as exc:
                    logger.error("Failed to apply series to ABS item %s: %s", external_id, exc)
                    failed.append(card_id)

        if applied_count:
            await _clear_abs_series_catalog_cache(store)

        return BulkSeriesApplyResponse(
            status="applied",
            count=applied_count,
            skipped_yoto_source=skipped_yoto,
            skipped_missing_external=skipped_missing_external,
            failed=failed,
        )
    finally:
        if abs_client is not None:
            await abs_client.aclose()


async def _load_active_cards(
    store: YotoStore,
    user_id: str,
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, dict[str, str]]]:
    """Load cached cards minus linked Yoto shadows, plus their meta."""
    cards = await store.load_all_cards(user_id)
    meta_by_card = await store.get_all_meta(user_id)
    known_card_ids = {card_id for card_id, _payload in cards}
    active = [
        (card_id, payload)
        for card_id, payload in cards
        if not _is_linked_yoto_shadow(card_id, meta_by_card.get(card_id), known_card_ids)
    ]
    return active, meta_by_card


def _parse_manual_grouping(text: str) -> dict[str, Any]:
    """Parse the JSON a user pasted back from an external LLM tool.

    Tolerates markdown code fences and surrounding prose by falling back to the
    first ``{...}`` block. Raises ``HTTPException(400)`` if nothing parses.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned.strip())
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise HTTPException(status_code=400, detail="Pasted response is not valid JSON.")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Could not parse pasted response as JSON: {exc}",
            ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=400,
            detail="Pasted response must be a JSON object with a 'series' key.",
        )
    return parsed


@router.post("/series/discover")
async def discover_series(
    request: Request,
    store: YotoStore = Depends(get_store),
    user_id: str = Depends(get_user_id),
    abs_match: bool = False,
) -> SeriesDiscoveryResponse:
    """Discover series groupings, ABS-first with LLM fallback.

    Strategy:

    1. Group ABS-backed cards using their cached ABS series metadata
       (Audiobookshelf is the upstream authority — if it already says
       "His Dark Materials" we trust it without calling OpenAI).
    2. Optionally run ABS quick-match against the residue of ABS-backed
       cards that have no series, to let ABS pull canonical metadata from
       Audible / Google before resorting to the LLM. Enable with
       ``?abs_match=true``; off by default because it can be slow.
    3. For everything still ungrouped (Yoto-only cards or ABS items ABS
       could not resolve), call OpenAI to suggest groupings.

    The response tags each suggestion with ``provenance`` (``"abs"`` or
    ``"llm"``) so the UI can show how each grouping was sourced.

    If the automatic LLM step is unavailable or fails (e.g. no OpenAI access),
    discovery degrades to ABS-only grouping and returns ``manual_prompt`` — a
    self-contained prompt the user can paste into any LLM tool. The response
    from that tool is submitted to ``POST /series/discover/manual``.
    """
    settings = request.app.state.settings

    cards, meta_by_card = await _load_active_cards(store, user_id)
    if not cards:
        return SeriesDiscoveryResponse(
            series=[], unmatched_card_ids=[], abs_group_count=0, llm_group_count=0,
            used_llm=False,
        )

    llm_service: LlmService | None = None
    if settings.openai_api_key:
        try:
            llm_service = LlmService(settings)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LlmService init failed; ABS-only grouping: %s", exc)
            llm_service = None

    abs_client = get_abs_client(request) if abs_match else None
    library_id: str | None = None
    if abs_match and abs_client is not None:
        try:
            library = await abs_client.get_book_library(settings.abs_library_name)
            library_id = library.get("id")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not resolve ABS library for quick-match: %s", exc)
            library_id = None

    try:
        try:
            result = await group_cards_by_series(
                cards,
                meta_by_card,
                llm_service=llm_service,
                abs_client=abs_client,
                library_id=library_id,
                quick_match_missing=bool(abs_match and abs_client and library_id),
            )
        except Exception as exc:  # noqa: BLE001
            # LLM-assisted grouping failed (e.g. no OpenAI access). Fall back to
            # deterministic ABS grouping and hand the residue back as a
            # copy-paste prompt the user can run in their own LLM tool.
            logger.warning(
                "LLM-assisted series grouping failed (%s); using ABS-only "
                "grouping with a manual prompt fallback",
                exc,
            )
            result = await group_cards_by_series(cards, meta_by_card, llm_service=None)
    finally:
        if abs_client is not None:
            await abs_client.aclose()

    payload_by_card = dict(cards)
    suggestions = [
        SeriesSuggestion(
            series_name=group.series_name,
            card_ids=group.card_ids,
            sequence_numbers=_sequence_numbers_for_group(group.card_ids, payload_by_card),
            provenance=group.provenance,
        )
        for group in result.groups
    ]

    manual_prompt: str | None = None
    if result.unmatched_card_ids:
        residue = [
            (card_id, payload_by_card[card_id])
            for card_id in result.unmatched_card_ids
            if card_id in payload_by_card
        ]
        if residue:
            manual_prompt = build_series_grouping_prompt(residue)

    return SeriesDiscoveryResponse(
        series=suggestions,
        unmatched_card_ids=result.unmatched_card_ids,
        abs_group_count=result.abs_count,
        llm_group_count=result.llm_count,
        used_llm=result.llm_count > 0,
        manual_prompt=manual_prompt,
    )


@router.post("/series/discover/manual")
async def discover_series_manual(
    payload: ManualSeriesRequest,
    request: Request,
    store: YotoStore = Depends(get_store),
    user_id: str = Depends(get_user_id),
) -> SeriesDiscoveryResponse:
    """Turn a manually-obtained LLM grouping response into series suggestions.

    The client posts the raw JSON produced by an external LLM tool (from the
    ``manual_prompt`` returned by ``/series/discover``). The ABS residue is
    recomputed deterministically here, so the pasted titles map back to the
    same card ids without the client needing to echo them.
    """
    parsed = _parse_manual_grouping(payload.response)

    cards, meta_by_card = await _load_active_cards(store, user_id)
    if not cards:
        return SeriesDiscoveryResponse(
            series=[], unmatched_card_ids=[], abs_group_count=0, llm_group_count=0,
            used_llm=False,
        )

    # ABS-only grouping reproduces the exact residue the manual prompt covered.
    result = await group_cards_by_series(cards, meta_by_card, llm_service=None)
    payload_by_card = dict(cards)
    residue = [
        (card_id, payload_by_card[card_id])
        for card_id in result.unmatched_card_ids
        if card_id in payload_by_card
    ]

    groups, unmatched = apply_llm_grouping(residue, parsed)
    suggestions = [
        SeriesSuggestion(
            series_name=group.series_name,
            card_ids=group.card_ids,
            sequence_numbers=_sequence_numbers_for_group(group.card_ids, payload_by_card),
            provenance="llm",
        )
        for group in groups
    ]

    return SeriesDiscoveryResponse(
        series=suggestions,
        unmatched_card_ids=unmatched,
        abs_group_count=0,
        llm_group_count=len(suggestions),
        used_llm=True,
    )


def _sequence_numbers_for_group(
    card_ids: list[str],
    payload_by_card: dict[str, dict[str, Any]],
) -> dict[str, int]:
    """Return per-card series order only when source metadata provides it."""
    numbers: dict[str, int] = {}
    for card_id in card_ids:
        sequence = resolve_sequence_number(payload_by_card.get(card_id, {}))
        if sequence is not None:
            numbers[card_id] = sequence
    return numbers
