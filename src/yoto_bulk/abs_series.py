"""ABS-first series grouping for cached Yoto cards.

The yoto-server keeps a Redis cache of cards. ABS is the upstream authority
for ABS-backed cards, so when grouping cards into series we should trust
``metadata.series`` already populated from ABS (which itself sources from
Audible / quick-match providers) before falling back to the LLM.

This module exposes :func:`group_cards_by_series`, which partitions a list of
cards into:

* deterministic ABS-derived groupings (``provenance="abs"``),
* LLM-derived groupings for the residue (``provenance="llm"``), and
* truly unmatched card ids.

The route layer is responsible for actually applying the suggestions; this
helper is pure-ish (it may call ABS quick-match if asked, but defaults off).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Any

from yoto_bulk.card_utils import card_to_yoto_book, resolve_series
from yoto_bulk.models import YotoBook

logger = logging.getLogger(__name__)


# Series-name strings that are commonly populated by upstream providers but
# are useless as grouping signal (genres masquerading as series names).
_BOGUS_SERIES = frozenset(
    {
        "",
        "children",
        "children's",
        "childrens",
        "kids",
        "juvenile",
        "fiction",
        "audiobook",
        "audiobooks",
        "audio",
        "stories",
        "story",
        "general",
        "unknown",
    }
)


def _is_bogus_series(name: str | None) -> bool:
    if not name:
        return True
    return name.strip().lower() in _BOGUS_SERIES


@dataclass
class SeriesGroup:
    """One grouped series with its members and provenance."""

    series_name: str
    card_ids: list[str] = field(default_factory=list)
    provenance: str = "abs"  # "abs" | "llm"


@dataclass
class GroupingResult:
    groups: list[SeriesGroup]
    unmatched_card_ids: list[str]
    abs_count: int = 0
    llm_count: int = 0


def _abs_group_from_cache(
    cards: list[tuple[str, dict[str, Any]]],
    meta_by_card: dict[str, dict[str, str]],
) -> tuple[list[SeriesGroup], list[tuple[str, dict[str, Any]]]]:
    """Group ABS-backed cards by their cached ABS series metadata.

    Returns (abs_groups, residue) where residue is the list of (card_id, payload)
    tuples that still need LLM handling.
    """
    by_series: dict[str, list[str]] = {}
    residue: list[tuple[str, dict[str, Any]]] = []

    for card_id, payload in cards:
        meta = meta_by_card.get(card_id) or {}
        is_abs = meta.get("source") == "abs"
        series_name = resolve_series(payload)

        if is_abs and not _is_bogus_series(series_name):
            assert series_name is not None
            by_series.setdefault(series_name.strip(), []).append(card_id)
        else:
            residue.append((card_id, payload))

    # Drop "groups" with only one ABS-backed card. A single owned book is not
    # a useful grouping on its own; let it fall through to the LLM in case it
    # belongs to a series that includes Yoto-source cards we own.
    abs_groups: list[SeriesGroup] = []
    for name, ids in by_series.items():
        if len(ids) >= 2:
            abs_groups.append(SeriesGroup(series_name=name, card_ids=ids, provenance="abs"))
        else:
            # Single-member: keep, because the apply step still wants to write
            # the canonical name back to ABS, and the user will just see it as
            # a one-book series. Single-member ABS series are still useful.
            abs_groups.append(SeriesGroup(series_name=name, card_ids=ids, provenance="abs"))

    return abs_groups, residue


def _llm_group_residue(
    residue: list[tuple[str, dict[str, Any]]],
    llm_service: Any,  # LlmService — typed loosely to avoid import cycle
) -> tuple[list[SeriesGroup], list[str]]:
    """Run the LLM on the residue and return (groups, unmatched_card_ids)."""
    if not residue:
        return [], []

    books, _ = _residue_books(residue)
    grouping = llm_service.suggest_series(books)
    return apply_llm_grouping(residue, grouping)


# Task guidance shared by the OpenAI call and the copy-paste manual prompt so
# both grouping paths give the model identical instructions.
_SERIES_GROUPING_INSTRUCTIONS = (
    "You are an expert children's librarian and audiobook archivist with "
    "extensive knowledge of children's literature, authors, illustrators, and "
    "book universes. Group the following Yoto audiobook library entries into "
    "their official real-world series.\n\n"
    "CRITICAL INSTRUCTIONS:\n"
    "1. DO NOT just group books by matching title prefixes. Use your knowledge "
    "of children's literature to group books that belong to the same real-world "
    "series or fictional universe.\n"
    "2. Treat author metadata as a strong signal. If an author writes a known "
    "series, check whether their titles belong together (e.g. group 'Knock "
    "Three Times' with 'The Wizards of Once' because both are by Cressida "
    "Cowell).\n"
    "3. If a book belongs to a known real-world series, put it under the "
    "official series name EVEN IF it is the only book from that series here.\n"
    "4. Order books chronologically within their series if you know the reading "
    "order. Use sequence_number only as a hint.\n"
    "5. The 'unmatched' array is strictly for true standalone books. If a book "
    "has sequels or prequels in the real world, it MUST go into a series.\n"
    "6. The 'existing_series' field is metadata from third-party providers and "
    "is OFTEN WRONG. Treat genre-like values such as \"Children\", \"Kids\", "
    "\"Juvenile\", \"Fiction\", \"Audiobook\" as bogus and IGNORE them."
)


def _residue_books(
    residue: list[tuple[str, dict[str, Any]]],
) -> tuple[list[YotoBook], dict[str, str]]:
    """Build YotoBooks and a title->card_id map for a residue list.

    Both the prompt builder and the response parser derive their title mapping
    from the same residue, so titles round-trip back to the correct card ids.
    """
    books: list[YotoBook] = []
    title_to_card: dict[str, str] = {}
    for card_id, payload in residue:
        book = card_to_yoto_book(card_id, payload)
        books.append(book)
        # Last-write wins on title collisions; rare and acceptable for grouping.
        title_to_card[book.title] = card_id
    return books, title_to_card


def build_series_grouping_prompt(residue: list[tuple[str, dict[str, Any]]]) -> str:
    """Build a self-contained, copy-paste LLM prompt for grouping the residue.

    The prompt asks for a JSON object matching the shape ``suggest_series``
    produces, so a pasted response can be fed straight into
    :func:`apply_llm_grouping`.
    """
    books, _ = _residue_books(residue)
    library_entries = [
        {
            "title": book.title,
            "author": book.authors,
            "existing_series": book.series,
            "sequence_number": book.sequence_number,
        }
        for book in books
    ]
    return (
        _SERIES_GROUPING_INSTRUCTIONS
        + "\n\nRespond with ONLY a JSON object of this exact shape (no markdown, "
        "no commentary):\n"
        '{\n'
        '  "series": [\n'
        '    {"series_name": "Official Series Name", "titles": ["Exact Title 1", "Exact Title 2"]}\n'
        '  ],\n'
        '  "unmatched": ["Exact Standalone Title"]\n'
        '}\n\n'
        "Use the EXACT title strings from the entries below so they can be "
        "matched back to the library.\n\n"
        "Library entries to process:\n"
        + json.dumps(library_entries, indent=2, ensure_ascii=False)
    )


def apply_llm_grouping(
    residue: list[tuple[str, dict[str, Any]]],
    grouping: dict[str, Any],
) -> tuple[list[SeriesGroup], list[str]]:
    """Map an LLM grouping response back onto residue card ids.

    ``grouping`` is the parsed JSON returned by the model — either the
    ``suggest_series`` dict form (``{"series": {name: [titles]}}``) or the
    structured-output list form (``{"series": [{"series_name", "titles"}]}``).
    """
    if not residue:
        return [], []

    _, title_to_card = _residue_books(residue)

    series_map: dict[str, list[str]] = {}
    if isinstance(grouping, dict):
        raw_series = grouping.get("series") or {}
        if isinstance(raw_series, dict):
            series_map = {str(k): list(v or []) for k, v in raw_series.items()}
        elif isinstance(raw_series, list):
            for entry in raw_series:
                if isinstance(entry, dict):
                    name = entry.get("series_name") or entry.get("name")
                    titles = entry.get("titles") or []
                    if name:
                        series_map[str(name)] = list(titles)

    all_titles = list(title_to_card.keys())
    matched_set: set[str] = set()
    llm_groups: list[SeriesGroup] = []

    for series_name, titles in series_map.items():
        ids: list[str] = []
        for title in titles:
            cid = title_to_card.get(title)
            if cid is None:
                closest = get_close_matches(title, all_titles, n=1, cutoff=0.8)
                if closest:
                    cid = title_to_card[closest[0]]
            if cid and cid not in ids:
                ids.append(cid)
                matched_set.add(cid)
        if ids:
            llm_groups.append(SeriesGroup(series_name=series_name, card_ids=ids, provenance="llm"))

    unmatched = [card_id for card_id, _ in residue if card_id not in matched_set]
    return llm_groups, unmatched


async def group_cards_by_series(
    cards: list[tuple[str, dict[str, Any]]],
    meta_by_card: dict[str, dict[str, str]],
    *,
    llm_service: Any | None,
    abs_client: Any | None = None,
    library_id: str | None = None,
    quick_match_missing: bool = False,
) -> GroupingResult:
    """ABS-first card grouping with optional LLM fallback.

    Parameters
    ----------
    cards:
        ``(card_id, payload)`` tuples as returned by ``YotoStore.load_all_cards``.
    meta_by_card:
        ``{card_id: meta}`` mapping (``YotoStore.get_all_meta``). Used to
        identify ABS-backed cards via ``source`` / ``external_id``.
    llm_service:
        Optional LLM service. When ``None``, LLM fallback is skipped and any
        residue is returned as ``unmatched``.
    abs_client, library_id, quick_match_missing:
        When all three are provided and ``quick_match_missing`` is True, ABS
        quick-match is invoked on ABS-backed residue items in an attempt to
        populate their series before falling back to the LLM. This is
        best-effort and any errors are swallowed.
    """
    abs_groups, residue = _abs_group_from_cache(cards, meta_by_card)

    # Optional ABS quick-match enrichment for ABS-backed residue items.
    if quick_match_missing and abs_client is not None and library_id is not None and residue:
        new_residue: list[tuple[str, dict[str, Any]]] = []
        for card_id, payload in residue:
            meta = meta_by_card.get(card_id) or {}
            external_id = meta.get("external_id")
            if meta.get("source") != "abs" or not external_id:
                new_residue.append((card_id, payload))
                continue

            try:
                await abs_client.quick_match_item(external_id, override_defaults=False)
                refreshed = await abs_client.get_item(external_id)
                series_entries = (
                    (refreshed.get("media") or {}).get("metadata", {}).get("series") or []
                )
                first = series_entries[0] if series_entries else {}
                series_name = str((first or {}).get("name") or "").strip() or None
                if series_name and not _is_bogus_series(series_name):
                    # Slot directly into an ABS group; we don't bother
                    # rewriting the cached payload here — apply_series will
                    # PATCH ABS and refresh the cache on success.
                    for grp in abs_groups:
                        if grp.series_name.lower() == series_name.lower():
                            grp.card_ids.append(card_id)
                            break
                    else:
                        abs_groups.append(
                            SeriesGroup(
                                series_name=series_name,
                                card_ids=[card_id],
                                provenance="abs",
                            )
                        )
                    continue
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.debug("quick_match_item failed for %s: %s", external_id, exc)

            new_residue.append((card_id, payload))
        residue = new_residue

    llm_groups: list[SeriesGroup] = []
    unmatched: list[str] = [card_id for card_id, _ in residue]
    if llm_service is not None and residue:
        llm_groups, unmatched = _llm_group_residue(residue, llm_service)

    groups = abs_groups + llm_groups
    return GroupingResult(
        groups=groups,
        unmatched_card_ids=unmatched,
        abs_count=len(abs_groups),
        llm_count=len(llm_groups),
    )
