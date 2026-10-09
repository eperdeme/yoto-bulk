from __future__ import annotations

import asyncio

from yoto_bulk.store import YotoStore
from yoto_server.reconcile import find_yoto_match_for_abs, reconcile_abs_yoto_links
from yoto_server.routes import _should_hide_from_library

from tests.test_yoto_store import _FakeRedis


def test_reconcile_links_abs_card_to_existing_yoto_card() -> None:
    redis_obj = _FakeRedis()
    store = YotoStore(redis_obj)  # type: ignore[arg-type]

    abs_payload = {
        "metadata": {
            "title": "Amelia Fang And The Barbaric Ball",
            "author": "Laura Ellen Anderson",
            "series": "Amelia Fang",
            "sequence_number": 2,
        },
        "content": {"chapters": [{"title": "One"}]},
    }
    yoto_payload = {
        "cardId": "yoto-123",
        "metadata": {
            "title": "Amelia Fang And The Barbaric Ball",
            "author": "Laura Ellen Anderson",
            "series": "Amelia Fang",
            "sequence_number": 2,
        },
        "content": {"chapters": [{"title": "One"}]},
    }

    async def _run() -> tuple[dict[str, str], dict[str, str], dict[str, int]]:
        await store.save_card("user-1", "abs-abc", abs_payload, update_pull_hash=True, source="abs", external_id="abc")
        await store.save_card("user-1", "yoto-123", yoto_payload, update_pull_hash=True, source="yoto")
        result = await reconcile_abs_yoto_links(store, "user-1")
        abs_meta = await store.get_meta("user-1", "abs-abc")
        yoto_meta = await store.get_meta("user-1", "yoto-123")
        return abs_meta, yoto_meta, result

    abs_meta, yoto_meta, result = asyncio.run(_run())

    assert result["linked"] == 1
    assert abs_meta["yoto_card_id"] == "yoto-123"
    assert yoto_meta["linked_abs_card_id"] == "abs-abc"


def test_reconcile_skips_conflicting_author_matches() -> None:
    redis_obj = _FakeRedis()
    store = YotoStore(redis_obj)  # type: ignore[arg-type]

    abs_payload = {
        "metadata": {"title": "Shared Title", "author": "Author One"},
        "content": {"chapters": []},
    }
    yoto_payload = {
        "cardId": "yoto-999",
        "metadata": {"title": "Shared Title", "author": "Author Two"},
        "content": {"chapters": []},
    }

    async def _run() -> tuple[dict[str, str], dict[str, str], dict[str, int]]:
        await store.save_card("user-1", "abs-xyz", abs_payload, update_pull_hash=True, source="abs", external_id="xyz")
        await store.save_card("user-1", "yoto-999", yoto_payload, update_pull_hash=True, source="yoto")
        result = await reconcile_abs_yoto_links(store, "user-1")
        abs_meta = await store.get_meta("user-1", "abs-xyz")
        yoto_meta = await store.get_meta("user-1", "yoto-999")
        return abs_meta, yoto_meta, result

    abs_meta, yoto_meta, result = asyncio.run(_run())

    assert result["linked"] == 0
    assert "yoto_card_id" not in abs_meta
    assert "linked_abs_card_id" not in yoto_meta


def test_hide_linked_yoto_cards_from_library_listing() -> None:
    assert _should_hide_from_library(
        "yoto-123",
        {"source": "yoto", "linked_abs_card_id": "abs-abc"},
        {"abs-abc", "yoto-123"},
    ) is True
    assert _should_hide_from_library(
        "yoto-123",
        {"source": "yoto"},
        {"yoto-123"},
    ) is False


def test_reconcile_substring_title_with_same_author() -> None:
    """BorrowBox may return 'Holiday Heatwave' while ABS has
    'Hotel Flamingo: Holiday Heatwave'. Reconciliation should match
    via substring when author also matches."""
    redis_obj = _FakeRedis()
    store = YotoStore(redis_obj)  # type: ignore[arg-type]

    abs_payload = {
        "metadata": {
            "title": "Hotel Flamingo: Holiday Heatwave",
            "author": "Alex Milway",
            "series": "Hotel Flamingo",
            "sequence_number": 2,
        },
        "content": {"chapters": [{"title": "Ch1"}, {"title": "Ch2"}]},
    }
    yoto_payload = {
        "cardId": "yoto-heatwave",
        "metadata": {
            "title": "Holiday Heatwave",
            "author": "Alex Milway",
        },
        "content": {"chapters": [{"title": "Ch1"}, {"title": "Ch2"}]},
    }

    async def _run() -> tuple[dict[str, str], dict[str, str], dict[str, int]]:
        await store.save_card("user-1", "abs-hf2", abs_payload, update_pull_hash=True, source="abs", external_id="hf2")
        await store.save_card("user-1", "yoto-heatwave", yoto_payload, update_pull_hash=True, source="yoto")
        result = await reconcile_abs_yoto_links(store, "user-1")
        abs_meta = await store.get_meta("user-1", "abs-hf2")
        yoto_meta = await store.get_meta("user-1", "yoto-heatwave")
        return abs_meta, yoto_meta, result

    abs_meta, yoto_meta, result = asyncio.run(_run())

    assert result["linked"] == 1
    assert abs_meta["yoto_card_id"] == "yoto-heatwave"
    assert yoto_meta["linked_abs_card_id"] == "abs-hf2"


def test_reconcile_substring_without_author_does_not_match() -> None:
    """Substring title match alone (no shared author) should NOT link."""
    redis_obj = _FakeRedis()
    store = YotoStore(redis_obj)  # type: ignore[arg-type]

    abs_payload = {
        "metadata": {"title": "The Great Adventure: Lost World"},
        "content": {"chapters": []},
    }
    yoto_payload = {
        "cardId": "yoto-lw",
        "metadata": {"title": "Lost World"},
        "content": {"chapters": []},
    }

    async def _run() -> dict[str, int]:
        await store.save_card("user-1", "abs-ga", abs_payload, update_pull_hash=True, source="abs", external_id="ga")
        await store.save_card("user-1", "yoto-lw", yoto_payload, update_pull_hash=True, source="yoto")
        return await reconcile_abs_yoto_links(store, "user-1")

    result = asyncio.run(_run())
    assert result["linked"] == 0


def test_find_yoto_match_for_abs_returns_match() -> None:
    """The single-card lookup used by push_card to prevent duplicates."""
    redis_obj = _FakeRedis()
    store = YotoStore(redis_obj)  # type: ignore[arg-type]

    abs_payload = {
        "metadata": {
            "title": "Excellent Excuses (and Other Good Stuff)",
            "author": "Andy Stanton",
        },
        "content": {"chapters": [{"title": "One"}]},
    }
    yoto_payload = {
        "cardId": "yoto-ee",
        "metadata": {
            "title": "Excellent Excuses (and Other Good Stuff)",
            "author": "Andy Stanton",
        },
        "content": {"chapters": [{"title": "One"}]},
    }

    async def _run() -> str | None:
        await store.save_card("user-1", "abs-ee", abs_payload, update_pull_hash=True, source="abs", external_id="ee")
        await store.save_card("user-1", "yoto-ee", yoto_payload, update_pull_hash=True, source="yoto")
        # Note: reconcile NOT run — we want to verify the standalone lookup works
        return await find_yoto_match_for_abs(store, "user-1", "abs-ee")

    assert asyncio.run(_run()) == "yoto-ee"


def test_find_yoto_match_for_abs_returns_none_when_no_match() -> None:
    redis_obj = _FakeRedis()
    store = YotoStore(redis_obj)  # type: ignore[arg-type]

    abs_payload = {
        "metadata": {"title": "Unique ABS Title", "author": "Some Author"},
        "content": {"chapters": []},
    }
    yoto_payload = {
        "cardId": "yoto-other",
        "metadata": {"title": "Completely Different Book", "author": "Other Author"},
        "content": {"chapters": []},
    }

    async def _run() -> str | None:
        await store.save_card("user-1", "abs-uniq", abs_payload, update_pull_hash=True, source="abs", external_id="uniq")
        await store.save_card("user-1", "yoto-other", yoto_payload, update_pull_hash=True, source="yoto")
        return await find_yoto_match_for_abs(store, "user-1", "abs-uniq")

    assert asyncio.run(_run()) is None
