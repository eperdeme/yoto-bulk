"""Detect and explain duplicate cards.

Why this exists
---------------
A book can be represented twice in the cache: once as an ABS-backed card
(``source="abs"``) and once as a card pulled from Yoto (``source="yoto"``).
``reconcile`` links the pair so the library shows one entry, but its matcher is
deliberately strict — a mismatched author, series or sequence number makes it
refuse to link rather than risk merging two different books. When it refuses,
the same title appears twice with no explanation and no way to fix it.

``scripts/cleanup_dupes.py`` was the previous answer: a hand-edited script with
hardcoded card ids. This module replaces it with detection, a diagnosis of *why*
each pair went unlinked, and a resolution the UI can offer.

Grouping here is intentionally looser than the matcher: titles only. That is the
point — it surfaces exactly the pairs the strict matcher rejected.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from yoto_server.reconcile import _build_indices, _candidate_score, _CardIdentity
from yoto_server.store import YotoStore

logger = logging.getLogger(__name__)

# What a caller may do about a group.
RESOLUTION_LINK = "link"                # non-destructive: record that they are the same book
RESOLUTION_DELETE_YOTO = "delete_yoto"  # destructive: a real duplicate card on Yoto
RESOLUTION_FIX_IN_ABS = "fix_in_abs"    # two Audiobookshelf items; not ours to merge
RESOLUTION_NONE = "none"

# Titles that carry no identity, so must never be used to group cards.
_PLACEHOLDER_TITLES = {"untitled", "unknown", "unknown title"}


@dataclass
class DuplicateCard:
    card_id: str
    source: str | None
    title: str
    authors: list[str]
    series: str | None
    sequence_number: int | None
    chapter_count: int
    yoto_card_id: str | None
    linked_abs_card_id: str | None
    external_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "source": self.source,
            "title": self.title,
            "authors": self.authors,
            "series": self.series,
            "sequence_number": self.sequence_number,
            "chapter_count": self.chapter_count,
            "yoto_card_id": self.yoto_card_id,
            "linked_abs_card_id": self.linked_abs_card_id,
            "external_id": self.external_id,
        }


@dataclass
class DuplicateGroup:
    key: str
    title: str
    kind: str
    resolution: str
    reason: str
    cards: list[DuplicateCard] = field(default_factory=list)
    # For a linkable pair, the two sides the caller should join.
    abs_card_id: str | None = None
    yoto_card_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "kind": self.kind,
            "resolution": self.resolution,
            "reason": self.reason,
            "abs_card_id": self.abs_card_id,
            "yoto_card_id": self.yoto_card_id,
            "cards": [c.to_dict() for c in self.cards],
        }


def _to_duplicate_card(identity: _CardIdentity) -> DuplicateCard:
    from yoto_bulk.card_utils import resolve_authors, resolve_series, resolve_title

    return DuplicateCard(
        card_id=identity.card_id,
        source=identity.source,
        title=resolve_title(identity.payload),
        authors=resolve_authors(identity.payload),
        series=resolve_series(identity.payload),
        sequence_number=identity.sequence_number,
        chapter_count=identity.chapter_count,
        yoto_card_id=identity.meta.get("yoto_card_id"),
        linked_abs_card_id=identity.meta.get("linked_abs_card_id"),
        external_id=identity.meta.get("external_id"),
    )


def explain_mismatch(
    abs_card: _CardIdentity, yoto_card: _CardIdentity, candidate_count: int = 1
) -> str:
    """Say why the strict matcher would not link these two.

    Mirrors the rejection order in ``reconcile._candidate_score`` so the message
    matches the actual behaviour rather than guessing. ``candidate_count`` is how
    many Yoto cards share the title, which separates genuine ambiguity from a
    pair that simply has not been reconciled yet.
    """
    if not abs_card.title_key or not yoto_card.title_key:
        return "one of the cards has no usable title"

    if abs_card.title_key != yoto_card.title_key:
        shorter, longer = sorted([abs_card.title_key, yoto_card.title_key], key=len)
        if not (len(shorter) >= 4 and shorter in longer):
            return "titles are too different to match automatically"

    if (
        abs_card.author_keys
        and yoto_card.author_keys
        and not (abs_card.author_keys & yoto_card.author_keys)
    ):
        return "authors do not match (Yoto and Audiobookshelf credit different names)"

    if abs_card.series_key and yoto_card.series_key and abs_card.series_key != yoto_card.series_key:
        return "series names differ"

    if (
        abs_card.sequence_number is not None
        and yoto_card.sequence_number is not None
        and abs_card.sequence_number != yoto_card.sequence_number
    ):
        return "series numbers differ"

    if _candidate_score(abs_card, yoto_card) is None:
        return "the matcher rejected the pair"
    if candidate_count > 1:
        return "more than one Yoto card matched equally well, so it was left alone"
    # Nothing conflicts, so reconcile would link these — it just has not run
    # since the card was pulled.
    return "not reconciled yet — running Sync ABS should link these automatically"


async def find_duplicates(store: YotoStore, user_id: str) -> list[DuplicateGroup]:
    """Group cached cards that look like the same book.

    Grouped on the deduplicated title alone, so pairs the strict matcher
    rejected still show up here.
    """
    abs_cards, yoto_cards_by_id, _by_title, _cards_by_id = await _build_indices(
        store, user_id
    )

    buckets: dict[str, list[_CardIdentity]] = defaultdict(list)
    for identity in [*abs_cards, *yoto_cards_by_id.values()]:
        # resolve_title() falls back to "Untitled", which would otherwise bucket
        # every nameless card together as one enormous false duplicate.
        if identity.title_key and identity.title_key not in _PLACEHOLDER_TITLES:
            buckets[identity.title_key].append(identity)

    groups: list[DuplicateGroup] = []
    for key, members in buckets.items():
        if len(members) < 2:
            continue

        # Already linked pairs are not duplicates from the user's point of view:
        # the library shows one entry.
        abs_ids = {m.card_id for m in members if m.source == "abs"}
        if all(
            m.source != "yoto" or (m.meta.get("linked_abs_card_id") in abs_ids)
            for m in members
        ) and len(abs_ids) <= 1:
            continue

        abs_members = [m for m in members if m.source == "abs"]
        yoto_members = [m for m in members if m.source == "yoto"]
        title = _to_duplicate_card(members[0]).title

        if len(abs_members) > 1:
            group = DuplicateGroup(
                key=key,
                title=title,
                kind="duplicate_in_audiobookshelf",
                resolution=RESOLUTION_FIX_IN_ABS,
                reason=(
                    "Audiobookshelf holds more than one copy of this book. "
                    "Remove the extra there; the cache follows on the next sync."
                ),
            )
        elif len(yoto_members) > 1 and not abs_members:
            group = DuplicateGroup(
                key=key,
                title=title,
                kind="duplicate_on_yoto",
                resolution=RESOLUTION_DELETE_YOTO,
                reason=(
                    "Several Yoto cards exist for this book and none is backed "
                    "by Audiobookshelf."
                ),
            )
        elif abs_members and yoto_members:
            abs_card = abs_members[0]
            # Prefer an unlinked Yoto card; a stale back-reference is the same fix.
            unlinked = [m for m in yoto_members if m.meta.get("linked_abs_card_id") not in abs_ids]
            yoto_card = unlinked[0] if unlinked else yoto_members[0]
            group = DuplicateGroup(
                key=key,
                title=title,
                kind="unlinked_pair",
                resolution=RESOLUTION_LINK,
                reason=explain_mismatch(
                    abs_card, yoto_card, candidate_count=len(yoto_members)
                ),
                abs_card_id=abs_card.card_id,
                yoto_card_id=yoto_card.card_id,
            )
            if len(yoto_members) > 1:
                group.reason += f". {len(yoto_members)} Yoto cards share this title."
        else:
            group = DuplicateGroup(
                key=key,
                title=title,
                kind="unknown",
                resolution=RESOLUTION_NONE,
                reason="Unrecognised combination.",
            )

        group.cards = [_to_duplicate_card(m) for m in members]
        groups.append(group)

    groups.sort(key=lambda g: g.title.lower())
    return groups


async def link_pair(store: YotoStore, user_id: str, abs_card_id: str, yoto_card_id: str) -> None:
    """Record that an ABS card and a Yoto card are the same book.

    Writes the same metadata ``reconcile`` would, in both directions: the
    forward reference lets a push update the existing card instead of creating
    another, and the back reference is what hides the duplicate from the library.
    """
    await store._r.hset(
        store._meta_key(user_id, abs_card_id),
        mapping={"yoto_card_id": yoto_card_id},
    )
    await store._r.hset(
        store._meta_key(user_id, yoto_card_id),
        mapping={"linked_abs_card_id": abs_card_id},
    )
    logger.info("Linked ABS card %s to Yoto card %s for %s", abs_card_id, yoto_card_id, user_id)


async def unlink_pair(store: YotoStore, user_id: str, abs_card_id: str, yoto_card_id: str) -> None:
    """Undo a link, in case two different books were joined by mistake."""
    await store._r.hdel(store._meta_key(user_id, abs_card_id), "yoto_card_id")
    await store._r.hdel(store._meta_key(user_id, yoto_card_id), "linked_abs_card_id")
    logger.info("Unlinked ABS card %s from Yoto card %s for %s", abs_card_id, yoto_card_id, user_id)
