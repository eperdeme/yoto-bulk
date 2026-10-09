"""Card and push routes for yoto-server."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from yoto_bulk.audiobookshelf import ABSClient, build_yoto_payload_from_abs
from yoto_bulk.auth import YotoAuthService
from yoto_bulk.card_utils import (
    preserve_yoto_media,
    resolve_authors,
    resolve_description,
    resolve_sequence_number,
    resolve_series,
    resolve_title,
    truncate_for_api,
    enrich_payload_icons,
)
from yoto_bulk.yoto_client import YotoClient
from yoto_bulk.store import content_hash
from yoto_bulk.workflows import report_workflow

from yoto_server.schemas import (
    AuthStatus,
    ChapterInfo,
    FamilyLibraryCard,
    PushResult,
    YotoCardDetail,
    YotoCardSummary,
)
from yoto_server.deps import get_abs_client, get_settings, get_store, get_user_id
from yoto_server.reconcile import find_yoto_match_for_abs
from yoto_server.store import YotoStore, extract_user_id_from_jwt

router = APIRouter()
logger = logging.getLogger(__name__)

_AUDIO_UPLOAD_CONCURRENCY = 4


def _should_hide_from_library(
    card_id: str,
    meta: dict[str, str] | None,
    known_card_ids: set[str],
) -> bool:
    meta = meta or {}
    if meta.get("source") != "yoto":
        return False
    linked_abs_card_id = meta.get("linked_abs_card_id")
    return bool(linked_abs_card_id and linked_abs_card_id in known_card_ids and linked_abs_card_id != card_id)


def _ensure_track_shape(payload: dict[str, Any]) -> None:
    """Backfill required Yoto track fields for cached payloads created before fixes."""
    chapters = ((payload.get("content") or {}).get("chapters") or [])
    for chapter in chapters:
        tracks = chapter.get("tracks") or []
        for idx, track in enumerate(tracks, start=1):
            if not isinstance(track, dict):
                continue
            track.setdefault("key", f"{idx:02d}")
            track.setdefault("type", "audio")


def _cover_url_for_card(
    request: Request,
    card_id: str,
    payload: dict[str, Any],
    meta: dict[str, str] | None = None,
) -> str | None:
    source = (meta or {}).get("source")
    cover = ((payload.get("metadata") or {}).get("cover") or {}).get("imageL")
    if source == "abs":
        return str(request.url_for("get_card_cover", card_id=card_id))
    return cover


def _media_type_for_card(payload: dict[str, Any]) -> str:
    category = (payload.get("metadata") or {}).get("category")
    return "podcast" if category == "podcast" else "book"


def _card_summary(
    request: Request,
    card_id: str,
    payload: dict[str, Any],
    meta: dict[str, str] | None = None,
    last_played_at: str | None = None,
) -> YotoCardSummary:
    """Build a card summary from a raw payload."""
    chapters = (payload.get("content") or {}).get("chapters") or []
    source = (meta or {}).get("source")
    yoto_card_id = (meta or {}).get("yoto_card_id")
    push_hash = (meta or {}).get("push_hash")
    needs_push = source == "abs" and (
        not yoto_card_id or bool(push_hash and content_hash(payload) != push_hash)
    )

    return YotoCardSummary(
        card_id=card_id,
        yoto_card_id=yoto_card_id or (card_id if source == "yoto" else None),
        source=source,
        media_type=_media_type_for_card(payload),
        needs_push=needs_push,
        title=resolve_title(payload),
        authors=resolve_authors(payload),
        series=resolve_series(payload),
        sequence_number=resolve_sequence_number(payload),
        description=resolve_description(payload),
        cover_url=_cover_url_for_card(request, card_id, payload, meta),
        chapter_count=len(chapters),
        last_played_at=last_played_at,
        updated_at=(meta or {}).get("external_updated_at") or (meta or {}).get("updated_at"),
        last_pushed_at=(meta or {}).get("pushed_at"),
    )


async def _card_detail(
    request: Request,
    card_id: str,
    payload: dict[str, Any],
    meta: dict[str, str] | None = None,
) -> YotoCardDetail:
    """Build full card detail from a raw payload."""
    raw_chapters = (payload.get("content") or {}).get("chapters") or []
    chapters = []
    for ch in raw_chapters:
        tracks = ch.get("tracks") or []
        chapters.append(ChapterInfo(
            title=ch.get("title", ""),
            key=ch.get("key"),
            track_count=len(tracks),
            duration=ch.get("duration"),
        ))

    source = (meta or {}).get("source")
    external_id = (meta or {}).get("external_id")
    external_url = None
    if source == "abs" and external_id:
        abs_url = request.app.state.settings.abs_url.rstrip("/")
        external_url = f"{abs_url}/item/{external_id}"

    diff_fields = []
    # If it's a known Yoto card, we can try to diff it.
    # To keep this fast and avoid hitting the Yoto API on every detail view,
    # we'd ideally want to cache the "last known live" state.
    # For now, we'll just check against the `pull_hash` logic.
    # If the card is unsynced, we can't easily say *which* fields are different
    # without the live payload.
    # As a compromise, if it's unsynced, we'll mark all metadata fields as potential diffs.
    if meta and meta.get("pull_hash") and content_hash(payload) != meta.get("pull_hash"):
        # This is a bit coarse but highlights that the local copy is "newer"
        diff_fields = ["title", "author", "series", "sequence_number", "description"]

    yoto_card_id = (meta or {}).get("yoto_card_id")
    # For native Yoto cards the card_id itself is the Yoto card ID
    if not yoto_card_id and source == "yoto":
        yoto_card_id = card_id

    return YotoCardDetail(
        card_id=card_id,
        yoto_card_id=yoto_card_id,
        source=source,
        needs_push=_card_summary(request, card_id, payload, meta).needs_push,
        media_type=_media_type_for_card(payload),
        title=resolve_title(payload),
        authors=resolve_authors(payload),
        series=resolve_series(payload),
        sequence_number=resolve_sequence_number(payload),
        description=resolve_description(payload),
        cover_url=_cover_url_for_card(request, card_id, payload, meta),
        external_url=external_url,
        diff_fields=diff_fields,
        chapter_count=len(raw_chapters),
        chapters=chapters,
        raw_metadata=payload.get("metadata") or {},
        updated_at=(meta or {}).get("external_updated_at") or (meta or {}).get("updated_at"),
        last_pushed_at=(meta or {}).get("pushed_at"),
    )


# ------------------------------------------------------------------
# List all cards
# ------------------------------------------------------------------


@router.delete("/cards")
async def clear_cards(
    store: YotoStore = Depends(get_store),
    user_id: str = Depends(get_user_id),
) -> dict:
    """Delete all cached cards for the current user. USE WITH CAUTION."""
    card_ids = await store.list_card_ids(user_id)
    count = 0
    for cid in card_ids:
        if await store.remove_card(user_id, cid):
            count += 1
    logger.info("Cleared %d cards from Redis for user %s", count, user_id)
    return {"cleared": count}


@router.get("/cards")
async def list_cards(
    request: Request,
    store: YotoStore = Depends(get_store),
    user_id: str = Depends(get_user_id),
) -> list[YotoCardSummary]:
    """List all cards in the user's Yoto library."""
    # Fetch last-played data from family library API
    last_played_map: dict[str, str] = {}
    try:
        from yoto_bulk.config import Settings as YotoBulkSettings
        yoto_settings = YotoBulkSettings()
        auth_service = YotoAuthService(yoto_settings)
        if auth_service.token_store.load() is not None:
            yoto_client = YotoClient(yoto_settings, auth_service)
            family_data = await yoto_client.get_json("/card/family/library")
            for entry in family_data.get("cards", []):
                lp = entry.get("lastPlayedAt")
                if lp:
                    last_played_map[entry.get("cardId", "")] = lp
    except Exception as exc:
        logger.debug("Failed to fetch family library for last-played: %s", exc)

    results: list[YotoCardSummary] = []
    all_meta = await store.get_all_meta(user_id)
    all_cards = await store.load_all_cards(user_id)
    known_card_ids = {card_id for card_id, _ in all_cards}
    for card_id, payload in all_cards:
        meta = all_meta.get(card_id) or {}
        if _should_hide_from_library(card_id, meta, known_card_ids):
            continue
        # Match by yoto_card_id (ABS cards) or card_id directly (yoto-source cards)
        yoto_cid = meta.get("yoto_card_id") or card_id
        played = last_played_map.get(yoto_cid)
        results.append(_card_summary(request, card_id, payload, meta, last_played_at=played))

    results.sort(
        key=lambda c: (
            c.series or "\xff",
            c.sequence_number or 999,
            c.title,
        )
    )
    return results


# ------------------------------------------------------------------
# Modified cards (changed since last push)
# ------------------------------------------------------------------


@router.get("/cards/modified")
async def list_modified(
    request: Request,
    store: YotoStore = Depends(get_store),
    user_id: str = Depends(get_user_id),
) -> list[YotoCardSummary]:
    """List cards that have been modified locally since their last push."""
    results: list[YotoCardSummary] = []
    all_meta = await store.get_all_meta(user_id)
    known_card_ids = set(all_meta)
    for card_id, payload, title in await store.modified_cards(user_id):
        meta = all_meta.get(card_id) or {}
        if _should_hide_from_library(card_id, meta, known_card_ids):
            continue
        results.append(_card_summary(request, card_id, payload, meta))
    return results


# ------------------------------------------------------------------
# Single card detail
# ------------------------------------------------------------------


@router.get("/cards/{card_id}")
async def get_card(
    card_id: str,
    request: Request,
    store: YotoStore = Depends(get_store),
    user_id: str = Depends(get_user_id),
) -> YotoCardDetail:
    """Get detailed card info including chapters."""
    payload = await store.load_card(user_id, card_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Card not found")
    meta = await store.get_meta(user_id, card_id)
    return await _card_detail(request, card_id, payload, meta)


# ------------------------------------------------------------------
# Card cover proxy
# ------------------------------------------------------------------


@router.get("/cards/{card_id}/cover", name="get_card_cover")
async def get_card_cover(
    card_id: str,
    request: Request,
    store: YotoStore = Depends(get_store),
    user_id: str = Depends(get_user_id),
) -> Response:
    """Proxy card cover art so the browser can render ABS-backed images."""
    payload = await store.load_card(user_id, card_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Card not found")

    meta = await store.get_meta(user_id, card_id)
    source = meta.get("source")
    cover = ((payload.get("metadata") or {}).get("cover") or {}).get("imageL")

    try:
        if source == "abs":
            external_id = meta.get("external_id")
            if not external_id:
                raise HTTPException(status_code=404, detail="ABS-backed card has no external id")
            abs_client = get_abs_client(request)
            try:
                try:
                    content, content_type = await abs_client.fetch_cover(external_id)
                except Exception as exc:
                    parsed = urlsplit(cover or "")
                    hostname = parsed.hostname or ""
                    if parsed.scheme != "https" or not (
                        hostname == "yotoplay.com" or hostname.endswith(".yotoplay.com")
                    ):
                        if "certificate has expired" in str(exc).lower():
                            raise HTTPException(
                                status_code=503,
                                detail="Audiobookshelf TLS certificate has expired",
                            ) from exc
                        raise
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        response = await client.get(cover)
                        response.raise_for_status()
                        return Response(
                            content=response.content,
                            media_type=response.headers.get("content-type", "image/jpeg"),
                            headers={
                                "Cache-Control": "private, max-age=300",
                                "X-Cover-Source": "yoto-cache",
                            },
                        )
            finally:
                await abs_client.aclose()
            return Response(content=content, media_type=content_type, headers={"Cache-Control": "private, max-age=300"})

        if not cover:
            raise HTTPException(status_code=404, detail="Card has no cover")

        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            resp = await client.get(cover)
            resp.raise_for_status()
            return Response(
                content=resp.content,
                media_type=resp.headers.get("content-type", "image/jpeg"),
                headers={"Cache-Control": "private, max-age=300"},
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to proxy cover for %s: %s", card_id, exc)
        raise HTTPException(status_code=502, detail="Failed to load cover") from exc


# ------------------------------------------------------------------
# Family library (purchased / shared Yoto cards)
# ------------------------------------------------------------------


@router.get("/family-library")
async def get_family_library(
    settings=Depends(get_settings),
) -> list[FamilyLibraryCard]:
    """Return purchased and shared cards from the Yoto family library.

    Calls ``GET /card/family/library`` on the Yoto API and filters out
    MYO cards (which are already managed via our own library).
    """
    from yoto_bulk.config import Settings as YotoBulkSettings

    yoto_settings = YotoBulkSettings()
    auth_service = YotoAuthService(yoto_settings)
    tokens = auth_service.token_store.load()
    if tokens is None:
        raise HTTPException(status_code=401, detail="Not authenticated with Yoto")

    yoto_client = YotoClient(yoto_settings, auth_service)
    data = await yoto_client.get_json("/card/family/library")
    raw_cards = data.get("cards", [])

    results: list[FamilyLibraryCard] = []
    for entry in raw_cards:
        share_type = entry.get("shareType", "")
        if share_type == "myo":
            continue  # skip MYO cards — already in our library

        inner = entry.get("card") or {}
        inner_meta = inner.get("metadata") or {}
        inner_content = inner.get("content") or {}
        cover_url = (inner_meta.get("cover") or {}).get("imageL")

        languages = inner_meta.get("languages") or []
        if isinstance(languages, str):
            languages = [languages]

        results.append(FamilyLibraryCard(
            card_id=entry.get("cardId", ""),
            title=inner.get("title", ""),
            author=inner_meta.get("author"),
            description=inner_meta.get("description"),
            cover_url=cover_url,
            category=inner_meta.get("category"),
            share_type=share_type,
            last_played_at=entry.get("lastPlayedAt"),
            added_at=entry.get("addedAt"),
            slug=inner.get("slug"),
            reason=entry.get("reason"),
            languages=languages,
            media=inner_meta.get("media") if isinstance(inner_meta.get("media"), dict) else None,
            audio_preview_url=inner_meta.get("audioPreviewUrl"),
            playback_type=inner_content.get("playbackType"),
        ))

    results.sort(key=lambda c: c.title.lower())
    return results


# ------------------------------------------------------------------
# Orphan detection and cleanup
# ------------------------------------------------------------------


@router.get("/orphans")
async def find_orphans(
    store: YotoStore = Depends(get_store),
    user_id: str = Depends(get_user_id),
    settings=Depends(get_settings),
) -> list[dict[str, Any]]:
    """Find Yoto cards that exist on the Yoto API but are not tracked in our DB.

    Returns a list of {cardId, title} for cards on Yoto that have no
    corresponding yoto_card_id or card_id in Redis.
    """
    from yoto_bulk.config import Settings as YotoBulkSettings

    yoto_settings = YotoBulkSettings()
    auth_service = YotoAuthService(yoto_settings)
    tokens = auth_service.token_store.load()
    if tokens is None:
        raise HTTPException(status_code=401, detail="Not authenticated with Yoto")

    yoto_client = YotoClient(yoto_settings, auth_service)
    mine = await yoto_client.get_content_mine()
    remote_cards = mine.get("cards", [])

    # Build set of all known Yoto card IDs from our Redis store
    all_meta = await store.get_all_meta(user_id)
    known_yoto_ids: set[str] = set()
    for card_id, meta in all_meta.items():
        # The card_id itself (for yoto-source cards)
        known_yoto_ids.add(card_id)
        # The linked yoto_card_id (for ABS-backed cards)
        yoto_card_id = meta.get("yoto_card_id")
        if yoto_card_id:
            known_yoto_ids.add(yoto_card_id)

    orphans = []
    for card in remote_cards:
        if not isinstance(card, dict):
            continue
        card_id = card.get("cardId") or ""
        if card_id and card_id not in known_yoto_ids:
            orphans.append({
                "cardId": card_id,
                "title": card.get("title") or "(untitled)",
                "createdAt": card.get("createdAt"),
                "updatedAt": card.get("updatedAt"),
            })

    return orphans


@router.delete("/orphans/{card_id}")
async def delete_orphan(
    card_id: str,
    settings=Depends(get_settings),
) -> dict[str, str]:
    """Delete a single orphan card from the Yoto API."""
    from yoto_bulk.config import Settings as YotoBulkSettings

    yoto_settings = YotoBulkSettings()
    auth_service = YotoAuthService(yoto_settings)
    tokens = auth_service.token_store.load()
    if tokens is None:
        raise HTTPException(status_code=401, detail="Not authenticated with Yoto")

    yoto_client = YotoClient(yoto_settings, auth_service)
    await yoto_client.delete_content(card_id)
    return {"status": "deleted", "cardId": card_id}


@router.delete("/cards/{card_id}")
async def delete_card(
    card_id: str,
    store: YotoStore = Depends(get_store),
    user_id: str = Depends(get_user_id),
    settings=Depends(get_settings),
) -> dict[str, str]:
    """Delete a yoto-source card from both the Yoto API and Redis.

    Only allowed for cards with source=yoto (i.e. not ABS-backed).
    """
    from yoto_bulk.config import Settings as YotoBulkSettings

    meta = await store.get_meta(user_id, card_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Card not found")
    if meta.get("source") != "yoto":
        raise HTTPException(status_code=400, detail="Only yoto-source cards can be deleted this way")

    # Delete from Yoto API
    yoto_settings = YotoBulkSettings()
    auth_service = YotoAuthService(yoto_settings)
    tokens = auth_service.token_store.load()
    if tokens is None:
        raise HTTPException(status_code=401, detail="Not authenticated with Yoto")

    yoto_client = YotoClient(yoto_settings, auth_service)
    try:
        await yoto_client.delete_content(card_id)
    except Exception as exc:
        logger.warning("Failed to delete card %s from Yoto API (may already be gone): %s", card_id, exc)

    # Remove from Redis
    await store.remove_card(user_id, card_id)
    logger.info("Deleted yoto-source card %s from Yoto API and Redis", card_id)
    return {"status": "deleted", "cardId": card_id}


@router.post("/orphans/delete")
async def delete_orphans_bulk(
    request: Request,
    settings=Depends(get_settings),
) -> dict[str, Any]:
    """Delete multiple orphan cards from the Yoto API.

    Body: {"card_ids": ["abc12", "def34", ...]}
    """
    from yoto_bulk.config import Settings as YotoBulkSettings

    body = await request.json()
    card_ids = body.get("card_ids", [])
    if not card_ids:
        return {"deleted": 0, "failed": []}

    yoto_settings = YotoBulkSettings()
    auth_service = YotoAuthService(yoto_settings)
    tokens = auth_service.token_store.load()
    if tokens is None:
        raise HTTPException(status_code=401, detail="Not authenticated with Yoto")

    yoto_client = YotoClient(yoto_settings, auth_service)
    deleted = 0
    failed = []
    for cid in card_ids:
        try:
            await yoto_client.delete_content(cid)
            deleted += 1
        except Exception as exc:
            logger.error("Failed to delete orphan %s: %s", cid, exc)
            failed.append({"cardId": cid, "error": str(exc)})

    return {"deleted": deleted, "failed": failed}


# ------------------------------------------------------------------
# Push to Yoto API
# ------------------------------------------------------------------


@router.post("/cards/{card_id}/push")
async def push_card(
    card_id: str,
    request: Request,
    store: YotoStore = Depends(get_store),
    user_id: str = Depends(get_user_id),
    settings=Depends(get_settings),
) -> PushResult:
    """Push a card to the Yoto API.

    ABS-backed cards stream their cover/audio from Audiobookshelf before
    saving to Yoto. Temporary imported cards still create a new Yoto card
    on first push.
    """
    payload = await store.load_card(user_id, card_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Card not found")

    title = resolve_title(payload)
    meta = await store.get_meta(user_id, card_id)
    source = meta.get("source")
    is_import = card_id.startswith("import-") or source == "abs"
    external_id = meta.get("external_id")
    existing_yoto_card_id = meta.get("yoto_card_id")

    # Defensive check: if reconcile didn't link this ABS card to an existing
    # Yoto card, look one up now so we update in place instead of creating a
    # duplicate on Yoto. (Common when the user pulls from Yoto then syncs ABS
    # but reconcile linkage was missed for any reason.)
    if source == "abs" and not existing_yoto_card_id:
        match_id = await find_yoto_match_for_abs(store, user_id, card_id)
        if match_id:
            logger.info(
                "Auto-linking ABS card %s to existing Yoto card %s before push",
                card_id,
                match_id,
            )
            await store._r.hset(
                store._meta_key(user_id, card_id),
                mapping={"yoto_card_id": match_id},
            )
            await store._r.hset(
                store._meta_key(user_id, match_id),
                mapping={"linked_abs_card_id": card_id},
            )
            existing_yoto_card_id = match_id

    # Build yoto client using shared settings
    from yoto_bulk.config import Settings as YotoBulkSettings

    yoto_settings = YotoBulkSettings()
    auth_service = YotoAuthService(yoto_settings)

    # Check we have tokens
    tokens = auth_service.token_store.load()
    if tokens is None:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated with Yoto. Configure saved Yoto OAuth tokens before pushing cards.",
        )

    yoto_client = YotoClient(yoto_settings, auth_service)

    try:
        await report_workflow("Preparing card")
        # Backup before pushing
        await store.backup_card(user_id, card_id)

        # Enrich icons
        enrich_payload_icons(payload)
        truncate_for_api(payload)

        # Upload cover/image assets for imported cards before pushing.
        if source == "abs":
            abs_client = get_abs_client(request)
            try:
                if not external_id:
                    raise HTTPException(status_code=500, detail="ABS-backed card is missing external_id")
                if existing_yoto_card_id:
                    try:
                        existing_yoto_payload = await yoto_client.get_content_card(existing_yoto_card_id)
                        if preserve_yoto_media(existing_yoto_payload, payload):
                            logger.info(
                                "Reused existing Yoto media for ABS card %s from %s",
                                card_id,
                                existing_yoto_card_id,
                            )
                    except Exception as exc:
                        logger.warning(
                            "Could not reuse existing Yoto media for ABS card %s from %s: %s",
                            card_id,
                            existing_yoto_card_id,
                            exc,
                        )
                await report_workflow("Uploading cover")
                await _upload_cover_from_abs(yoto_client, abs_client, external_id, payload)
                await _upload_audio_from_abs(yoto_client, abs_client, external_id, payload, title)
            finally:
                await abs_client.aclose()

        push_payload = payload.copy()

        # ABS-backed cards keep a stable Redis key based on the ABS item id.
        if source == "abs":
            if existing_yoto_card_id:
                push_payload["cardId"] = existing_yoto_card_id
            else:
                push_payload.pop("cardId", None)
        elif is_import:
            push_payload.pop("cardId", None)

        _ensure_track_shape(push_payload)
        await report_workflow("Saving card to Yoto")
        result = await yoto_client.save_content(push_payload)

        new_card = result.get("card", result)
        real_id = new_card.get("cardId") or new_card.get("contentId") or card_id

        if source == "abs":
            payload["cardId"] = real_id
            await store.save_card(user_id, card_id, payload, update_pull_hash=True)
            await store._r.hset(
                store._meta_key(user_id, card_id),
                mapping={
                    "source": "abs",
                    "external_id": external_id or "",
                    "yoto_card_id": real_id,
                    "push_hash": content_hash(payload),
                },
            )
        elif real_id != card_id:
            payload["cardId"] = real_id
            await store.rename_card(user_id, card_id, real_id, payload)
        else:
            await store.save_card(user_id, card_id, payload, update_pull_hash=True)

        pushed_at = datetime.now(UTC).isoformat()
        await store._r.hset(
            store._meta_key(user_id, card_id if source == "abs" else real_id),
            mapping={"pushed_at": pushed_at},
        )
        await store._r.hset(f"ui:sync:{user_id}", mapping={"last_yoto_push": pushed_at})

        return PushResult(
            card_id=real_id,
            title=title,
            status="pushed",
            message="Pushed successfully",
            new_card_id=real_id if real_id != card_id else None,
        )

    except asyncio.CancelledError:
        if source == "abs":
            await asyncio.shield(_save_partial_abs_push(store, user_id, card_id, payload, external_id))
        raise
    except Exception as exc:
        if source == "abs":
            await _save_partial_abs_push(store, user_id, card_id, payload, external_id)
        logger.error("Failed to push card %s: %s", card_id, exc)
        return PushResult(
            card_id=card_id,
            title=title,
            status="error",
            message=str(exc),
        )


async def _save_partial_abs_push(
    store: YotoStore,
    user_id: str,
    card_id: str,
    payload: dict[str, Any],
    external_id: str | None,
) -> None:
    try:
        await store.save_card(
            user_id,
            card_id,
            payload,
            source="abs",
            external_id=external_id,
        )
        logger.info("Saved partial ABS push state for %s; retry can reuse uploaded media", card_id)
    except Exception as exc:
        logger.warning("Could not save partial ABS push state for %s: %s", card_id, exc)


async def _upload_cover_from_abs(
    yoto_client: YotoClient,
    abs_client: ABSClient,
    item_id: str,
    payload: dict[str, Any],
) -> None:
    """Upload ABS cover image and set metadata.cover.imageL."""
    cover = (payload.get("metadata") or {}).get("cover", {}).get("imageL", "")
    if cover and "yotoplay.com" in cover:
        return

    file_data, content_type = await abs_client.fetch_cover(item_id)
    media_url = await yoto_client.upload_cover_image(file_data, content_type)
    payload.setdefault("metadata", {}).setdefault("cover", {})["imageL"] = media_url
    logger.info("Uploaded ABS cover for %s → %s", item_id, media_url)


async def _upload_audio_from_abs(
    yoto_client: YotoClient,
    abs_client: ABSClient,
    item_id: str,
    payload: dict[str, Any],
    title: str,
) -> None:
    """Upload ABS audio files for tracks with empty trackUrl."""
    chapters = payload.get("content", {}).get("chapters", [])
    is_podcast = (payload.get("metadata") or {}).get("category") == "podcast"
    if is_podcast:
        audio_tracks: list[dict[str, Any] | None] = [None] * len(chapters)
    else:
        playback = await abs_client.get_playback_session(item_id)
        audio_tracks = playback.get("audioTracks") or []
        if len(audio_tracks) != len(chapters):
            raise ValueError(
                f"Audio file count ({len(audio_tracks)}) != chapter count "
                f"({len(chapters)}) for {title}"
            )

    semaphore = asyncio.Semaphore(_AUDIO_UPLOAD_CONCURRENCY)
    completed = sum(bool((chapter.get("tracks") or [{}])[0].get("trackUrl")) for chapter in chapters)
    await report_workflow("Preparing audio", tracks_total=len(chapters), tracks_done=completed)

    async def _upload_one(
        idx: int, track_info: dict[str, Any] | None, chapter: dict[str, Any]
    ) -> None:
        nonlocal completed
        track = chapter.get("tracks", [{}])[0]
        if track.get("trackUrl") and track["trackUrl"] != "":
            return

        if is_podcast:
            episode_id = str(chapter.get("key") or "").strip()
            if not episode_id:
                raise ValueError(f"Podcast chapter {idx + 1} for {title} is missing an episode ID")
            playback = await abs_client.get_episode_playback_session(item_id, episode_id)
            episode_tracks = playback.get("audioTracks") or []
            if len(episode_tracks) != 1:
                raise ValueError(
                    f"Podcast episode {episode_id} for {title} has {len(episode_tracks)} audio tracks"
                )
            track_info = episode_tracks[0]

        content_url = track_info.get("contentUrl") if track_info else None
        if not content_url:
            raise ValueError(f"ABS audio track {idx + 1} for {title} is missing contentUrl")

        async with semaphore:
            await report_workflow(
                "Uploading audio", track_index=idx + 1, tracks_total=len(chapters),
                tracks_done=completed,
            )
            logger.info("Uploading ABS audio %d/%d for %s", idx + 1, len(audio_tracks), title)
            async with abs_client.download_audio_track_stream(content_url) as (
                stream,
                content_type,
                content_length,
            ):
                transcode = await yoto_client.upload_stream_and_transcode(
                    stream, content_type, content_length=content_length
                )
                info = transcode.get("transcodedInfo", {})

                track["trackUrl"] = f"yoto:#{transcode['transcodedSha256']}"
                track["duration"] = info.get("duration", 0)
                track["fileSize"] = info.get("fileSize", 0)
                track["channels"] = info.get("channels", "stereo")
                track["format"] = info.get("format", content_type.split("/")[-1])
                completed += 1
                await report_workflow("Audio ready", tracks_done=completed, tracks_total=len(chapters))

    results = await asyncio.gather(
        *[
            _upload_one(idx, track_info, chapter)
            for idx, (track_info, chapter) in enumerate(zip(audio_tracks, chapters, strict=True))
        ],
        return_exceptions=True,
    )
    errors = [result for result in results if isinstance(result, Exception)]
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise ExceptionGroup(f"Failed to upload {len(errors)} audio tracks for {title}", errors)


# ------------------------------------------------------------------
# Auth status
# ------------------------------------------------------------------


@router.get("/auth/status")
async def auth_status(
    settings=Depends(get_settings),
) -> AuthStatus:
    """Check whether we have a valid Yoto OAuth token."""
    from yoto_bulk.config import Settings as YotoBulkSettings

    yoto_settings = YotoBulkSettings()
    auth_service = YotoAuthService(yoto_settings)
    tokens = auth_service.token_store.load()

    user_id = None
    if tokens:
        try:
            user_id = extract_user_id_from_jwt(tokens.access_token)
        except ValueError:
            pass

    return AuthStatus(
        authenticated=tokens is not None,
        token_path=str(yoto_settings.token_path),
        user_id=user_id,
    )
