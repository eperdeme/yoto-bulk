"""Helpers for linking ABS-backed cards to existing Yoto cards."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from yoto_bulk.card_utils import (
    normalize_title,
    normalize_title_for_dedup,
    resolve_authors,
    resolve_sequence_number,
    resolve_series,
    resolve_title,
)

from yoto_server.store import YotoStore


@dataclass(frozen=True)
class _CardIdentity:
    card_id: str
    source: str | None
    payload: dict
    meta: dict[str, str]
    title_key: str
    author_keys: frozenset[str]
    series_key: str | None
    sequence_number: int | None
    chapter_count: int


def _normalize_people(authors: list[str]) -> frozenset[str]:
    names: set[str] = set()
    for author in authors:
        normalized = normalize_title(author)
        if normalized and normalized not in {"unknown author"}:
            names.add(normalized)
    return frozenset(names)


def _card_identity(card_id: str, payload: dict, meta: dict[str, str]) -> _CardIdentity:
    chapters = ((payload.get("content") or {}).get("chapters") or [])
    series = resolve_series(payload)
    return _CardIdentity(
        card_id=card_id,
        source=meta.get("source"),
        payload=payload,
        meta=meta,
        title_key=normalize_title_for_dedup(resolve_title(payload)),
        author_keys=_normalize_people(resolve_authors(payload)),
        series_key=normalize_title(series) if series else None,
        sequence_number=resolve_sequence_number(payload),
        chapter_count=len(chapters),
    )


def _candidate_score(abs_card: _CardIdentity, yoto_card: _CardIdentity) -> int | None:
    if not abs_card.title_key or not yoto_card.title_key:
        return None

    # Exact title match (best case)
    title_exact = abs_card.title_key == yoto_card.title_key

    # Substring match — handles "Holiday Heatwave" vs "Hotel Flamingo Holiday Heatwave"
    # when BorrowBox strips series prefix from the title
    shorter, longer = sorted([abs_card.title_key, yoto_card.title_key], key=len)
    title_substring = (
        not title_exact
        and len(shorter) >= 4
        and shorter in longer
    )

    if not title_exact and not title_substring:
        return None

    # For substring matches, require at least one corroborating signal
    # (author or series) to avoid false positives across unrelated books
    if title_substring:
        has_author_overlap = (
            abs_card.author_keys and yoto_card.author_keys
            and bool(abs_card.author_keys & yoto_card.author_keys)
        )
        has_series_match = (
            abs_card.series_key and yoto_card.series_key
            and abs_card.series_key == yoto_card.series_key
        )
        if not has_author_overlap and not has_series_match:
            return None

    if abs_card.author_keys and yoto_card.author_keys:
        if not (abs_card.author_keys & yoto_card.author_keys):
            return None

    if abs_card.series_key and yoto_card.series_key and abs_card.series_key != yoto_card.series_key:
        return None

    if (
        abs_card.sequence_number is not None
        and yoto_card.sequence_number is not None
        and abs_card.sequence_number != yoto_card.sequence_number
    ):
        return None

    score = 0
    if abs_card.author_keys and yoto_card.author_keys and abs_card.author_keys & yoto_card.author_keys:
        score += 4
    if abs_card.series_key and yoto_card.series_key and abs_card.series_key == yoto_card.series_key:
        score += 2
    if (
        abs_card.sequence_number is not None
        and yoto_card.sequence_number is not None
        and abs_card.sequence_number == yoto_card.sequence_number
    ):
        score += 2
    if abs_card.chapter_count and yoto_card.chapter_count and abs_card.chapter_count == yoto_card.chapter_count:
        score += 1
    return score


def _find_best_match(
    abs_card: _CardIdentity,
    yoto_cards_by_id: dict[str, _CardIdentity],
    yoto_cards_by_title: dict[str, list[_CardIdentity]],
    cards_by_id: dict[str, dict],
) -> _CardIdentity | None:
    """Return the best unambiguous yoto match for an ABS card, or None."""
    candidates: list[tuple[int, _CardIdentity]] = []
    # Fast path: exact title match via index
    for yoto_card in yoto_cards_by_title.get(abs_card.title_key, []):
        linked_abs_id = yoto_card.meta.get("linked_abs_card_id")
        if linked_abs_id and linked_abs_id != abs_card.card_id and linked_abs_id in cards_by_id:
            continue
        score = _candidate_score(abs_card, yoto_card)
        if score is not None:
            candidates.append((score, yoto_card))

    # Slow path: substring title matching across all yoto cards
    if not candidates:
        for yoto_card in yoto_cards_by_id.values():
            if yoto_card.title_key == abs_card.title_key:
                continue  # already checked above
            linked_abs_id = yoto_card.meta.get("linked_abs_card_id")
            if linked_abs_id and linked_abs_id != abs_card.card_id and linked_abs_id in cards_by_id:
                continue
            score = _candidate_score(abs_card, yoto_card)
            if score is not None:
                candidates.append((score, yoto_card))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1].card_id), reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None  # ambiguous
    return candidates[0][1]


async def _build_indices(
    store: YotoStore, user_id: str
) -> tuple[
    list[_CardIdentity],
    dict[str, _CardIdentity],
    dict[str, list[_CardIdentity]],
    dict[str, dict],
]:
    all_meta = await store.get_all_meta(user_id)
    all_cards = await store.load_all_cards(user_id)
    cards_by_id = {card_id: payload for card_id, payload in all_cards}

    abs_cards: list[_CardIdentity] = []
    yoto_cards_by_id: dict[str, _CardIdentity] = {}
    yoto_cards_by_title: dict[str, list[_CardIdentity]] = defaultdict(list)

    for card_id, payload in all_cards:
        meta = all_meta.get(card_id) or {}
        identity = _card_identity(card_id, payload, meta)
        if identity.source == "abs":
            abs_cards.append(identity)
        elif identity.source == "yoto":
            yoto_cards_by_id[card_id] = identity
            if identity.title_key:
                yoto_cards_by_title[identity.title_key].append(identity)

    return abs_cards, yoto_cards_by_id, yoto_cards_by_title, cards_by_id


async def find_yoto_match_for_abs(
    store: YotoStore, user_id: str, abs_card_id: str
) -> str | None:
    """Find an existing yoto-source card that matches the given ABS card.

    Returns the matching ``yoto_card_id`` if a single unambiguous candidate
    exists, otherwise ``None``. Used as a defensive check before pushing an
    ABS-backed card to Yoto so we don't create duplicates when reconcile
    failed to link them earlier.
    """
    abs_cards, yoto_cards_by_id, yoto_cards_by_title, cards_by_id = await _build_indices(store, user_id)
    target = next((c for c in abs_cards if c.card_id == abs_card_id), None)
    if target is None:
        return None
    match = _find_best_match(target, yoto_cards_by_id, yoto_cards_by_title, cards_by_id)
    return match.card_id if match else None


async def reconcile_abs_yoto_links(store: YotoStore, user_id: str) -> dict[str, int]:
    """Link ABS-backed cards to matching pulled Yoto cards.

    ABS remains authoritative. We only persist linkage metadata so the UI can
    stop treating already-present Yoto cards as needing a new push.
    """

    abs_cards, yoto_cards_by_id, yoto_cards_by_title, cards_by_id = await _build_indices(store, user_id)

    linked = 0
    ambiguous = 0

    for abs_card in abs_cards:
        preferred_yoto_id = abs_card.meta.get("yoto_card_id")
        if preferred_yoto_id:
            existing_yoto = yoto_cards_by_id.get(preferred_yoto_id)
            if existing_yoto:
                if existing_yoto.meta.get("linked_abs_card_id") != abs_card.card_id:
                    await store._r.hset(
                        store._meta_key(user_id, existing_yoto.card_id),
                        mapping={"linked_abs_card_id": abs_card.card_id},
                    )
                    linked += 1
                continue

        # Track ambiguity separately for the bulk reporter
        candidates: list[tuple[int, _CardIdentity]] = []
        for yoto_card in yoto_cards_by_title.get(abs_card.title_key, []):
            linked_abs_id = yoto_card.meta.get("linked_abs_card_id")
            if linked_abs_id and linked_abs_id != abs_card.card_id and linked_abs_id in cards_by_id:
                continue
            score = _candidate_score(abs_card, yoto_card)
            if score is not None:
                candidates.append((score, yoto_card))
        if not candidates:
            for yoto_card in yoto_cards_by_id.values():
                if yoto_card.title_key == abs_card.title_key:
                    continue
                linked_abs_id = yoto_card.meta.get("linked_abs_card_id")
                if linked_abs_id and linked_abs_id != abs_card.card_id and linked_abs_id in cards_by_id:
                    continue
                score = _candidate_score(abs_card, yoto_card)
                if score is not None:
                    candidates.append((score, yoto_card))

        if not candidates:
            continue

        candidates.sort(key=lambda item: (item[0], item[1].card_id), reverse=True)
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            ambiguous += 1
            continue

        best_yoto = candidates[0][1]
        await store._r.hset(
            store._meta_key(user_id, abs_card.card_id),
            mapping={"yoto_card_id": best_yoto.card_id},
        )
        await store._r.hset(
            store._meta_key(user_id, best_yoto.card_id),
            mapping={"linked_abs_card_id": abs_card.card_id},
        )
        linked += 1

    return {"linked": linked, "ambiguous": ambiguous}
