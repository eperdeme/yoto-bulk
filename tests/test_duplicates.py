"""Tests for duplicate detection.

The motivating case: "The BFG" showing once on Yoto, once in Audiobookshelf, but
twice in the app — an ABS card and a pulled Yoto card that reconcile refused to
link, with no explanation and no way to fix it.
"""

from __future__ import annotations

import asyncio

from yoto_server import duplicates

run = asyncio.run


def payload(title: str, authors: list[str] | None = None, series: str | None = None,
            seq: int | None = None, chapters: int = 1) -> dict:
    """A card payload shaped the way the resolvers expect."""
    return {
        "title": title,
        "metadata": {
            "title": title,
            "author": ", ".join(authors or []),
            "series": series,
            # resolve_sequence_number() reads metadata.sequence_number.
            "sequence_number": seq,
        },
        "content": {"chapters": [{"title": f"ch{i}", "tracks": []} for i in range(chapters)]},
    }


class FakeStore:
    """In-memory stand-in covering what reconcile/duplicates read and write."""

    def __init__(self) -> None:
        self.cards: dict[str, dict] = {}
        self.meta: dict[str, dict[str, str]] = {}
        self._r = self  # duplicates writes via store._r.hset

    # --- reads used by reconcile._build_indices ---
    async def get_all_meta(self, user_id: str) -> dict[str, dict[str, str]]:
        return {k: dict(v) for k, v in self.meta.items()}

    async def load_all_cards(self, user_id: str) -> list[tuple[str, dict]]:
        return list(self.cards.items())

    async def load_card(self, user_id: str, card_id: str) -> dict | None:
        return self.cards.get(card_id)

    async def get_meta(self, user_id: str, card_id: str) -> dict[str, str]:
        return dict(self.meta.get(card_id, {}))

    # --- writes ---
    def _meta_key(self, user_id: str, card_id: str) -> str:
        return card_id  # the fake keys meta by card id directly

    async def hset(self, key: str, mapping: dict[str, str]) -> None:
        self.meta.setdefault(key, {}).update(mapping)

    async def hdel(self, key: str, *fields: str) -> None:
        for f in fields:
            self.meta.get(key, {}).pop(f, None)

    # --- helper ---
    def add(self, card_id: str, source: str, card: dict, **meta) -> None:
        self.cards[card_id] = card
        self.meta[card_id] = {"source": source, **{k: str(v) for k, v in meta.items()}}


USER = "auth0|test"


def groups_for(store: FakeStore) -> list[duplicates.DuplicateGroup]:
    return run(duplicates.find_duplicates(store, USER))


# ── the reported case ───────────────────────────────────────────────────────


def test_unlinked_abs_and_yoto_pair_is_reported() -> None:
    store = FakeStore()
    store.add("abs-1", "abs", payload("The BFG", ["Roald Dahl"]), external_id="1")
    # Yoto credits the narrator instead, which is what stops the matcher.
    store.add("yoto-1", "yoto", payload("The BFG", ["David Walliams"]))

    groups = groups_for(store)
    assert len(groups) == 1
    group = groups[0]
    assert group.kind == "unlinked_pair"
    assert group.resolution == duplicates.RESOLUTION_LINK
    assert group.abs_card_id == "abs-1"
    assert group.yoto_card_id == "yoto-1"
    assert "authors do not match" in group.reason


def test_leading_article_does_not_hide_the_duplicate() -> None:
    """"The BFG" and "BFG" are the same book."""
    store = FakeStore()
    store.add("abs-1", "abs", payload("The BFG", ["Roald Dahl"]), external_id="1")
    store.add("yoto-1", "yoto", payload("BFG", ["Roald Dahl"]))
    assert len(groups_for(store)) == 1


def test_already_linked_pair_is_not_a_duplicate() -> None:
    """Once linked the library shows one entry, so it must not be reported."""
    store = FakeStore()
    store.add(
        "abs-1", "abs", payload("The BFG", ["Roald Dahl"]), external_id="1", yoto_card_id="yoto-1"
    )
    store.add("yoto-1", "yoto", payload("The BFG", ["Roald Dahl"]), linked_abs_card_id="abs-1")
    assert groups_for(store) == []


def test_linking_makes_the_duplicate_go_away() -> None:
    store = FakeStore()
    store.add("abs-1", "abs", payload("The BFG", ["Roald Dahl"]), external_id="1")
    store.add("yoto-1", "yoto", payload("The BFG", ["David Walliams"]))
    assert len(groups_for(store)) == 1

    run(duplicates.link_pair(store, USER, "abs-1", "yoto-1"))

    assert store.meta["abs-1"]["yoto_card_id"] == "yoto-1"
    assert store.meta["yoto-1"]["linked_abs_card_id"] == "abs-1"
    assert groups_for(store) == []


def test_unlink_restores_the_duplicate() -> None:
    store = FakeStore()
    store.add("abs-1", "abs", payload("The BFG", ["Roald Dahl"]), external_id="1")
    store.add("yoto-1", "yoto", payload("The BFG", ["Roald Dahl"]))
    run(duplicates.link_pair(store, USER, "abs-1", "yoto-1"))
    run(duplicates.unlink_pair(store, USER, "abs-1", "yoto-1"))
    assert "yoto_card_id" not in store.meta["abs-1"]
    assert len(groups_for(store)) == 1


# ── other shapes ────────────────────────────────────────────────────────────


def test_two_yoto_cards_with_no_abs_backing_suggest_deletion() -> None:
    store = FakeStore()
    store.add("yoto-1", "yoto", payload("Matilda", ["Roald Dahl"]))
    store.add("yoto-2", "yoto", payload("Matilda", ["Roald Dahl"]))

    group = groups_for(store)[0]
    assert group.kind == "duplicate_on_yoto"
    assert group.resolution == duplicates.RESOLUTION_DELETE_YOTO
    assert len(group.cards) == 2


def test_two_abs_items_are_reported_as_an_abs_problem() -> None:
    """Not ours to merge: Audiobookshelf is the source of truth."""
    store = FakeStore()
    store.add("abs-1", "abs", payload("Matilda", ["Roald Dahl"]), external_id="1")
    store.add("abs-2", "abs", payload("Matilda", ["Roald Dahl"]), external_id="2")

    group = groups_for(store)[0]
    assert group.kind == "duplicate_in_audiobookshelf"
    assert group.resolution == duplicates.RESOLUTION_FIX_IN_ABS
    assert "Audiobookshelf" in group.reason


def test_stale_back_reference_is_treated_as_linkable() -> None:
    """A forward link with a missing back reference still shows as a duplicate."""
    store = FakeStore()
    store.add(
        "abs-1", "abs", payload("The BFG", ["Roald Dahl"]), external_id="1", yoto_card_id="yoto-1"
    )
    store.add("yoto-1", "yoto", payload("The BFG", ["Roald Dahl"]))  # no linked_abs_card_id

    group = groups_for(store)[0]
    assert group.kind == "unlinked_pair"
    assert group.abs_card_id == "abs-1"
    assert group.yoto_card_id == "yoto-1"


def test_distinct_books_are_not_grouped() -> None:
    store = FakeStore()
    store.add("abs-1", "abs", payload("The BFG", ["Roald Dahl"]), external_id="1")
    store.add("yoto-1", "yoto", payload("Matilda", ["Roald Dahl"]))
    assert groups_for(store) == []


def test_single_card_is_never_a_duplicate() -> None:
    store = FakeStore()
    store.add("abs-1", "abs", payload("The BFG", ["Roald Dahl"]), external_id="1")
    assert groups_for(store) == []


def test_cards_without_titles_are_ignored() -> None:
    store = FakeStore()
    store.add("abs-1", "abs", payload(""), external_id="1")
    store.add("yoto-1", "yoto", payload(""))
    assert groups_for(store) == []


# ── diagnosis quality ───────────────────────────────────────────────────────


def test_reason_explains_a_series_number_mismatch() -> None:
    store = FakeStore()
    store.add("abs-1", "abs", payload("Book", ["A"], series="S", seq=1), external_id="1")
    store.add("yoto-1", "yoto", payload("Book", ["A"], series="S", seq=2))
    assert "series numbers differ" in groups_for(store)[0].reason


def test_reason_explains_a_series_mismatch() -> None:
    store = FakeStore()
    store.add("abs-1", "abs", payload("Book", ["A"], series="One"), external_id="1")
    store.add("yoto-1", "yoto", payload("Book", ["A"], series="Two"))
    assert "series names differ" in groups_for(store)[0].reason


def test_ambiguity_is_reported_when_several_yoto_cards_match() -> None:
    store = FakeStore()
    store.add("abs-1", "abs", payload("Book", ["A"]), external_id="1")
    store.add("yoto-1", "yoto", payload("Book", ["A"]))
    store.add("yoto-2", "yoto", payload("Book", ["A"]))
    assert "matched equally well" in groups_for(store)[0].reason


def test_a_clean_pair_is_reported_as_merely_unreconciled() -> None:
    """When nothing conflicts, the reason must not invent a cause."""
    store = FakeStore()
    store.add("abs-1", "abs", payload("Book", ["A"]), external_id="1")
    store.add("yoto-1", "yoto", payload("Book", ["A"]))
    assert "not reconciled yet" in groups_for(store)[0].reason


def test_groups_are_sorted_by_title() -> None:
    store = FakeStore()
    store.add("abs-z", "abs", payload("Zebra"), external_id="z")
    store.add("yoto-z", "yoto", payload("Zebra"))
    store.add("abs-a", "abs", payload("Apple"), external_id="a")
    store.add("yoto-a", "yoto", payload("Apple"))
    assert [g.title for g in groups_for(store)] == ["Apple", "Zebra"]


def test_serialisation_is_json_friendly() -> None:
    import json

    store = FakeStore()
    store.add("abs-1", "abs", payload("The BFG", ["Roald Dahl"]), external_id="1")
    store.add("yoto-1", "yoto", payload("The BFG", ["David Walliams"]))
    encoded = json.dumps([g.to_dict() for g in groups_for(store)])
    assert "The BFG" in encoded
    assert "abs-1" in encoded
