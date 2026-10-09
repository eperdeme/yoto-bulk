from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from yoto_bulk.store import YotoStore
from yoto_server import routes_sync
from yoto_server.schemas import (
    BulkSeriesUpdate,
    CardMetadataUpdate,
    ManualSeriesRequest,
    SeriesSuggestion,
)

from tests.test_yoto_store import _FakeRedis


def _request() -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace())))


def test_apply_series_updates_native_yoto_card_with_sequence(monkeypatch) -> None:
    redis_obj = _FakeRedis()
    store = YotoStore(redis_obj)  # type: ignore[arg-type]
    saved_payloads: list[dict] = []

    class FakeYotoClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def save_content(self, payload: dict) -> dict:
            saved_payloads.append(copy.deepcopy(payload))
            return {"card": payload}

    monkeypatch.setattr(routes_sync, "YotoClient", FakeYotoClient)
    monkeypatch.setattr(routes_sync, "YotoAuthService", lambda settings: object())

    async def _run():
        await store.save_card(
            "user-1",
            "yoto-1",
            {"cardId": "yoto-1", "metadata": {"title": "Hilda"}, "content": {"chapters": []}},
            update_pull_hash=True,
            source="yoto",
        )
        return await routes_sync.apply_series_suggestions(
            request=_request(),
            update=BulkSeriesUpdate(
                suggestions=[
                    SeriesSuggestion(
                        series_name="Hilda",
                        card_ids=["yoto-1"],
                        sequence_numbers={"yoto-1": 5},
                    )
                ]
            ),
            store=store,
            user_id="user-1",
        )

    response = asyncio.run(_run())
    cached = asyncio.run(store.load_card("user-1", "yoto-1"))
    meta = asyncio.run(store.get_meta("user-1", "yoto-1"))

    assert response.count == 1
    assert response.failed == []
    assert saved_payloads[0]["metadata"]["series"] == "Hilda"
    assert saved_payloads[0]["metadata"]["sequence_number"] == 5
    assert cached["metadata"]["series"] == "Hilda"
    assert cached["metadata"]["sequence_number"] == 5
    assert meta["source"] == "yoto"


def test_apply_series_updates_abs_card_with_sequence(monkeypatch) -> None:
    redis_obj = _FakeRedis()
    store = YotoStore(redis_obj)  # type: ignore[arg-type]

    class FakeAbsClient:
        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.closed = False

        async def update_item_metadata(self, item_id: str, **kwargs) -> dict:
            self.calls.append({"item_id": item_id, **kwargs})
            return {
                "id": item_id,
                "title": "Northern Lights",
                "media": {
                    "metadata": {
                        "title": "Northern Lights",
                        "series": [
                            {
                                "name": kwargs["series"],
                                "sequence": str(kwargs["sequence"]),
                            }
                        ],
                    }
                },
            }

        async def get_playback_session(self, item_id: str) -> dict:
            return {"audioTracks": []}

        async def aclose(self) -> None:
            self.closed = True

    fake_abs = FakeAbsClient()
    monkeypatch.setattr(routes_sync, "get_abs_client", lambda request: fake_abs)

    async def _run():
        await store.save_card(
            "user-1",
            "abs-1",
            {"metadata": {"title": "Northern Lights"}, "content": {"chapters": []}},
            update_pull_hash=True,
            source="abs",
            external_id="abs-item-1",
        )
        return await routes_sync.apply_series_suggestions(
            request=_request(),
            update=BulkSeriesUpdate(
                suggestions=[
                    SeriesSuggestion(
                        series_name="His Dark Materials",
                        card_ids=["abs-1"],
                        sequence_numbers={"abs-1": 1},
                    )
                ]
            ),
            store=store,
            user_id="user-1",
        )

    response = asyncio.run(_run())
    cached = asyncio.run(store.load_card("user-1", "abs-1"))

    assert response.count == 1
    assert fake_abs.calls == [
        {
            "item_id": "abs-item-1",
            "series": "His Dark Materials",
            "sequence": 1,
        }
    ]
    assert fake_abs.closed is True
    assert cached["metadata"]["series"] == "His Dark Materials"
    assert cached["metadata"]["sequence_number"] == 1


def test_update_native_yoto_card_can_clear_series_fields(monkeypatch) -> None:
    redis_obj = _FakeRedis()
    store = YotoStore(redis_obj)  # type: ignore[arg-type]
    saved_payloads: list[dict] = []

    class FakeYotoClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def save_content(self, payload: dict) -> dict:
            saved_payloads.append(copy.deepcopy(payload))
            return {"card": payload}

    monkeypatch.setattr(routes_sync, "YotoClient", FakeYotoClient)
    monkeypatch.setattr(routes_sync, "YotoAuthService", lambda settings: object())

    async def _run():
        await store.save_card(
            "user-1",
            "yoto-1",
            {
                "cardId": "yoto-1",
                "metadata": {
                    "title": "Standalone",
                    "series": "Old Series",
                    "sequence_number": 3,
                },
                "content": {"chapters": []},
            },
            update_pull_hash=True,
            source="yoto",
        )
        return await routes_sync.update_card_metadata(
            card_id="yoto-1",
            update=CardMetadataUpdate(series="", sequence_number=None),
            request=_request(),
            store=store,
            user_id="user-1",
        )

    response = asyncio.run(_run())
    cached = asyncio.run(store.load_card("user-1", "yoto-1"))

    assert response == {"status": "updated", "card_id": "yoto-1"}
    assert "series" not in saved_payloads[0]["metadata"]
    assert "sequence_number" not in saved_payloads[0]["metadata"]
    assert "series" not in cached["metadata"]
    assert "sequence_number" not in cached["metadata"]


def test_apply_series_skips_linked_yoto_shadow_cards(monkeypatch) -> None:
    redis_obj = _FakeRedis()
    store = YotoStore(redis_obj)  # type: ignore[arg-type]

    class FakeYotoClient:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("linked Yoto shadow cards should not be pushed directly")

    monkeypatch.setattr(routes_sync, "YotoClient", FakeYotoClient)

    async def _run():
        await store.save_card(
            "user-1",
            "abs-1",
            {"metadata": {"title": "Book"}, "content": {"chapters": []}},
            update_pull_hash=True,
            source="abs",
            external_id="abs-item-1",
        )
        await store.save_card(
            "user-1",
            "yoto-1",
            {"cardId": "yoto-1", "metadata": {"title": "Book"}, "content": {"chapters": []}},
            update_pull_hash=True,
            source="yoto",
        )
        await redis_obj.hset(
            store._meta_key("user-1", "yoto-1"),
            mapping={"linked_abs_card_id": "abs-1"},
        )
        return await routes_sync.apply_series_suggestions(
            request=_request(),
            update=BulkSeriesUpdate(
                suggestions=[
                    SeriesSuggestion(
                        series_name="Series",
                        card_ids=["yoto-1"],
                        sequence_numbers={"yoto-1": 1},
                    )
                ]
            ),
            store=store,
            user_id="user-1",
        )

    response = asyncio.run(_run())

    assert response.count == 0
    assert response.skipped_yoto_source == ["yoto-1"]


def test_update_linked_yoto_shadow_card_is_rejected() -> None:
    redis_obj = _FakeRedis()
    store = YotoStore(redis_obj)  # type: ignore[arg-type]

    async def _run():
        await store.save_card(
            "user-1",
            "yoto-1",
            {"cardId": "yoto-1", "metadata": {"title": "Book"}, "content": {"chapters": []}},
            update_pull_hash=True,
            source="yoto",
        )
        await redis_obj.hset(
            store._meta_key("user-1", "yoto-1"),
            mapping={"linked_abs_card_id": "abs-1"},
        )
        return await routes_sync.update_card_metadata(
            card_id="yoto-1",
            update=CardMetadataUpdate(series="Series", sequence_number=1),
            request=_request(),
            store=store,
            user_id="user-1",
        )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_run())

    assert exc_info.value.status_code == 409


def test_sequence_numbers_for_group_preserves_existing_metadata() -> None:
    assert routes_sync._sequence_numbers_for_group(
        ["a", "b"],
        {
            "a": {"metadata": {"sequence_number": 7}},
            "b": {"metadata": {}},
        },
    ) == {"a": 7}


def test_sequence_numbers_for_group_does_not_invent_title_order() -> None:
    assert routes_sync._sequence_numbers_for_group(
        ["104", "26", "143", "130", "52", "91", "39", "169", "156", "13", "tales", "65", "117", "78"],
        {
            "104": {"title": "The 104-Storey Treehouse", "metadata": {}},
            "26": {"title": "The 26-Storey Treehouse", "metadata": {}},
            "143": {"title": "The 143-Storey Treehouse", "metadata": {}},
            "130": {"title": "The 130-Storey Treehouse", "metadata": {}},
            "52": {"title": "The 52-Storey Treehouse", "metadata": {}},
            "91": {"title": "The 91-Storey Treehouse", "metadata": {}},
            "39": {"title": "The 39-Storey Treehouse", "metadata": {}},
            "169": {"title": "The 169-Storey Treehouse", "metadata": {}},
            "156": {"title": "The 156-Storey Treehouse", "metadata": {}},
            "13": {"title": "The 13-Storey Treehouse", "metadata": {}},
            "tales": {"title": "Treehouse Tales: Too Silly to Be Told ... Until No", "metadata": {}},
            "65": {"title": "The 65-Storey Treehouse", "metadata": {}},
            "117": {"title": "The 117-Storey Treehouse", "metadata": {}},
            "78": {"title": "The 78-Storey Treehouse", "metadata": {}},
        },
    ) == {}


def _discover_request() -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(openai_api_key=None, abs_library_name=None)
            )
        )
    )


async def _seed_residue_cards(store: YotoStore) -> None:
    await store.save_card(
        "user-1",
        "yoto-1",
        {"cardId": "yoto-1", "metadata": {"title": "The Wizards of Once"}, "content": {"chapters": []}},
        update_pull_hash=True,
        source="yoto",
    )
    await store.save_card(
        "user-1",
        "yoto-2",
        {"cardId": "yoto-2", "metadata": {"title": "Knock Three Times"}, "content": {"chapters": []}},
        update_pull_hash=True,
        source="yoto",
    )


def test_discover_series_returns_manual_prompt_when_no_openai() -> None:
    store = YotoStore(_FakeRedis())  # type: ignore[arg-type]

    async def _run():
        await _seed_residue_cards(store)
        return await routes_sync.discover_series(
            request=_discover_request(), store=store, user_id="user-1",
        )

    response = asyncio.run(_run())

    assert response.used_llm is False
    assert response.llm_group_count == 0
    assert response.manual_prompt is not None
    assert "The Wizards of Once" in response.manual_prompt
    assert "Knock Three Times" in response.manual_prompt
    assert set(response.unmatched_card_ids) == {"yoto-1", "yoto-2"}


def test_discover_series_manual_maps_pasted_grouping_to_cards() -> None:
    store = YotoStore(_FakeRedis())  # type: ignore[arg-type]
    pasted = (
        "```json\n"
        '{"series": [{"series_name": "The Wizards of Once", '
        '"titles": ["The Wizards of Once", "Knock Three Times"]}], '
        '"unmatched": []}\n'
        "```"
    )

    async def _run():
        await _seed_residue_cards(store)
        return await routes_sync.discover_series_manual(
            payload=ManualSeriesRequest(response=pasted),
            request=_discover_request(),
            store=store,
            user_id="user-1",
        )

    response = asyncio.run(_run())

    assert response.used_llm is True
    assert response.llm_group_count == 1
    assert len(response.series) == 1
    group = response.series[0]
    assert group.series_name == "The Wizards of Once"
    assert set(group.card_ids) == {"yoto-1", "yoto-2"}
    assert group.provenance == "llm"
    assert response.unmatched_card_ids == []


def test_discover_series_manual_rejects_unparseable_response() -> None:
    store = YotoStore(_FakeRedis())  # type: ignore[arg-type]

    async def _run():
        await _seed_residue_cards(store)
        return await routes_sync.discover_series_manual(
            payload=ManualSeriesRequest(response="not json at all"),
            request=_discover_request(),
            store=store,
            user_id="user-1",
        )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_run())
    assert exc_info.value.status_code == 400


def test_abs_series_info_from_entry_extracts_nested_books() -> None:
    info = routes_sync._abs_series_info_from_entry(
        {
            "id": "series-1",
            "name": "Tom Gates",
            "books": [
                {
                    "libraryItem": {
                        "id": "abs-1",
                        "media": {
                            "metadata": {
                                "title": "The Brilliant World of Tom Gates",
                                "authors": ["Liz Pichon"],
                                "series": [{"name": "Tom Gates", "sequence": "1"}],
                            }
                        },
                    }
                }
            ],
        }
    )

    assert info is not None
    assert info.series_id == "series-1"
    assert info.name == "Tom Gates"
    assert len(info.items) == 1
    assert info.items[0].title == "The Brilliant World of Tom Gates"
    assert info.items[0].authors == ["Liz Pichon"]
    assert info.items[0].series_number == 1
    assert info.items[0].abs_item_id == "abs-1"


def test_find_abs_series_uses_normalized_partial_match() -> None:
    catalog = [
        routes_sync.SeriesCatalogInfo(series_id="series-1", name="The World of Tom Gates", items=[]),
    ]

    match = routes_sync._find_abs_series(catalog, "Tom Gates")

    assert match is catalog[0]


def test_provider_series_catalog_extracts_audible_series_entries() -> None:
    catalog = routes_sync._series_catalog_from_provider_results(
        [
            {
                "title": "Harry Potter and the Philosopher's Stone",
                "author": "J.K. Rowling",
                "asin": "B017V4IMVQ",
                "cover": "https://example.test/hp1.jpg",
                "series": [{"series": "Harry Potter", "sequence": "Book 1"}],
            },
            {
                "title": "Quidditch Through the Ages",
                "author": "J.K. Rowling",
                "asin": "B000OTHER",
                "series": [{"series": "Hogwarts Library", "sequence": "1"}],
            },
        ],
        requested_name="Harry Potter",
        provider="audible.uk",
    )

    assert catalog.source == "abs_provider"
    assert catalog.name == "Harry Potter"
    assert len(catalog.items) == 1
    assert catalog.items[0].title == "Harry Potter and the Philosopher's Stone"
    assert catalog.items[0].authors == ["J.K. Rowling"]
    assert catalog.items[0].series_number == 1
    assert catalog.items[0].provider == "audible.uk"
    assert catalog.items[0].provider_id == "B017V4IMVQ"
    assert catalog.items[0].cover_url == "https://example.test/hp1.jpg"


def test_provider_series_catalog_preserves_decimal_sequence() -> None:
    catalog = routes_sync._series_catalog_from_provider_results(
        [
            {
                "title": "A Novella",
                "author": "Author",
                "series": [{"series": "Example Series", "sequence": "1.5"}],
            }
        ],
        requested_name="Example Series",
        provider="audible",
    )

    assert catalog.items[0].series_number == 1.5


def test_provider_series_catalog_dedupes_same_sequence() -> None:
    first = routes_sync.SeriesCatalogItem(
        title="Harry Potter and the Philosopher's Stone",
        series_name="Harry Potter",
        series_number=1,
        source="abs_provider",
        provider="audible.uk",
    )
    regional_duplicate = routes_sync.SeriesCatalogItem(
        title="Harry Potter and the Sorcerer's Stone",
        series_name="Harry Potter",
        series_number=1,
        source="abs_provider",
        provider="audible",
    )

    assert routes_sync._dedupe_series_catalog_items([first, regional_duplicate]) == [first]


class _RecordingRedis:
    """Minimal async Redis stub that records set() calls and their TTLs."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.set_calls: list[tuple[str, int | None]] = []

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value
        self.set_calls.append((key, ex))


class _StubABSClient:
    def __init__(self, *, results=None, error: Exception | None = None) -> None:
        self._results = results or []
        self._error = error

    async def search_metadata_books(self, *, title, author=None, provider="audible.uk"):
        if self._error is not None:
            raise self._error
        return list(self._results)

    async def aclose(self) -> None:
        return None


def test_provider_series_not_cached_when_all_providers_fail(monkeypatch) -> None:
    redis_obj = _RecordingRedis()
    store = YotoStore(redis_obj)  # type: ignore[arg-type]
    monkeypatch.setattr(
        routes_sync,
        "get_abs_client",
        lambda request: _StubABSClient(error=RuntimeError("403 Forbidden")),
    )

    result = asyncio.run(
        routes_sync._load_provider_series_catalog(
            _request(), store, name="Tom Gates", author="Liz Pichon", provider="audible.uk"
        )
    )

    # A transient provider failure must not poison the cache with an empty result.
    assert result.items == []
    assert redis_obj.set_calls == []


def test_provider_series_caches_empty_when_provider_succeeds_with_no_match(monkeypatch) -> None:
    redis_obj = _RecordingRedis()
    store = YotoStore(redis_obj)  # type: ignore[arg-type]
    monkeypatch.setattr(
        routes_sync,
        "get_abs_client",
        lambda request: _StubABSClient(results=[]),
    )

    result = asyncio.run(
        routes_sync._load_provider_series_catalog(
            _request(), store, name="Tom Gates", author="Liz Pichon", provider="audible.uk"
        )
    )

    # A genuine "provider searched, found nothing" is cached with the miss TTL.
    assert result.items == []
    assert len(redis_obj.set_calls) == 1
    assert redis_obj.set_calls[0][1] == routes_sync._PROVIDER_SERIES_CACHE_MISS_TTL_SECONDS


def test_provider_series_caches_hits_with_long_ttl(monkeypatch) -> None:
    redis_obj = _RecordingRedis()
    store = YotoStore(redis_obj)  # type: ignore[arg-type]
    monkeypatch.setattr(
        routes_sync,
        "get_abs_client",
        lambda request: _StubABSClient(
            results=[
                {
                    "title": "Tom Gates: The Brilliant World of Tom Gates",
                    "author": "Liz Pichon",
                    "asin": "B00EXAMPLE",
                    "series": [{"series": "Tom Gates", "sequence": "1"}],
                }
            ]
        ),
    )

    result = asyncio.run(
        routes_sync._load_provider_series_catalog(
            _request(), store, name="Tom Gates", author="Liz Pichon", provider="audible.uk"
        )
    )

    assert len(result.items) == 1
    assert len(redis_obj.set_calls) == 1
    assert redis_obj.set_calls[0][1] == routes_sync._PROVIDER_SERIES_CACHE_HIT_TTL_SECONDS


def test_sync_prunes_orphaned_abs_cards(monkeypatch) -> None:
    redis_obj = _FakeRedis()
    store = YotoStore(redis_obj)  # type: ignore[arg-type]

    class FakePruneAbsClient:
        def __init__(self) -> None:
            self.closed = False

        async def get_book_library(self, preferred_name=None):
            return {"id": "lib-1"}

        async def get_podcast_libraries(self):
            return []

        async def list_library_items(self, library_id):
            # Only one of the three cached ABS items still exists.
            return [{"id": "live-item", "updatedAt": "v1"}]

        async def aclose(self):
            self.closed = True

    fake_abs = FakePruneAbsClient()
    monkeypatch.setattr(routes_sync, "get_abs_client", lambda request: fake_abs)

    req = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(abs_library_name=None)))
    )

    async def _run():
        # One live (unchanged) ABS card + two orphaned ABS cards whose items
        # were deleted from Audiobookshelf.
        await store.save_card(
            "user-1",
            "abs-keep",
            {"metadata": {"title": "Keeper"}, "content": {"chapters": []}},
            update_pull_hash=True,
            source="abs",
            external_id="live-item",
        )
        await redis_obj.hset(store._meta_key("user-1", "abs-keep"), mapping={"external_updated_at": "v1"})
        for cid, ext in (("abs-orphan-1", "dead-1"), ("abs-orphan-2", "dead-2")):
            await store.save_card(
                "user-1",
                cid,
                {"metadata": {"title": "Orphan"}, "content": {"chapters": []}},
                update_pull_hash=True,
                source="abs",
                external_id=ext,
            )
        return await routes_sync.sync_from_audiobookshelf(request=req, store=store, user_id="user-1")

    result = asyncio.run(_run())

    assert result["pruned"] == 2
    assert asyncio.run(store.list_card_ids("user-1")) == ["abs-keep"]
    assert fake_abs.closed is True


def test_sync_reconciles_even_when_nothing_needs_importing(monkeypatch) -> None:
    """A steady-state sync must still link ABS cards to pulled Yoto cards.

    Reconciliation used to sit behind an early return taken whenever there was
    nothing to import, so once a library settled — the normal case — duplicates
    were never linked no matter how often you synced.
    """
    redis_obj = _FakeRedis()
    store = YotoStore(redis_obj)  # type: ignore[arg-type]

    class FakeUnchangedAbsClient:
        async def get_book_library(self, preferred_name=None):
            return {"id": "lib-1"}

        async def get_podcast_libraries(self):
            return []

        async def list_library_items(self, library_id):
            # Unchanged: same updatedAt as the cached card, so nothing is fetched.
            return [{"id": "live-item", "updatedAt": "v1"}]

        async def get_item(self, item_id):  # pragma: no cover - must not be called
            raise AssertionError("unchanged items must not be re-fetched")

        async def aclose(self):
            return None

    monkeypatch.setattr(routes_sync, "get_abs_client", lambda request: FakeUnchangedAbsClient())
    req = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(abs_library_name=None)))
    )

    async def _run():
        # An ABS card and a matching pulled Yoto card that are not yet linked.
        book = {
            "metadata": {"title": "The BFG", "author": "Roald Dahl"},
            "content": {"chapters": []},
        }
        await store.save_card(
            "user-1", "abs-bfg", book, update_pull_hash=True, source="abs", external_id="live-item"
        )
        await redis_obj.hset(
            store._meta_key("user-1", "abs-bfg"), mapping={"external_updated_at": "v1"}
        )
        await store.save_card("user-1", "yoto-bfg", book, update_pull_hash=True, source="yoto")
        return await routes_sync.sync_from_audiobookshelf(
            request=req, store=store, user_id="user-1"
        )

    result = asyncio.run(_run())

    # Nothing to import, but the pair must have been linked.
    assert result["imported"] == 0
    assert result["updated"] == 0
    assert result["unchanged"] == 1
    assert result["linked"] == 1

    abs_meta = asyncio.run(store.get_meta("user-1", "abs-bfg"))
    yoto_meta = asyncio.run(store.get_meta("user-1", "yoto-bfg"))
    assert abs_meta.get("yoto_card_id") == "yoto-bfg"
    assert yoto_meta.get("linked_abs_card_id") == "abs-bfg"


def test_sync_response_shape_is_the_same_with_and_without_imports(monkeypatch) -> None:
    """Clients read `linked` and `ambiguous`; they must always be present."""
    redis_obj = _FakeRedis()
    store = YotoStore(redis_obj)  # type: ignore[arg-type]

    class FakeEmptyAbsClient:
        async def get_book_library(self, preferred_name=None):
            return {"id": "lib-1"}

        async def get_podcast_libraries(self):
            return []

        async def list_library_items(self, library_id):
            return []

        async def aclose(self):
            return None

    monkeypatch.setattr(routes_sync, "get_abs_client", lambda request: FakeEmptyAbsClient())
    req = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(abs_library_name=None)))
    )

    result = asyncio.run(
        routes_sync.sync_from_audiobookshelf(request=req, store=store, user_id="user-1")
    )

    for key in (
        "imported",
        "updated",
        "skipped",
        "conflicted",
        "total",
        "unchanged",
        "fetch_failed",
        "no_audio",
        "linked",
        "ambiguous",
        "pruned",
    ):
        assert key in result, f"missing {key}"


def test_sync_imports_downloaded_episodes_from_podcast_libraries(monkeypatch) -> None:
    redis_obj = _FakeRedis()
    store = YotoStore(redis_obj)  # type: ignore[arg-type]

    class FakePodcastAbsClient:
        async def get_book_library(self, preferred_name=None):
            return {"id": "books"}

        async def get_podcast_libraries(self):
            return [{"id": "podcasts"}]

        async def list_library_items(self, library_id):
            if library_id == "books":
                return []
            return [{"id": "podcast-1", "updatedAt": "v1"}]

        async def get_item(self, item_id):
            return {
                "id": item_id,
                "updatedAt": "v1",
                "media": {
                    "metadata": {"title": "The Test Show", "author": "Test Author"},
                    "episodes": [
                        {
                            "id": "episode-1",
                            "title": "First episode",
                            "duration": 12,
                            "audioTrack": {"contentUrl": "/api/items/podcast-1/file/1"},
                        }
                    ],
                },
            }

        async def aclose(self):
            return None

    monkeypatch.setattr(routes_sync, "get_abs_client", lambda request: FakePodcastAbsClient())
    req = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(abs_library_name=None)))
    )

    result = asyncio.run(
        routes_sync.sync_from_audiobookshelf(request=req, store=store, user_id="user-1")
    )
    card = asyncio.run(store.load_card("user-1", "abs-podcast-1"))

    assert result["imported"] == 1
    assert result["total"] == 1
    assert card is not None
    assert card["metadata"]["category"] == "podcast"
    assert [chapter["key"] for chapter in card["content"]["chapters"]] == ["episode-1"]
