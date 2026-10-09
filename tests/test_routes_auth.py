from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

from borrowbox_server import routes_auth
from borrowbox_server.schemas import LoanInfo


class _FakeRedis:
    def __init__(self, cached_url: str | None) -> None:
        self.cached_url = cached_url
        self.setex_calls: list[tuple[str, int, str]] = []

    async def get(self, key: str) -> str | None:
        return self.cached_url

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.setex_calls.append((key, ttl, value))

    async def delete(self, key: str) -> None:
        return None


class _FakeStore:
    def __init__(self, cached_url: str | None) -> None:
        self._r = _FakeRedis(cached_url)
        self.saved_products: list[tuple[str, str, dict]] = []
        self.abs_links: dict[tuple[str, str], str] = {}

    async def get_product(self, library: str, product_id: str):
        return None

    async def set_product(self, library: str, product_id: str, data: dict):
        self.saved_products.append((library, product_id, data))

    async def get_current_loans(self, user_id: str, library: str):
        return None

    async def get_abs_link(self, library: str, product_id: str):
        return self.abs_links.get((library, product_id))

    async def set_abs_link(self, library: str, product_id: str, abs_item_id: str):
        self.abs_links[(library, product_id)] = abs_item_id


class _FakeDownloadResponse:
    def __init__(self, url: str) -> None:
        self.url = url
        self.content = b"zip-bytes"

    def raise_for_status(self) -> None:
        return None


class _FakeAsyncClient:
    requested_urls: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str):
        self.requested_urls.append(url)
        return _FakeDownloadResponse(url)


class _StagedImport:
    def __init__(self) -> None:
        self.result = {"status": "skipped", "reason": "test"}
        self.cleaned_up = False

    def cleanup(self) -> None:
        self.cleaned_up = True


class _FakeAbsClient:
    def __init__(self, items: list[dict]) -> None:
        self.items = items

    async def get_book_library(self, preferred_name=None):
        return {"id": "lib-1"}

    async def list_library_items(self, library_id: str):
        return self.items

    async def aclose(self) -> None:
        return None


class _FakeBorrowBoxCatalog:
    def __init__(self, library: str) -> None:
        self.library = library

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get_product(self, product_id: str):
        @dataclass
        class _FakeProductDetail:
            product_id: str
            title: str
            authors: list[str]
            narrators: list[str]
            description: str | None
            isbn: str | None
            duration_seconds: int | None
            format: str | None
            file_type: str | None
            language: str | None
            publisher: str | None
            released: str | None
            series_name: str | None
            series_number: int | None
            series_id: str | None
            availability: str | None
            available_date: str | None
            cover_url: str | None
            product_url: str | None

        return _FakeProductDetail(
            product_id=product_id,
            title="Mirabelle and the Magical Mayhem",
            authors=["Harriet Muncaster"],
            narrators=[],
            description="A proper BorrowBox description",
            isbn=None,
            duration_seconds=None,
            format="eAudiobooks",
            file_type=None,
            language=None,
            publisher=None,
            released=None,
            series_name="Mirabelle",
            series_number=2,
            series_id="series-1",
            availability="on_loan",
            available_date=None,
            cover_url=None,
            product_url=None,
        )


def test_import_discards_bad_cached_download_url_and_refreshes(monkeypatch, tmp_path) -> None:
    store = _FakeStore("/broken-relative-url.zip")
    settings = SimpleNamespace(abs_token="", libraries={"hounslow": "hounslow"}, zip_archive_dir=str(tmp_path / "zip-archive"))
    staged = _StagedImport()
    refreshed_urls: list[str] = []

    async def fake_with_client(library, settings_obj, fn, store=None):
        refreshed_urls.append(library)
        return "https://download-eu.borrowbox.com/fresh.zip"

    monkeypatch.setattr(routes_auth, "_with_client", fake_with_client)
    monkeypatch.setattr(routes_auth.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(routes_auth, "stage_borrowbox_zip_import", lambda zip_bytes, title=None: staged)

    result = asyncio.run(
        routes_auth.import_to_yoto(
            library="hounslow",
            loan_id="291194018",
            product_id="BOL_750376",
            store=store,
            settings=settings,
        )
    )

    assert result == {"status": "skipped", "reason": "test"}
    assert refreshed_urls == ["hounslow"]
    assert _FakeAsyncClient.requested_urls == ["https://download-eu.borrowbox.com/fresh.zip"]
    assert store._r.setex_calls == [
        ("bb:download_url:hounslow:291194018", 600, "https://download-eu.borrowbox.com/fresh.zip")
    ]
    assert staged.cleaned_up is True


def test_mark_loans_present_in_abs_matches_live_abs_results(monkeypatch) -> None:
    loans = [
        LoanInfo(
            product_id="BOL_1",
            title="Harry Potter and the Philosopher's Stone",
            authors=["J.K. Rowling"],
            loan_type="loan",
            loan_id="123",
        ),
        LoanInfo(
            product_id="BOL_2",
            title="Different Book",
            authors=["Other Author"],
            loan_type="loan",
            loan_id="456",
        ),
    ]
    fake_abs = _FakeAbsClient(
        [
            {
                "id": "abs-1",
                "path": "/audiobooks/J.K. Rowling/Harry Potter and the Philosopher's Stone",
                "media": {
                    "metadata": {
                        "title": "Harry Potter and the Philosopher's Stone",
                        "authors": [{"name": "J.K. Rowling"}],
                    }
                },
            },
        ]
    )
    monkeypatch.setattr(routes_auth, "_get_abs_client", lambda settings: fake_abs)

    updated = asyncio.run(routes_auth._mark_loans_present_in_abs(loans, SimpleNamespace(abs_token="token", abs_library_name=None)))

    assert updated[0].in_abs is True
    assert updated[0].abs_item_id == "abs-1"
    assert updated[1].in_abs is False


def _abs_settings() -> SimpleNamespace:
    return SimpleNamespace(abs_token="token", abs_library_name=None)


def test_mark_loans_uses_stable_link_even_when_title_changed(monkeypatch) -> None:
    # Simulate a metadata edit: the ABS title no longer matches the loan title,
    # but a recorded link points at an existing item, so it stays "in ABS".
    loans = [
        LoanInfo(
            product_id="BOL_9",
            title="Welcome to Dead House",
            authors=["R. L. Stine"],
            loan_type="loan",
            loan_id="1",
        )
    ]
    fake_abs = _FakeAbsClient(
        [{"id": "abs-42", "path": "/x", "media": {"metadata": {"title": "Classic Goosebumps: Welcome to Dead House"}}}]
    )
    monkeypatch.setattr(routes_auth, "_get_abs_client", lambda settings: fake_abs)
    store = _FakeStore(None)
    store.abs_links[("hounslow", "BOL_9")] = "abs-42"

    updated = asyncio.run(
        routes_auth._mark_loans_present_in_abs(loans, _abs_settings(), store=store, library="hounslow")
    )

    assert updated[0].in_abs is True
    assert updated[0].abs_item_id == "abs-42"


def test_mark_loans_ignores_stale_link_and_backfills(monkeypatch) -> None:
    # A recorded link that points at a since-deleted item is ignored; the loan
    # is matched by title and the link is repaired to the live id.
    loans = [
        LoanInfo(
            product_id="BOL_1",
            title="Harry Potter and the Philosopher's Stone",
            authors=["J.K. Rowling"],
            loan_type="loan",
            loan_id="1",
        )
    ]
    fake_abs = _FakeAbsClient(
        [
            {
                "id": "abs-live",
                "path": "/audiobooks/J.K. Rowling/Harry Potter and the Philosopher's Stone",
                "media": {
                    "metadata": {
                        "title": "Harry Potter and the Philosopher's Stone",
                        "authors": [{"name": "J.K. Rowling"}],
                    }
                },
            }
        ]
    )
    monkeypatch.setattr(routes_auth, "_get_abs_client", lambda settings: fake_abs)
    store = _FakeStore(None)
    store.abs_links[("hounslow", "BOL_1")] = "abs-deleted"

    updated = asyncio.run(
        routes_auth._mark_loans_present_in_abs(loans, _abs_settings(), store=store, library="hounslow")
    )

    assert updated[0].in_abs is True
    assert updated[0].abs_item_id == "abs-live"
    # Stale link repaired to the live id.
    assert store.abs_links[("hounslow", "BOL_1")] == "abs-live"


def test_abs_search_match_handles_series_prefixed_loan_title() -> None:
    # ABS stores the bare book title; the loan carries the "Series: Title" form.
    # The full title is too short a ratio to match, but the series-stripped tail does.
    loan = LoanInfo(
        product_id="p",
        title="Diary of a Wimpy Kid: Rodrick Rules",
        authors=["Jeff Kinney"],
        loan_type="loan",
        loan_id="1",
    )
    item = {
        "id": "abs-1",
        "media": {"metadata": {"title": "Rodrick Rules", "authors": [{"name": "Jeff Kinney"}]}},
    }

    assert routes_auth._abs_search_match(loan, item) is True


def test_find_existing_abs_item_via_link(monkeypatch) -> None:
    fake_abs = _FakeAbsClient([{"id": "abs-1", "media": {"metadata": {"title": "Anything"}}}])
    monkeypatch.setattr(routes_auth, "_get_abs_client", lambda settings: fake_abs)
    store = _FakeStore(None)
    store.abs_links[("hounslow", "BOL_1")] = "abs-1"

    result = asyncio.run(
        routes_auth._find_existing_abs_item(_abs_settings(), store, "hounslow", "BOL_1", None)
    )

    assert result == "abs-1"


def test_find_existing_abs_item_via_title_backfills_link(monkeypatch) -> None:
    fake_abs = _FakeAbsClient(
        [{"id": "abs-9", "media": {"metadata": {"title": "Rodrick Rules", "authors": [{"name": "Jeff Kinney"}]}}}]
    )
    monkeypatch.setattr(routes_auth, "_get_abs_client", lambda settings: fake_abs)
    store = _FakeStore(None)
    bb_product = {
        "title": "Rodrick Rules",
        "authors": [{"name": "Jeff Kinney"}],
        "series_name": "Diary of a Wimpy Kid",
    }

    result = asyncio.run(
        routes_auth._find_existing_abs_item(_abs_settings(), store, "hounslow", "BOL_2", bb_product)
    )

    assert result == "abs-9"
    assert store.abs_links[("hounslow", "BOL_2")] == "abs-9"


def test_find_existing_abs_item_ignores_stale_link(monkeypatch) -> None:
    fake_abs = _FakeAbsClient(
        [{"id": "abs-live", "media": {"metadata": {"title": "Book", "authors": [{"name": "A"}]}}}]
    )
    monkeypatch.setattr(routes_auth, "_get_abs_client", lambda settings: fake_abs)
    store = _FakeStore(None)
    store.abs_links[("hounslow", "BOL_1")] = "abs-deleted"  # no longer in ABS
    bb_product = {"title": "Book", "authors": [{"name": "A"}]}

    result = asyncio.run(
        routes_auth._find_existing_abs_item(_abs_settings(), store, "hounslow", "BOL_1", bb_product)
    )

    assert result == "abs-live"
    assert store.abs_links[("hounslow", "BOL_1")] == "abs-live"


def test_find_existing_abs_item_returns_none_when_absent(monkeypatch) -> None:
    fake_abs = _FakeAbsClient([{"id": "abs-1", "media": {"metadata": {"title": "Totally Different"}}}])
    monkeypatch.setattr(routes_auth, "_get_abs_client", lambda settings: fake_abs)
    store = _FakeStore(None)
    bb_product = {"title": "Welcome to Dead House", "authors": [{"name": "R. L. Stine"}]}

    result = asyncio.run(
        routes_auth._find_existing_abs_item(_abs_settings(), store, "hounslow", "BOL_3", bb_product)
    )

    assert result is None


def test_find_existing_abs_item_none_without_token() -> None:
    store = _FakeStore(None)
    settings = SimpleNamespace(abs_token="", abs_library_name=None)

    result = asyncio.run(
        routes_auth._find_existing_abs_item(settings, store, "hounslow", "BOL_1", None)
    )

    assert result is None


def test_import_returns_already_imported_and_skips_download(monkeypatch) -> None:
    store = _FakeStore(None)
    settings = SimpleNamespace(
        abs_token="token",
        libraries={"hounslow": "hounslow"},
        zip_archive_dir="/tmp/zip",
        abs_library_name=None,
    )

    async def fake_get_bb_product(library, pid, store, settings):
        return {"title": "Welcome to Dead House", "authors": [{"name": "R. L. Stine"}]}

    async def fake_find_existing(settings, store, library, product_id, bb_product):
        return "abs-existing"

    def _boom(*args, **kwargs):
        raise AssertionError("must not download/stage when already imported")

    monkeypatch.setattr(routes_auth, "_get_bb_product", fake_get_bb_product)
    monkeypatch.setattr(routes_auth, "_find_existing_abs_item", fake_find_existing)
    monkeypatch.setattr(routes_auth, "stage_borrowbox_zip_import", _boom)

    result = asyncio.run(
        routes_auth.import_to_yoto(
            library="hounslow",
            loan_id="123",
            product_id="BOL_1",
            store=store,
            settings=settings,
        )
    )

    assert result["status"] == "already_imported"
    assert result["item_id"] == "abs-existing"


def test_get_bb_product_fetches_live_when_cache_misses(monkeypatch) -> None:
    store = _FakeStore(None)
    settings = SimpleNamespace(libraries={"hounslow": "hounslow"})
    monkeypatch.setattr(routes_auth, "BorrowBoxCatalog", _FakeBorrowBoxCatalog)

    product = asyncio.run(routes_auth._get_bb_product("hounslow", "BOL_123", store, settings))

    assert product is not None
    assert product["title"] == "Mirabelle and the Magical Mayhem"
    assert product["authors"] == ["Harriet Muncaster"]
    assert store.saved_products == [("hounslow", "BOL_123", product)]
