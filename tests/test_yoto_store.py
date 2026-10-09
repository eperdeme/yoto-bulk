from __future__ import annotations

import asyncio
import json

from yoto_bulk.store import YotoStore


class _FakePipeline:
    def __init__(self, redis_obj: "_FakeRedis") -> None:
        self.redis_obj = redis_obj
        self.ops: list[tuple[str, str, str | None]] = []

    def set(self, key: str, value: str):
        self.ops.append(("set", key, value))
        return self

    def sadd(self, key: str, value: str):
        self.ops.append(("sadd", key, value))
        return self

    def get(self, key: str):
        self.ops.append(("get", key, None))
        return self

    def hgetall(self, key: str):
        self.ops.append(("hgetall", key, None))
        return self

    def srem(self, key: str, value: str):
        self.ops.append(("srem", key, value))
        return self

    def delete(self, key: str):
        self.ops.append(("delete", key, None))
        return self

    async def execute(self):
        results = []
        for op, key, value in self.ops:
            if op == "set":
                self.redis_obj.values[key] = value
                results.append(None)
            elif op == "sadd":
                self.redis_obj.sets.setdefault(key, set()).add(value)
                results.append(None)
            elif op == "get":
                results.append(self.redis_obj.values.get(key))
            elif op == "hgetall":
                results.append(dict(self.redis_obj.hashes.get(key, {})))
            elif op == "srem":
                self.redis_obj.sets.get(key, set()).discard(value)
                results.append(None)
            elif op == "delete":
                self.redis_obj.values.pop(key, None)
                self.redis_obj.hashes.pop(key, None)
                self.redis_obj.sets.pop(key, None)
                results.append(None)
        self.ops.clear()
        return results


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    async def hset(self, key: str, mapping: dict[str, str]) -> None:
        self.hashes.setdefault(key, {}).update(mapping)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    async def sismember(self, key: str, value: str) -> bool:
        return value in self.sets.get(key, set())


def test_save_card_preserves_source_and_external_id_when_updating_pull_hash() -> None:
    redis_obj = _FakeRedis()
    store = YotoStore(redis_obj)  # type: ignore[arg-type]
    payload = {
        "title": "Bunny vs Monkey",
        "metadata": {"author": "Jamie Smart"},
        "content": {"chapters": []},
    }

    asyncio.run(
        store.save_card(
            user_id="user-1",
            card_id="abs-123",
            payload=payload,
            update_pull_hash=True,
            source="abs",
            external_id="123",
        )
    )

    meta = asyncio.run(store.get_meta("user-1", "abs-123"))
    raw_payload = json.loads(redis_obj.values["yoto:user-1:card:abs-123"])

    assert raw_payload["title"] == "Bunny vs Monkey"
    assert meta["source"] == "abs"
    assert meta["external_id"] == "123"
    assert "pull_hash" in meta


def test_import_from_audiobookshelf_preserves_uploaded_media_refs() -> None:
    redis_obj = _FakeRedis()
    store = YotoStore(redis_obj)  # type: ignore[arg-type]
    existing_payload = {
        "title": "Which Way to the Future",
        "metadata": {
            "description": "Old description",
            "cover": {"imageL": "https://card-content.yotoplay.com/cover-id"},
        },
        "content": {
            "chapters": [
                {
                    "title": "Chapter 1",
                    "tracks": [
                        {
                            "title": "Chapter 1",
                            "trackUrl": "yoto:#audio-1",
                            "duration": 12,
                            "fileSize": 34,
                            "channels": "stereo",
                            "format": "mp3",
                        }
                    ],
                }
            ]
        },
    }
    refreshed_payload = {
        "title": "Which Way to the Future",
        "metadata": {"description": "New description"},
        "content": {
            "chapters": [
                {
                    "title": "Chapter 1",
                    "tracks": [
                        {
                            "title": "Chapter 1",
                            "trackUrl": "",
                            "format": "mp3",
                        }
                    ],
                }
            ]
        },
    }

    async def _run() -> tuple[dict, dict]:
        await store.save_card(
            "user-1",
            "abs-123",
            existing_payload,
            update_pull_hash=True,
            source="abs",
            external_id="123",
        )
        result = await store.import_from_audiobookshelf("user-1", [("123", refreshed_payload, "2")])
        saved = await store.load_card("user-1", "abs-123")
        assert saved is not None
        return result, saved

    result, saved = asyncio.run(_run())

    assert result["updated"] == 1
    assert saved["metadata"]["description"] == "New description"
    assert saved["metadata"]["cover"]["imageL"] == "https://card-content.yotoplay.com/cover-id"
    track = saved["content"]["chapters"][0]["tracks"][0]
    assert track["trackUrl"] == "yoto:#audio-1"
    assert track["fileSize"] == 34
