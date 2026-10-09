from __future__ import annotations

import asyncio
import json

from borrowbox_server.store import Store


class _FakeRedis:
    def __init__(self) -> None:
        self.set_calls: list[tuple[str, str, int | None]] = []

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.set_calls.append((key, value, ex))


def test_current_loans_cache_uses_one_hour_ttl() -> None:
    redis_obj = _FakeRedis()
    store = Store(redis_obj, ttl_availability=3600, ttl_session=86400)  # type: ignore[arg-type]

    asyncio.run(store.set_current_loans("default", "hounslow", [{"title": "Book"}]))

    assert redis_obj.set_calls == [
        ("bb:loans:default:hounslow", json.dumps([{"title": "Book"}]), 3600)
    ]
