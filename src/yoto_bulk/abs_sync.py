"""Shared helpers for syncing Audiobookshelf items into the Redis-backed Yoto cache."""

from __future__ import annotations

import logging
from typing import Any

from yoto_bulk.audiobookshelf import build_yoto_payload_from_abs
from yoto_bulk.auth import YotoAuthService
from yoto_bulk.config import Settings as YotoBulkSettings
from yoto_bulk.store import YotoStore, extract_user_id_from_jwt

logger = logging.getLogger(__name__)


async def sync_abs_item_to_redis(store: Any, item: dict[str, Any], abs_client: Any) -> None:
    """Write an ABS item into the yoto-server Redis keyspace via YotoStore."""
    yoto_settings = YotoBulkSettings()
    auth_service = YotoAuthService(yoto_settings)
    tokens = auth_service.token_store.load()
    if tokens is None:
        logger.warning("No Yoto auth tokens — skipping Redis sync")
        return

    try:
        user_id = extract_user_id_from_jwt(tokens.access_token)
    except ValueError:
        logger.warning("Failed to decode JWT — skipping Redis sync")
        return

    item_id = item["id"]
    playback = await abs_client.get_playback_session(item_id)
    payload = build_yoto_payload_from_abs(item, playback)
    card_id = f"abs-{item_id}"

    yoto_store = YotoStore(store._r)
    await yoto_store.save_card(
        user_id=user_id,
        card_id=card_id,
        payload=payload,
        update_pull_hash=True,
        source="abs",
        external_id=item_id,
    )
    logger.info("Synced ABS item %s to Redis for user %s", item_id, user_id)
