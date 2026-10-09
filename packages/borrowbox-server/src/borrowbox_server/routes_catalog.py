"""Catalog API routes — search, series, product detail, availability."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import asdict
from fastapi import APIRouter, Depends, HTTPException, Request

from borrowbox import BorrowBoxCatalog, CatalogItem, Format
from borrowbox_server.credentials import vault_for
from borrowbox_server.schemas import (
    AvailabilityInfo,
    CrossLibraryResult,
    MultiLibraryAvailability,
    SearchResult,
    SeriesInfo,
)
from borrowbox_server.store import Store

router = APIRouter()

logger = logging.getLogger(__name__)


def _get_store(request: Request) -> Store:
    return request.app.state.store


def _get_settings(request: Request):
    return request.app.state.settings


def _get_library_names(request: Request) -> list[str]:
    return list(request.app.state.settings.libraries.keys())


def _item_to_result(item: CatalogItem) -> SearchResult:
    """Convert a borrowbox CatalogItem to an API SearchResult."""
    return SearchResult(
        product_id=item.product_id,
        title=item.title,
        authors=item.authors,
        series_name=item.series_name,
        series_number=item.series_number,
        series_id=str(item.series_id) if item.series_id else None,
        availability=item.availability.value if item.availability else "unknown",
        available_date=item.available_date,
        format=item.format,
        isbn=item.isbn,
        description=item.description,
        cover_url=item.cover_url,
        product_url=item.product_url,
    )


def _is_audio_item(item: CatalogItem) -> bool:
    return bool(item.format and "audio" in item.format.lower())


def _normalize_series_lookup(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _series_lookup_candidates(items: list[CatalogItem], name: str) -> list[CatalogItem]:
    """Return matching series search hits, preferring audiobook series IDs."""
    needle = _normalize_series_lookup(name)
    exact_audio: list[CatalogItem] = []
    exact_other: list[CatalogItem] = []
    partial_audio: list[CatalogItem] = []
    partial_other: list[CatalogItem] = []
    title_audio: list[CatalogItem] = []
    title_other: list[CatalogItem] = []

    for item in items:
        if not item.series_id:
            continue
        series_name = _normalize_series_lookup(item.series_name)
        title = _normalize_series_lookup(item.title)
        if series_name == needle:
            (exact_audio if _is_audio_item(item) else exact_other).append(item)
        elif series_name and needle in series_name:
            (partial_audio if _is_audio_item(item) else partial_other).append(item)
        elif title and needle in title:
            (title_audio if _is_audio_item(item) else title_other).append(item)

    candidates: list[CatalogItem] = []
    seen_series_ids: set[str] = set()
    for item in [*exact_audio, *exact_other, *partial_audio, *partial_other, *title_audio, *title_other]:
        series_id = str(item.series_id)
        if series_id in seen_series_ids:
            continue
        seen_series_ids.add(series_id)
        candidates.append(item)
    return candidates


def _audio_search_cache_query(query: str) -> str:
    return f"audio:v1:{query}"


async def _cache_items(store: Store, library: str, items: list[CatalogItem]) -> None:
    """Cache products and availability for a list of CatalogItems."""
    if not items:
        return
    await store.set_products_bulk(library, [asdict(it) for it in items])
    avail_items = [
        {
            "product_id": it.product_id,
            "status": it.availability.value if it.availability else "unknown",
            "available_date": it.available_date,
        }
        for it in items
    ]
    await store.set_availability_bulk(library, avail_items)


def _validate_library(library: str, settings) -> str:
    """Validate library name against configured libraries."""
    if library not in settings.libraries:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown library '{library}'. Configured: {list(settings.libraries.keys())}",
        )
    return settings.libraries[library]  # returns subdomain


# ------------------------------------------------------------------
# Search
# ------------------------------------------------------------------


@router.get("/search/{library}")
async def search(
    library: str,
    q: str,
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
) -> list[SearchResult]:
    """Search a library's catalog. Results are cached."""
    subdomain = _validate_library(library, settings)

    # Check cache first
    cache_query = _audio_search_cache_query(q)
    cached = await store.get_search(library, cache_query)
    if cached is not None:
        logger.info("Search cache hit: %s on %s (%d results)", q, library, len(cached))
        return [SearchResult(**{k: v for k, v in r.items() if k in SearchResult.model_fields}) for r in cached]

    # Run the synchronous BorrowBox client in a thread
    def _do_search() -> list[CatalogItem]:
        with BorrowBoxCatalog(subdomain) as cat:
            return cat.search(q, Format.AUDIOBOOK)

    items = await asyncio.to_thread(_do_search)

    # Cache search results, individual products, and availability
    results = [_item_to_result(it) for it in items]
    if items:
        result_dicts = [r.model_dump() for r in results]
        await store.set_search(library, cache_query, result_dicts)
        await _cache_items(store, library, items)

    return results


# ------------------------------------------------------------------
# Series
# ------------------------------------------------------------------


@router.get("/series/{library}/{series_id}")
async def get_series(
    library: str,
    series_id: str,
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
) -> SeriesInfo:
    """Get all items in a series."""
    subdomain = _validate_library(library, settings)

    # Check cache first
    cached = await store.get_series(library, series_id)
    if cached:
        # Return cached series — products inside have cached availability too
        product_ids = cached.get("product_ids", [])
        items = []
        for pid in product_ids:
            product = await store.get_product(library, pid)
            if product and product.get("format") == "eAudiobooks":
                items.append(SearchResult(**{
                    k: v for k, v in product.items()
                    if k in SearchResult.model_fields
                }))
        if items:
            return SeriesInfo(
                series_id=series_id,
                name=cached.get("name", "Unknown"),
                items=items,
            )

    def _do_series() -> list[CatalogItem]:
        with BorrowBoxCatalog(subdomain) as cat:
            return cat.get_series(series_id)

    items = await asyncio.to_thread(_do_series)

    series_name = items[0].series_name if items else "Unknown"

    # Cache series, products, and availability
    product_ids = [it.product_id for it in items]
    await store.set_series(library, series_id, series_name or "Unknown", product_ids)
    await _cache_items(store, library, items)

    return SeriesInfo(
        series_id=series_id,
        name=series_name or "Unknown",
        items=[_item_to_result(it) for it in items if _is_audio_item(it)],
    )


@router.get("/series-lookup")
async def series_lookup(
    name: str,
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
) -> SeriesInfo:
    """Search BorrowBox for a series by name, returning all items in it.

    Results are cached in Redis for 7 days so subsequent loads are instant.
    Tries each configured library until one returns a matching series.
    """
    # Check Redis cache first (keyed by normalized series name)
    cache_key = f"bb:series_lookup:v3:{name.lower().strip()}"
    cached_raw = await store._r.get(cache_key)
    if cached_raw:
        cached = json.loads(cached_raw)
        logger.info("Series-lookup cache hit: %s (%d items)", name, len(cached.get("items", [])))
        return SeriesInfo(**cached)

    # Snapshot: the registry can change while this loop awaits.
    for lib_name, subdomain in list(settings.libraries.items()):

        def _search_and_fetch(sd=subdomain) -> list[CatalogItem] | None:
            with BorrowBoxCatalog(sd) as cat:
                results = cat.search(name)
                for candidate in _series_lookup_candidates(results, name):
                    items = cat.get_series(candidate.series_id)
                    audio_items = [it for it in items if _is_audio_item(it)]
                    if audio_items:
                        return audio_items
                return None

        items = await asyncio.to_thread(_search_and_fetch)
        if items:
            series_name = items[0].series_name or name
            series_id = str(items[0].series_id) if items[0].series_id else "unknown"
            result = SeriesInfo(
                series_id=series_id,
                name=series_name,
                items=[_item_to_result(it) for it in items],
            )
            # Cache for 7 days
            await store._r.set(cache_key, result.model_dump_json(), ex=604800)
            return result

    # No library had a match — cache the miss too (1 day) to avoid repeated lookups
    empty_result = SeriesInfo(series_id="unknown", name=name, items=[])
    await store._r.set(cache_key, empty_result.model_dump_json(), ex=86400)
    return empty_result


@router.delete("/series-cache")
async def clear_series_cache(store: Store = Depends(_get_store)) -> dict:
    """Clear all cached series-lookup data so stale entries are refreshed."""
    keys = []
    async for key in store._r.scan_iter("bb:series_lookup:*"):
        keys.append(key)
    if keys:
        await store._r.delete(*keys)
    return {"cleared": len(keys)}


# ------------------------------------------------------------------
# Product detail
# ------------------------------------------------------------------


@router.get("/product/{library}/{product_id}")
async def get_product(
    library: str,
    product_id: str,
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
) -> SearchResult:
    """Get detailed product info."""
    subdomain = _validate_library(library, settings)

    # Check cache
    cached = await store.get_product(library, product_id)
    if cached:
        return SearchResult(**{
            k: v for k, v in cached.items()
            if k in SearchResult.model_fields
        })

    def _do_product():
        with BorrowBoxCatalog(subdomain) as cat:
            return cat.get_product(product_id)

    detail = await asyncio.to_thread(_do_product)
    if not detail:
        raise HTTPException(status_code=404, detail="Product not found")

    data = asdict(detail)
    await store.set_product(library, product_id, data)

    return SearchResult(**{
        k: v for k, v in data.items()
        if k in SearchResult.model_fields
    })


# ------------------------------------------------------------------
# Multi-library availability
# ------------------------------------------------------------------


@router.get("/availability/{product_id}")
async def check_availability(
    product_id: str,
    title: str = "",
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
) -> MultiLibraryAvailability:
    """Check availability of a product across all configured libraries."""
    libraries = list(settings.libraries.keys())

    # Check cache first
    cached = await store.get_availability_multi(libraries, product_id)

    # For any library without cached data, do a live lookup
    missing_libs = [lib for lib in libraries if cached[lib] is None]

    if missing_libs:

        async def _check_one(lib_name: str) -> tuple[str, CatalogItem | None]:
            subdomain = settings.libraries[lib_name]

            def _do():
                with BorrowBoxCatalog(subdomain) as cat:
                    result = cat.get_product(product_id)
                    return result

            product = await asyncio.to_thread(_do)
            return lib_name, product

        tasks = [_check_one(lib) for lib in missing_libs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.error("Availability check failed: %s", result)
                continue
            lib_name, product = result
            if product:
                status = product.availability.value if product.availability else "unknown"
                logger.info(
                    "Availability for %s in %s: %s (date=%s)",
                    product_id, lib_name, status, product.available_date,
                )
                await store.set_availability(
                    lib_name, product_id, status, product.available_date
                )
                cached[lib_name] = {
                    "status": status,
                    "available_date": product.available_date,
                    "checked_at": None,
                }

    lib_avail = {}
    for lib in libraries:
        info = cached.get(lib)
        if info:
            lib_avail[lib] = AvailabilityInfo(
                product_id=product_id,
                library=lib,
                status=info["status"],
                available_date=info.get("available_date"),
                checked_at=info.get("checked_at"),
            )

    return MultiLibraryAvailability(
        product_id=product_id,
        title=title,
        libraries=lib_avail,
    )


# ------------------------------------------------------------------
# Cross-library search
# ------------------------------------------------------------------


@router.get("/search-all")
async def search_all(
    q: str,
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
) -> list[CrossLibraryResult]:
    """Search all configured libraries in parallel and merge results.

    Results are deduplicated by product_id and include per-library
    availability, sorted by best availability (available first, then
    earliest available_date).
    """
    libraries = list(settings.libraries.keys())
    if not libraries:
        return []

    async def _search_one(lib_name: str) -> tuple[str, list[CatalogItem]]:
        subdomain = settings.libraries[lib_name]

        # Check cache first
        cache_query = _audio_search_cache_query(q)
        cached = await store.get_search(lib_name, cache_query)
        if cached is not None:
            logger.info("Cross-search cache hit: %s on %s (%d)", q, lib_name, len(cached))
            # Reconstruct CatalogItem-like dicts from cached data
            return lib_name, cached

        def _do():
            with BorrowBoxCatalog(subdomain) as cat:
                return cat.search(q, Format.AUDIOBOOK)

        items = await asyncio.to_thread(_do)

        # Cache results
        results = [_item_to_result(it) for it in items]
        if items:
            result_dicts = [r.model_dump() for r in results]
            await store.set_search(lib_name, cache_query, result_dicts)
            await _cache_items(store, lib_name, items)

        return lib_name, items

    # Search all libraries in parallel
    tasks = [_search_one(lib) for lib in libraries]
    search_results = await asyncio.gather(*tasks, return_exceptions=True)

    # Merge by product_id — collect availability from each library
    merged: dict[str, dict] = {}  # product_id -> merged data

    for result in search_results:
        if isinstance(result, Exception):
            logger.error("Cross-library search failed for a library: %s", result)
            continue

        lib_name, items = result
        for item in items:
            # Items may be CatalogItem objects or cached dicts
            if isinstance(item, dict):
                pid = item.get("product_id", "")
                status = item.get("availability", "unknown")
                avail_date = item.get("available_date")
            else:
                pid = item.product_id
                status = item.availability.value if item.availability else "unknown"
                avail_date = item.available_date

            if not pid:
                continue

            if pid not in merged:
                # First time seeing this product — use its data as base
                if isinstance(item, dict):
                    merged[pid] = {
                        "product_id": pid,
                        "title": item.get("title", ""),
                        "authors": item.get("authors", []),
                        "series_name": item.get("series_name"),
                        "series_number": item.get("series_number"),
                        "series_id": item.get("series_id"),
                        "format": item.get("format"),
                        "isbn": item.get("isbn"),
                        "description": item.get("description"),
                        "cover_url": item.get("cover_url"),
                        "product_url": item.get("product_url"),
                        "libraries": {},
                    }
                else:
                    merged[pid] = {
                        "product_id": pid,
                        "title": item.title,
                        "authors": item.authors,
                        "series_name": item.series_name,
                        "series_number": item.series_number,
                        "series_id": str(item.series_id) if item.series_id else None,
                        "format": item.format,
                        "isbn": item.isbn,
                        "description": item.description,
                        "cover_url": item.cover_url,
                        "product_url": item.product_url,
                        "libraries": {},
                    }

            merged[pid]["libraries"][lib_name] = AvailabilityInfo(
                product_id=pid,
                library=lib_name,
                status=status,
                available_date=avail_date,
            )

    # Determine best library for each result and sort
    def _availability_sort_key(entry: dict) -> tuple[int, str]:
        """Sort: available first (0), then on_loan by date (1), unknown last (2)."""
        libs = entry.get("libraries", {})
        best_rank = 2
        best_date = "9999"
        for info in libs.values():
            if info.status == "available":
                best_rank = 0
                best_date = ""
            elif info.status == "on_loan" and best_rank > 0:
                best_rank = 1
                if info.available_date and info.available_date < best_date:
                    best_date = info.available_date
        return (best_rank, best_date)

    results = []
    for entry in sorted(merged.values(), key=_availability_sort_key):
        libs = entry.pop("libraries")
        best_lib = None
        best_status = "unknown"
        best_date = "9999"
        for lib_name, info in libs.items():
            if info.status == "available":
                if best_status != "available":
                    best_lib = lib_name
                    best_status = "available"
                    best_date = ""
            elif info.status == "on_loan":
                if best_status != "available":
                    date = info.available_date or "9999"
                    if date < best_date:
                        best_lib = lib_name
                        best_status = "on_loan"
                        best_date = date

        if best_lib is None and libs:
            best_lib = next(iter(libs))

        results.append(CrossLibraryResult(
            **entry,
            libraries=libs,
            best_library=best_lib,
            best_status=best_status,
        ))

    return results


# ------------------------------------------------------------------
# Libraries listing
# ------------------------------------------------------------------


@router.get("/libraries")
async def list_libraries(
    store: Store = Depends(_get_store),
    settings=Depends(_get_settings),
) -> list[dict]:
    """List configured libraries with their credential state.

    This previously returned only name and subdomain, while the LibraryInfo
    schema advertised an ``is_authenticated`` field that no route populated —
    so clients reading it saw every library as signed out. The field is now
    real, derived from the last recorded verification.
    """
    vault = vault_for(store, settings)
    out: list[dict] = []
    # Snapshot: awaits inside the loop, and the registry is mutable at runtime.
    for name, subdomain in list(settings.libraries.items()):
        status = await vault.status(name)
        out.append(
            {
                "name": name,
                "subdomain": subdomain,
                "has_credentials": status.has_credentials,
                "credential_source": status.source,
                # None means "never verified", which is distinct from False.
                "is_authenticated": status.verified,
                "credential_error": status.error,
            }
        )
    return out
