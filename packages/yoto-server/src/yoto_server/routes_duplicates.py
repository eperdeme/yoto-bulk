"""Duplicate detection and resolution endpoints.

Replaces the hand-edited ``scripts/cleanup_dupes.py``: the same three cases it
handled (real Yoto duplicates, unlinked pairs, stale shadows) are now detected
automatically and resolved through the UI.

Linking is non-destructive and is the fix for the common case. Deleting is left
to the existing ``DELETE /api/yoto/cards/{card_id}`` so there is one destructive
path, not two.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from yoto_server import duplicates
from yoto_server.deps import get_store, get_user_id
from yoto_server.store import YotoStore

router = APIRouter()
logger = logging.getLogger(__name__)


class LinkRequest(BaseModel):
    """The two sides of a duplicate to join."""

    abs_card_id: str = Field(min_length=1)
    yoto_card_id: str = Field(min_length=1)


@router.get("/duplicates")
async def list_duplicates(
    store: YotoStore = Depends(get_store),
    user_id: str = Depends(get_user_id),
) -> dict:
    """Books that appear more than once, with why and what to do about it."""
    groups = await duplicates.find_duplicates(store, user_id)
    return {
        "count": len(groups),
        "linkable": sum(1 for g in groups if g.resolution == duplicates.RESOLUTION_LINK),
        "groups": [g.to_dict() for g in groups],
    }


async def _require_cards(store: YotoStore, user_id: str, *card_ids: str) -> None:
    for card_id in card_ids:
        if await store.load_card(user_id, card_id) is None:
            raise HTTPException(status_code=404, detail=f"Unknown card '{card_id}'")


@router.post("/duplicates/link")
async def link_duplicate(
    payload: LinkRequest,
    store: YotoStore = Depends(get_store),
    user_id: str = Depends(get_user_id),
) -> dict:
    """Mark an ABS card and a Yoto card as the same book.

    The library then shows one entry, and a later push updates the existing Yoto
    card rather than creating yet another copy.
    """
    if payload.abs_card_id == payload.yoto_card_id:
        raise HTTPException(status_code=422, detail="A card cannot be linked to itself")
    await _require_cards(store, user_id, payload.abs_card_id, payload.yoto_card_id)

    meta = await store.get_meta(user_id, payload.abs_card_id)
    if meta.get("source") != "abs":
        raise HTTPException(
            status_code=422,
            detail=f"'{payload.abs_card_id}' is not an Audiobookshelf-backed card",
        )
    yoto_meta = await store.get_meta(user_id, payload.yoto_card_id)
    if yoto_meta.get("source") != "yoto":
        raise HTTPException(
            status_code=422,
            detail=f"'{payload.yoto_card_id}' is not a card pulled from Yoto",
        )

    await duplicates.link_pair(store, user_id, payload.abs_card_id, payload.yoto_card_id)
    return {
        "status": "linked",
        "abs_card_id": payload.abs_card_id,
        "yoto_card_id": payload.yoto_card_id,
    }


@router.post("/duplicates/unlink")
async def unlink_duplicate(
    payload: LinkRequest,
    store: YotoStore = Depends(get_store),
    user_id: str = Depends(get_user_id),
) -> dict:
    """Undo a link, for when two different books were joined by mistake."""
    await duplicates.unlink_pair(store, user_id, payload.abs_card_id, payload.yoto_card_id)
    return {
        "status": "unlinked",
        "abs_card_id": payload.abs_card_id,
        "yoto_card_id": payload.yoto_card_id,
    }
