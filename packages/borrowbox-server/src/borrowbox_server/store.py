"""Redis-backed store for BorrowBox cached data.

Key schema::

    bb:product:{library}:{product_id}       → JSON product doc (TTL: 30d)
    bb:availability:{library}:{product_id}  → JSON {status, available_date, checked_at} (TTL: 1h)
    bb:series:{library}:{series_id}         → JSON {name, product_ids[]} (TTL: 30d)
    bb:discovery:{library}:{title_hash}     → JSON {series_id, series_name} (TTL: 7d)
    bb:session:{user_id}:{library}          → JSON auth state (TTL: 24h)
    bb:loans:{user_id}:{library}            → JSON list of current loans (TTL: 1h)
    bb:loan_history:{user_id}:{library}     → JSON list of historical loans (TTL: 24h)
    bb:series_index:{library}               → SET of series_id strings (no TTL)
    bb:abs_link:{library}:{product_id}      → ABS library item id for an imported loan (no TTL)
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from typing import Any

import redis.asyncio as redis


class Store:
    """Async Redis store for BorrowBox data."""

    def __init__(
        self,
        redis_client: redis.Redis,
        *,
        ttl_availability: int = 3600,
        ttl_credits: int = 300,
        ttl_discovery: int = 604800,
        ttl_product: int = 2592000,
        ttl_session: int = 86400,
    ) -> None:
        self._r = redis_client
        self._ttl_availability = ttl_availability
        self._ttl_credits = ttl_credits
        self._ttl_discovery = ttl_discovery
        self._ttl_product = ttl_product
        self._ttl_session = ttl_session

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------

    async def set_product(
        self, library: str, product_id: str, data: dict[str, Any]
    ) -> None:
        key = f"bb:product:{library}:{product_id}"
        await self._r.set(key, json.dumps(data), ex=self._ttl_product)

    async def get_product(
        self, library: str, product_id: str
    ) -> dict[str, Any] | None:
        key = f"bb:product:{library}:{product_id}"
        raw = await self._r.get(key)
        return json.loads(raw) if raw else None

    async def set_products_bulk(
        self, library: str, products: list[dict[str, Any]]
    ) -> None:
        pipe = self._r.pipeline()
        for p in products:
            pid = p.get("product_id", "")
            key = f"bb:product:{library}:{pid}"
            pipe.set(key, json.dumps(p), ex=self._ttl_product)
        await pipe.execute()

    # ------------------------------------------------------------------
    # ABS import links (product_id -> ABS library item id)
    # ------------------------------------------------------------------

    async def set_abs_link(self, library: str, product_id: str, abs_item_id: str) -> None:
        """Record which ABS library item an imported loan produced.

        Keyed by BorrowBox ``product_id`` (stable across re-borrows) so the
        loans view can detect the book in ABS by id even after metadata edits
        change its title/author.
        """
        key = f"bb:abs_link:{library}:{product_id}"
        await self._r.set(key, json.dumps(abs_item_id))

    async def get_abs_link(self, library: str, product_id: str) -> str | None:
        key = f"bb:abs_link:{library}:{product_id}"
        raw = await self._r.get(key)
        return json.loads(raw) if raw else None

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    async def set_availability(
        self,
        library: str,
        product_id: str,
        status: str,
        available_date: str | None = None,
    ) -> None:
        key = f"bb:availability:{library}:{product_id}"
        doc = {
            "status": status,
            "available_date": available_date,
            "checked_at": time.time(),
        }
        await self._r.set(key, json.dumps(doc), ex=self._ttl_availability)

    async def get_availability(
        self, library: str, product_id: str
    ) -> dict[str, Any] | None:
        key = f"bb:availability:{library}:{product_id}"
        raw = await self._r.get(key)
        return json.loads(raw) if raw else None

    async def set_availability_bulk(
        self,
        library: str,
        items: list[dict[str, Any]],
    ) -> None:
        """Store availability for multiple products at once.

        Each item should have: product_id, status, and optionally available_date.
        """
        pipe = self._r.pipeline()
        now = time.time()
        for item in items:
            pid = item["product_id"]
            key = f"bb:availability:{library}:{pid}"
            doc = {
                "status": item["status"],
                "available_date": item.get("available_date"),
                "checked_at": now,
            }
            pipe.set(key, json.dumps(doc), ex=self._ttl_availability)
        await pipe.execute()

    async def get_availability_multi(
        self, libraries: list[str], product_id: str
    ) -> dict[str, dict[str, Any] | None]:
        """Get availability across multiple libraries for one product."""
        pipe = self._r.pipeline()
        for lib in libraries:
            pipe.get(f"bb:availability:{lib}:{product_id}")
        results = await pipe.execute()
        return {
            lib: (json.loads(raw) if raw else None)
            for lib, raw in zip(libraries, results)
        }

    # ------------------------------------------------------------------
    # Series
    # ------------------------------------------------------------------

    async def set_series(
        self,
        library: str,
        series_id: str,
        name: str,
        product_ids: list[str],
    ) -> None:
        key = f"bb:series:{library}:{series_id}"
        doc = {"name": name, "product_ids": product_ids}
        await self._r.set(key, json.dumps(doc), ex=self._ttl_product)
        # Also track in the series index
        await self._r.sadd(f"bb:series_index:{library}", series_id)

    async def get_series(
        self, library: str, series_id: str
    ) -> dict[str, Any] | None:
        key = f"bb:series:{library}:{series_id}"
        raw = await self._r.get(key)
        return json.loads(raw) if raw else None

    async def get_all_series_ids(self, library: str) -> set[str]:
        return {
            m.decode() if isinstance(m, bytes) else m
            for m in await self._r.smembers(f"bb:series_index:{library}")
        }

    # ------------------------------------------------------------------
    # Series discovery cache (title → series mapping)
    # ------------------------------------------------------------------

    @staticmethod
    def _title_key(library: str, title: str) -> str:
        h = hashlib.sha256(title.lower().strip().encode()).hexdigest()[:16]
        return f"bb:discovery:{library}:{h}"

    async def cache_discovery(
        self,
        library: str,
        title: str,
        series_id: str | None,
        series_name: str | None,
    ) -> None:
        key = self._title_key(library, title)
        doc = {"series_id": series_id, "series_name": series_name}
        await self._r.set(key, json.dumps(doc), ex=self._ttl_discovery)

    async def get_discovery(
        self, library: str, title: str
    ) -> dict[str, str | None] | None:
        key = self._title_key(library, title)
        raw = await self._r.get(key)
        return json.loads(raw) if raw else None

    async def get_discoveries_bulk(
        self, library: str, titles: list[str]
    ) -> dict[str, dict[str, str | None] | None]:
        """Look up cached discoveries for many titles at once."""
        if not titles:
            return {}
        pipe = self._r.pipeline()
        keys = [self._title_key(library, t) for t in titles]
        for k in keys:
            pipe.get(k)
        results = await pipe.execute()
        return {
            title: (json.loads(raw) if raw else None)
            for title, raw in zip(titles, results)
        }

    # ------------------------------------------------------------------
    # User sessions (BorrowBox auth state)
    # ------------------------------------------------------------------

    async def set_session(
        self, user_id: str, library: str, data: dict[str, Any]
    ) -> None:
        key = f"bb:session:{user_id}:{library}"
        # Jitter ±0-120 min so libraries don't all expire simultaneously
        jitter = random.randint(-7200, 7200)
        ttl = max(3600, self._ttl_session + jitter)
        await self._r.set(key, json.dumps(data), ex=ttl)

    async def get_session(
        self, user_id: str, library: str
    ) -> dict[str, Any] | None:
        key = f"bb:session:{user_id}:{library}"
        raw = await self._r.get(key)
        return json.loads(raw) if raw else None

    async def delete_session(self, user_id: str, library: str) -> None:
        await self._r.delete(f"bb:session:{user_id}:{library}")

    # ------------------------------------------------------------------
    # Loans
    # ------------------------------------------------------------------

    async def set_current_loans(
        self, user_id: str, library: str, loans: list[dict[str, Any]]
    ) -> None:
        key = f"bb:loans:{user_id}:{library}"
        await self._r.set(key, json.dumps(loans), ex=self._ttl_availability)

    async def get_current_loans(
        self, user_id: str, library: str
    ) -> list[dict[str, Any]] | None:
        key = f"bb:loans:{user_id}:{library}"
        raw = await self._r.get(key)
        return json.loads(raw) if raw else None

    async def set_loan_history(
        self, user_id: str, library: str, loans: list[dict[str, Any]]
    ) -> None:
        key = f"bb:loan_history:{user_id}:{library}"
        await self._r.set(key, json.dumps(loans), ex=self._ttl_session)

    async def get_loan_history(
        self, user_id: str, library: str
    ) -> list[dict[str, Any]] | None:
        key = f"bb:loan_history:{user_id}:{library}"
        raw = await self._r.get(key)
        return json.loads(raw) if raw else None

    # ------------------------------------------------------------------
    # Library registry (which libraries exist)
    # ------------------------------------------------------------------
    #
    # Configuration, not cache: no TTL. A single hash of name -> subdomain,
    # plus a marker so an intentionally emptied registry is not re-seeded from
    # the environment on the next restart.

    _LIBRARIES_KEY = "bb:libraries"
    _LIBRARIES_INIT_KEY = "bb:libraries_initialised"

    async def get_libraries(self) -> dict[str, str]:
        return await self._r.hgetall(self._LIBRARIES_KEY) or {}

    async def set_library(self, name: str, subdomain: str) -> None:
        await self._r.hset(self._LIBRARIES_KEY, name, subdomain)
        await self._r.set(self._LIBRARIES_INIT_KEY, "1")

    async def delete_library(self, name: str) -> bool:
        return bool(await self._r.hdel(self._LIBRARIES_KEY, name))

    async def libraries_initialised(self) -> bool:
        """Whether the registry has ever been populated."""
        return bool(await self._r.exists(self._LIBRARIES_INIT_KEY))

    async def mark_libraries_initialised(self) -> None:
        await self._r.set(self._LIBRARIES_INIT_KEY, "1")

    # ------------------------------------------------------------------
    # Library credentials (encrypted at rest — see credentials.py)
    # ------------------------------------------------------------------
    #
    # Deliberately unlike everything else in this class: no TTL. These are
    # configuration, not cache, so they must survive restarts. The value is an
    # opaque Fernet token; this layer never sees the plaintext.

    @staticmethod
    def _credentials_key(library: str) -> str:
        return f"bb:credentials:{library}"

    @staticmethod
    def _credential_status_key(library: str) -> str:
        return f"bb:credential_status:{library}"

    async def set_credentials_blob(self, library: str, sealed: str) -> None:
        await self._r.set(self._credentials_key(library), sealed)

    async def get_credentials_blob(self, library: str) -> str | None:
        return await self._r.get(self._credentials_key(library))

    async def delete_credentials_blob(self, library: str) -> bool:
        return bool(await self._r.delete(self._credentials_key(library)))

    async def list_credentialled_libraries(self) -> list[str]:
        """Libraries with credentials in the store.

        Uses SCAN rather than KEYS so a large keyspace is not blocked.
        """
        found: list[str] = []
        async for key in self._r.scan_iter(match="bb:credentials:*", count=100):
            found.append(str(key).split(":", 2)[-1])
        return sorted(found)

    async def set_credential_status(self, library: str, status: dict[str, Any]) -> None:
        """Last verification outcome. Kept alongside, never inside, the secret."""
        await self._r.set(self._credential_status_key(library), json.dumps(status))

    async def get_credential_status(self, library: str) -> dict[str, Any] | None:
        raw = await self._r.get(self._credential_status_key(library))
        return json.loads(raw) if raw else None

    async def delete_credential_status(self, library: str) -> None:
        await self._r.delete(self._credential_status_key(library))

    # ------------------------------------------------------------------
    # Credits
    # ------------------------------------------------------------------

    async def set_credits(
        self, user_id: str, library: str, credits: dict[str, Any]
    ) -> None:
        key = f"bb:credits:{user_id}:{library}"
        await self._r.set(key, json.dumps(credits), ex=self._ttl_credits)

    async def get_credits(
        self, user_id: str, library: str
    ) -> dict[str, Any] | None:
        key = f"bb:credits:{user_id}:{library}"
        raw = await self._r.get(key)
        return json.loads(raw) if raw else None

    # ------------------------------------------------------------------
    # Search cache
    # ------------------------------------------------------------------

    @staticmethod
    def _search_key(library: str, query: str) -> str:
        q = query.lower().strip()
        h = hashlib.sha256(q.encode()).hexdigest()[:16]
        return f"bb:search:{library}:{h}"

    async def set_search(
        self, library: str, query: str, results: list[dict[str, Any]]
    ) -> None:
        key = self._search_key(library, query)
        await self._r.set(key, json.dumps(results), ex=self._ttl_availability)

    async def get_search(
        self, library: str, query: str
    ) -> list[dict[str, Any]] | None:
        key = self._search_key(library, query)
        raw = await self._r.get(key)
        return json.loads(raw) if raw else None

    # ------------------------------------------------------------------
    # Pub/sub for availability notifications
    # ------------------------------------------------------------------

    async def publish_available(
        self, library: str, product_id: str, title: str
    ) -> None:
        """Publish when a product becomes available."""
        channel = f"bb:notify:available:{library}"
        msg = json.dumps({"product_id": product_id, "title": title, "library": library})
        await self._r.publish(channel, msg)

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    async def ping(self) -> bool:
        return await self._r.ping()
