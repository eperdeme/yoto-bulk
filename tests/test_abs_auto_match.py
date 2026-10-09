from __future__ import annotations

import asyncio
from typing import Any

from yoto_bulk.audiobookshelf import (
    _DEFAULT_MATCH_FIELDS,
    ABSClient,
    _match_result_to_metadata,
)


def _run(coro):
    return asyncio.run(coro)


class _RecordingClient(ABSClient):
    """ABSClient with network calls stubbed for unit-testing auto-match."""

    def __init__(self, results_by_provider: dict[str, list[dict[str, Any]]]) -> None:
        super().__init__(base_url="http://abs.test", token="tok")
        self._results_by_provider = results_by_provider
        self.searched: list[tuple[str, str | None, str]] = []
        self.patched: list[dict[str, Any]] = []

    async def search_metadata_books(  # type: ignore[override]
        self, *, title, author=None, provider="audible.uk", item_id=None
    ):
        self.searched.append((provider, item_id, title))
        return self._results_by_provider.get(provider, [])

    async def _request(self, method, path, **kwargs):  # type: ignore[override]
        if method == "PATCH":
            self.patched.append({"path": path, "json": kwargs.get("json")})
        return {}


async def _auto_match(results, **kwargs):
    client = _RecordingClient(results)
    try:
        result = await client.auto_match_item(**kwargs)
        return result, list(client.searched), list(client.patched)
    finally:
        await client.aclose()


# ------------------------------------------------------------------
# _match_result_to_metadata
# ------------------------------------------------------------------


def test_match_result_to_metadata_maps_and_skips_blanks() -> None:
    book = {
        "title": "Welcome to Dead House",
        "subtitle": "",
        "description": "Classic Goosebumps!",
        "narrator": "Tara Sands",
        "publishedYear": "2015",
        "publisher": "Scholastic",
        "genres": ["Horror", "  "],
        "language": "",
        "series": [{"series": "Goosebumps", "sequence": "1"}],
    }

    meta = _match_result_to_metadata(book, _DEFAULT_MATCH_FIELDS)

    assert meta["description"] == "Classic Goosebumps!"
    assert meta["narrators"] == ["Tara Sands"]
    assert meta["publishedYear"] == "2015"
    assert meta["publisher"] == "Scholastic"
    assert meta["genres"] == ["Horror"]  # blank genre dropped
    assert "language" not in meta  # blank skipped, not wiped
    assert "subtitle" not in meta  # blank skipped
    assert "series" not in meta  # not a default (curated) field


def test_match_result_to_metadata_series_uses_abs_shape() -> None:
    book = {"title": "x", "series": [{"series": "Goosebumps", "sequence": "1"}]}

    meta = _match_result_to_metadata(book, ("series",))

    assert meta["series"] == [{"name": "Goosebumps", "sequence": "1"}]


# ------------------------------------------------------------------
# auto_match_item
# ------------------------------------------------------------------


def test_auto_match_applies_high_confidence_result() -> None:
    results = {
        "audible.uk": [
            {"title": "Wrong Book", "matchConfidence": 0.30, "description": "nope"},
            {
                "title": "Welcome to Dead House",
                "description": "Classic Goosebumps!",
                "narrator": "Tara Sands",
                "matchConfidence": 0.84,
            },
        ],
    }

    result, searched, patched = _run(
        _auto_match(
            results,
            item_id="item-1",
            title="Welcome to Dead House",
            author="R. L. Stine",
            min_confidence=0.7,
        )
    )

    assert result["applied"] is True
    assert result["confidence"] == 0.84
    assert result["provider"] == "audible.uk"
    assert result["matched_title"] == "Welcome to Dead House"
    # The item id must be passed so ABS can compute duration-based confidence.
    assert searched[0] == ("audible.uk", "item-1", "Welcome to Dead House")
    assert len(patched) == 1
    body = patched[0]["json"]["metadata"]
    assert body["description"] == "Classic Goosebumps!"
    assert body["narrators"] == ["Tara Sands"]


def test_auto_match_skips_low_confidence_result() -> None:
    results = {"audible.uk": [{"title": "Meh", "matchConfidence": 0.4, "description": "x"}]}

    result, _searched, patched = _run(
        _auto_match(results, item_id="i", title="t", min_confidence=0.7)
    )

    assert result["applied"] is False
    assert result["confidence"] == 0.4
    assert patched == []  # nothing patched, existing metadata preserved


def test_auto_match_ignores_results_without_confidence() -> None:
    # Non-audible providers do not return matchConfidence; treat as no match.
    results = {"google": [{"title": "x", "description": "y"}]}

    result, _searched, patched = _run(
        _auto_match(results, item_id="i", title="t", min_confidence=0.7, providers=("google",))
    )

    assert result["applied"] is False
    assert result["confidence"] is None
    assert patched == []


def test_auto_match_falls_back_to_next_provider() -> None:
    results = {
        "audible.uk": [{"title": "weak", "matchConfidence": 0.5, "description": "x"}],
        "audible": [{"title": "strong", "matchConfidence": 0.9, "description": "good"}],
    }

    result, searched, patched = _run(
        _auto_match(
            results,
            item_id="i",
            title="t",
            min_confidence=0.7,
            providers=("audible.uk", "audible"),
        )
    )

    assert result["applied"] is True
    assert result["provider"] == "audible"
    assert result["confidence"] == 0.9
    assert [s[0] for s in searched] == ["audible.uk", "audible"]
    assert patched[0]["json"]["metadata"]["description"] == "good"


def test_auto_match_stops_early_on_confident_first_provider() -> None:
    results = {
        "audible.uk": [{"title": "strong", "matchConfidence": 0.9, "description": "good"}],
        "audible": [{"title": "other", "matchConfidence": 0.95, "description": "other"}],
    }

    result, searched, _patched = _run(
        _auto_match(
            results,
            item_id="i",
            title="t",
            min_confidence=0.7,
            providers=("audible.uk", "audible"),
        )
    )

    assert result["provider"] == "audible.uk"
    assert [s[0] for s in searched] == ["audible.uk"]  # second provider not queried
