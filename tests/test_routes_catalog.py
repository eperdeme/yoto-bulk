from __future__ import annotations

import asyncio
from types import SimpleNamespace

from borrowbox import Availability, CatalogItem, Format
from borrowbox_server import routes_catalog
from borrowbox_server.routes_catalog import _series_lookup_candidates


class _FakeSearchStore:
    def __init__(self) -> None:
        self.get_search_calls: list[tuple[str, str]] = []
        self.set_search_calls: list[tuple[str, str, list[dict]]] = []
        self.products: list[tuple[str, list[dict]]] = []
        self.availability: list[tuple[str, list[dict]]] = []

    async def get_search(self, library: str, query: str):
        self.get_search_calls.append((library, query))
        return None

    async def set_search(self, library: str, query: str, results: list[dict]) -> None:
        self.set_search_calls.append((library, query, results))

    async def set_products_bulk(self, library: str, products: list[dict]) -> None:
        self.products.append((library, products))

    async def set_availability_bulk(self, library: str, items: list[dict]) -> None:
        self.availability.append((library, items))


def test_series_lookup_candidates_prefers_audio_series_ids() -> None:
    ebook = CatalogItem(
        product_id="ebook-1",
        title="Harry Potter and the Philosopher's Stone",
        series_name="Harry Potter",
        series_id="ebook-series",
        format="eBooks",
    )
    audiobook = CatalogItem(
        product_id="audio-1",
        title="Harry Potter and the Goblet of Fire",
        series_name="Harry Potter",
        series_id="audio-series",
        format="eAudiobooks",
    )

    candidates = _series_lookup_candidates([ebook, audiobook], "Harry Potter")

    assert [item.series_id for item in candidates] == ["audio-series", "ebook-series"]


def test_series_lookup_candidates_deduplicates_series_ids() -> None:
    first = CatalogItem(
        product_id="audio-1",
        title="Book One",
        series_name="Hilda",
        series_id="series-1",
        format="eAudiobooks",
    )
    second = CatalogItem(
        product_id="audio-2",
        title="Book Two",
        series_name="Hilda",
        series_id="series-1",
        format="eAudiobooks",
    )

    candidates = _series_lookup_candidates([first, second], "Hilda")

    assert candidates == [first]


def test_series_lookup_candidates_falls_back_to_title_matches() -> None:
    audiobook = CatalogItem(
        product_id="audio-1",
        title="Tom Gates: Excellent Excuses",
        series_name=None,
        series_id="tom-gates-audio",
        format="eAudiobooks",
    )

    candidates = _series_lookup_candidates([audiobook], "Tom Gates")

    assert [item.series_id for item in candidates] == ["tom-gates-audio"]


def test_search_uses_audiobook_filter_and_audio_cache(monkeypatch) -> None:
    calls: list[tuple[str, str, Format]] = []

    class FakeCatalog:
        def __init__(self, subdomain: str) -> None:
            self.subdomain = subdomain

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def search(self, query: str, format_filter: Format = Format.ALL) -> list[CatalogItem]:
            calls.append((self.subdomain, query, format_filter))
            return [
                CatalogItem(
                    product_id="BOL_AUDIO_1",
                    title="Harry Potter and the Prisoner of Azkaban",
                    authors=["J.K. Rowling"],
                    availability=Availability.AVAILABLE,
                    format="eAudiobooks",
                )
            ]

    store = _FakeSearchStore()
    settings = SimpleNamespace(libraries={"hounslow": "hounslow"})
    monkeypatch.setattr(routes_catalog, "BorrowBoxCatalog", FakeCatalog)

    results = asyncio.run(
        routes_catalog.search(
            library="hounslow",
            q="Harry Potter",
            store=store,  # type: ignore[arg-type]
            settings=settings,
        )
    )

    assert calls == [("hounslow", "Harry Potter", Format.AUDIOBOOK)]
    assert store.get_search_calls == [("hounslow", "audio:v1:Harry Potter")]
    assert store.set_search_calls[0][0:2] == ("hounslow", "audio:v1:Harry Potter")
    assert [result.format for result in results] == ["eAudiobooks"]