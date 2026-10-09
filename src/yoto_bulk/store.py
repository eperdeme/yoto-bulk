"""Redis-backed store for Yoto card data, user-scoped for multi-tenancy.

Key schema::

    yoto:{user_id}:card_ids               → SET of card_id strings
    yoto:{user_id}:card:{card_id}          → JSON full card payload
    yoto:{user_id}:meta:{card_id}          → HASH {pull_hash, pulled_at, source, title, updated_at}
    yoto:{user_id}:backup:{card_id}        → LIST of JSON backup payloads (newest first)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as redis

from yoto_bulk.card_utils import (
    preserve_yoto_media,
    resolve_title,
)

logger = logging.getLogger(__name__)

MAX_BACKUPS = 20


def extract_user_id_from_jwt(access_token: str) -> str:
    """Extract the ``sub`` claim from a JWT access token without verification.

    The Yoto API issues Auth0 JWTs with a ``sub`` like
    ``auth0|66e34e35ce3fc3e1ac2e38ec``.  We use this as the multi-tenant
    user key.
    """
    try:
        payload_b64 = access_token.split(".")[1]
        # Add padding
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        decoded = json.loads(base64.urlsafe_b64decode(payload_b64))
        sub = decoded.get("sub")
        if not sub:
            raise ValueError("JWT has no 'sub' claim")
        return sub
    except (IndexError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JWT: {exc}") from exc


def content_hash(payload: dict[str, Any]) -> str:
    """SHA-256 of the JSON-serialised card payload (truncated to 16 chars).

    Must match ``yoto_bulk.library.content_hash`` for change detection
    to work with pull_hash values imported from the local library.
    """
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class YotoStore:
    """Async Redis store for Yoto card data, user-scoped for multi-tenancy."""

    def __init__(self, redis_client: redis.Redis) -> None:
        self._r = redis_client

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _card_ids_key(user_id: str) -> str:
        return f"yoto:{user_id}:card_ids"

    @staticmethod
    def _card_key(user_id: str, card_id: str) -> str:
        return f"yoto:{user_id}:card:{card_id}"

    @staticmethod
    def _meta_key(user_id: str, card_id: str) -> str:
        return f"yoto:{user_id}:meta:{card_id}"

    @staticmethod
    def _backup_key(user_id: str, card_id: str) -> str:
        return f"yoto:{user_id}:backup:{card_id}"

    # ------------------------------------------------------------------
    # Card CRUD
    # ------------------------------------------------------------------

    async def save_card(
        self,
        user_id: str,
        card_id: str,
        payload: dict[str, Any],
        *,
        update_pull_hash: bool = False,
        source: str | None = None,
        external_id: str | None = None,
    ) -> None:
        """Store a card payload and update metadata."""
        pipe = self._r.pipeline()

        # Store payload
        pipe.set(self._card_key(user_id, card_id), json.dumps(payload, ensure_ascii=False))

        # Add to card_ids set
        pipe.sadd(self._card_ids_key(user_id), card_id)

        await pipe.execute()

        # Update metadata hash
        now = datetime.now(UTC).isoformat()
        meta: dict[str, str] = {
            "title": resolve_title(payload),
            "updated_at": now,
        }
        if update_pull_hash:
            meta["pull_hash"] = content_hash(payload)
            meta["pulled_at"] = now
        if source is not None:
            meta["source"] = source
        if external_id is not None:
            meta["external_id"] = external_id

        # Use hset to merge (doesn't overwrite fields we don't specify).
        # `pull_hash` tracks sync state; it must not erase provenance fields
        # like `source` or `external_id`.
        await self._r.hset(self._meta_key(user_id, card_id), mapping=meta)

    async def load_card(self, user_id: str, card_id: str) -> dict[str, Any] | None:
        """Load a single card payload."""
        raw = await self._r.get(self._card_key(user_id, card_id))
        if raw is None:
            return None
        return json.loads(raw)

    async def load_all_cards(self, user_id: str) -> list[tuple[str, dict[str, Any]]]:
        """Load all cards for a user. Returns (card_id, payload) pairs."""
        card_ids = await self._r.smembers(self._card_ids_key(user_id))
        if not card_ids:
            return []

        # Fetch all payloads in one pipeline
        pipe = self._r.pipeline()
        sorted_ids = sorted(card_ids)
        for cid in sorted_ids:
            pipe.get(self._card_key(user_id, cid))
        results = await pipe.execute()

        cards = []
        for cid, raw in zip(sorted_ids, results):
            if raw is not None:
                cards.append((cid, json.loads(raw)))
        return cards

    async def list_card_ids(self, user_id: str) -> list[str]:
        """Return all card IDs for a user."""
        card_ids = await self._r.smembers(self._card_ids_key(user_id))
        return sorted(card_ids)

    async def get_meta(self, user_id: str, card_id: str) -> dict[str, str]:
        """Get metadata hash for a card."""
        return await self._r.hgetall(self._meta_key(user_id, card_id))

    async def get_all_meta(self, user_id: str) -> dict[str, dict[str, str]]:
        """Get metadata for all cards in one pipeline."""
        card_ids = await self._r.smembers(self._card_ids_key(user_id))
        if not card_ids:
            return {}

        pipe = self._r.pipeline()
        sorted_ids = sorted(card_ids)
        for cid in sorted_ids:
            pipe.hgetall(self._meta_key(user_id, cid))
        results = await pipe.execute()

        return {cid: meta for cid, meta in zip(sorted_ids, results) if meta}

    # ------------------------------------------------------------------
    # Change detection
    # ------------------------------------------------------------------

    async def modified_cards(
        self, user_id: str
    ) -> list[tuple[str, dict[str, Any], str]]:
        """Return (card_id, payload, title) for cards modified since last push.

        Skips imported cards (source=import).
        """
        all_meta = await self.get_all_meta(user_id)
        result: list[tuple[str, dict[str, Any], str]] = []

        # Only load cards that might be modified
        candidates = [
            cid for cid, meta in all_meta.items()
            if meta.get("source") != "import" and "pull_hash" in meta
        ]

        if not candidates:
            return result

        pipe = self._r.pipeline()
        for cid in candidates:
            pipe.get(self._card_key(user_id, cid))
        payloads = await pipe.execute()

        for cid, raw in zip(candidates, payloads):
            if raw is None:
                continue
            payload = json.loads(raw)
            meta = all_meta[cid]
            if content_hash(payload) != meta.get("pull_hash"):
                result.append((cid, payload, resolve_title(payload)))

        return result

    async def imported_cards(
        self, user_id: str
    ) -> list[tuple[str, dict[str, Any], str]]:
        """Return (card_id, payload, title) for imported cards."""
        all_meta = await self.get_all_meta(user_id)
        import_ids = [
            cid for cid, meta in all_meta.items()
            if meta.get("source") == "import"
        ]

        if not import_ids:
            return []

        pipe = self._r.pipeline()
        for cid in import_ids:
            pipe.get(self._card_key(user_id, cid))
        payloads = await pipe.execute()

        result = []
        for cid, raw in zip(import_ids, payloads):
            if raw is not None:
                payload = json.loads(raw)
                result.append((cid, payload, resolve_title(payload)))
        return result

    # ------------------------------------------------------------------
    # Backups
    # ------------------------------------------------------------------

    async def backup_card(self, user_id: str, card_id: str) -> bool:
        """Save a backup of the current card payload before pushing.

        Returns True if a backup was saved, False if the card doesn't exist.
        """
        payload = await self.load_card(user_id, card_id)
        if payload is None:
            return False

        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = json.dumps({"timestamp": ts, "payload": payload}, ensure_ascii=False)
        pipe = self._r.pipeline()
        pipe.lpush(self._backup_key(user_id, card_id), backup)
        pipe.ltrim(self._backup_key(user_id, card_id), 0, MAX_BACKUPS - 1)
        await pipe.execute()
        return True

    async def list_backups(self, user_id: str, card_id: str) -> list[dict[str, Any]]:
        """Return backups for a card (newest first)."""
        raw_list = await self._r.lrange(self._backup_key(user_id, card_id), 0, -1)
        return [json.loads(r) for r in raw_list]

    # ------------------------------------------------------------------
    # Delete / rename
    # ------------------------------------------------------------------

    async def remove_card(self, user_id: str, card_id: str) -> bool:
        """Delete a card and all its associated data."""
        exists = await self._r.sismember(self._card_ids_key(user_id), card_id)
        if not exists:
            return False

        pipe = self._r.pipeline()
        pipe.srem(self._card_ids_key(user_id), card_id)
        pipe.delete(self._card_key(user_id, card_id))
        pipe.delete(self._meta_key(user_id, card_id))
        pipe.delete(self._backup_key(user_id, card_id))
        await pipe.execute()
        return True

    async def rename_card(
        self,
        user_id: str,
        old_card_id: str,
        new_card_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Re-key a card under a new ID, preserving metadata and backups."""
        # Load old metadata
        old_meta = await self._r.hgetall(self._meta_key(user_id, old_card_id))
        old_backups = await self._r.lrange(
            self._backup_key(user_id, old_card_id), 0, -1
        )

        # Restore old metadata under new ID first, then save_card overwrites
        # with fresh pull_hash / updated_at so they aren't rolled back.
        if old_meta:
            await self._r.hset(self._meta_key(user_id, new_card_id), mapping=old_meta)

        # Save under new ID (fresh pull_hash / updated_at win)
        await self.save_card(user_id, new_card_id, payload, update_pull_hash=True)

        # Move backups
        if old_backups:
            pipe = self._r.pipeline()
            for backup in old_backups:
                pipe.rpush(self._backup_key(user_id, new_card_id), backup)
            await pipe.execute()

        # Remove old card
        await self.remove_card(user_id, old_card_id)

    # ------------------------------------------------------------------
    # Import from Audiobookshelf
    # ------------------------------------------------------------------

    async def import_from_audiobookshelf(
        self,
        user_id: str,
        items: list[tuple[str, dict[str, Any], str | int | None]],
    ) -> dict[str, Any]:
        """Bulk import ABS library items into Redis."""
        imported = 0
        updated = 0
        skipped = 0
        conflicted = 0

        for item_id, payload, updated_at in items:
            card_id = f"abs-{item_id}"
            existing_meta = await self.get_meta(user_id, card_id)
            existing_payload = await self.load_card(user_id, card_id) if existing_meta else None
            if existing_payload is not None:
                preserve_yoto_media(existing_payload, payload)
            new_hash = content_hash(payload)
            if existing_meta.get("pull_hash") == new_hash:
                skipped += 1
                continue
            if (
                existing_meta.get("pull_hash")
                and existing_payload is not None
                and content_hash(existing_payload) != existing_meta.get("pull_hash")
            ):
                conflicted += 1
                continue

            await self.save_card(user_id, card_id, payload)
            meta_update = {
                "title": resolve_title(payload),
                "updated_at": datetime.now(UTC).isoformat(),
                "pull_hash": new_hash,
                "pulled_at": datetime.now(UTC).isoformat(),
                "source": "abs",
                "external_id": item_id,
            }
            if updated_at is not None:
                meta_update["external_updated_at"] = str(updated_at)
            await self._r.hset(self._meta_key(user_id, card_id), mapping=meta_update)

            if existing_meta:
                updated += 1
            else:
                imported += 1

        return {
            "user_id": user_id,
            "imported": imported,
            "updated": updated,
            "skipped": skipped,
            "conflicted": conflicted,
            "total": len(items),
        }

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def ping(self) -> bool:
        try:
            return await self._r.ping()
        except Exception:
            return False
