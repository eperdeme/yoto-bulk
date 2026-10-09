"""Shared helpers for extracting metadata from Yoto card payloads.

Every module that needs to read card titles, authors, series, etc. should
import from here so the extraction logic lives in one place.
"""

from __future__ import annotations

import html
import re
from typing import Any

from yoto_bulk.models import YotoBook


# ------------------------------------------------------------------
# Yoto API field limits
# ------------------------------------------------------------------

MAX_TITLE_LENGTH = 50
MAX_DESCRIPTION_LENGTH = 500


# ------------------------------------------------------------------
# Standard Yoto numbered chapter icons (shared across all MYO cards)
# Index 0 = chapter 1, index 1 = chapter 2, etc.
# ------------------------------------------------------------------

STANDARD_CHAPTER_ICONS: list[str] = [
    "yoto:#InKMUu5w-OTU4K1LbvmZ533aSmldf-_Z-PjMXv24yUw",  # 1
    "yoto:#g2bCqc9B3ZGgd1aCCgSeUvpbLRHgU2UFeowtWLOHP0A",  # 2
    "yoto:#Lp0kH7FXAcL3RJ6d31oWfqO9rkAbOAqrrQlVir4bdt4",  # 3
    "yoto:#0H-Jh1ujRclugtZfXmTas84MTMfvkSYp7VeF8aEB1pw",  # 4
    "yoto:#rMTK7rXGlPH2XBSzAPdLB63uSJvcWEMaQnhT2CmYiP8",  # 5
    "yoto:#NEqSbTXGIEI8KwAOQuv1cn2xboLavOMjnpDaWV_d6Nw",  # 6
    "yoto:#oCHl1M8m7Exe9w_uKeuD5-6pgTYu3lzi3hlPqXUiY6E",  # 7
    "yoto:#l_lgrtn81ChnLFHA0m5lYvqXKC-Jt4R0z4P0aiLKYUg",  # 8
    "yoto:#zyiyRYt6dNMDaMYuXOkQQ8jN4C6_sE4oWzdCvc2_bE8",  # 9
    "yoto:#AXu4LhB9lb7kPPtDfJb1Q7pBmwNqtGHX2eLhBosAKd4",  # 10
    "yoto:#4PcvvM5CYc1nmeEHsWJcYcQW1jAbqePuQ97ccWGmPnA",  # 11
    "yoto:#CsIt18X9xr89kCL-Ul56ctslbxQT0-LrM2zzJgkpC5g",  # 12
    "yoto:#ZlfndSuvWt-3ww6cZ3q0DLA-rRZdKs8u1N0Ld2HIVDI",  # 13
    "yoto:#JUz6hvPH5T6EigjWxC01EL5HLvm7RZq9XE_D5Tg_X-4",  # 14
    "yoto:#c_H-W1ODpXZDoEfVHbpgGnqfsoxsn0airFm76QON0SQ",  # 15
    "yoto:#Pj-UytMwmQ9TPnpV9Mlgkr8u4Dp0z1imr5qNErqbaTY",  # 16
    "yoto:#3QWNlW4h2gTFeg70OWK2miSCHQ5l289RI7WuD8h49ho",  # 17
    "yoto:#pgsljINMdVOsLpqQgkWWRIfhROeu948l05FL1Ib6vYw",  # 18
    "yoto:#2Usm3Dsh7Y86Qr_81c4PKwhTXIjIJiQQ7f_HQ06BGHg",  # 19
    "yoto:#LqIRe47aDoLozLO8dGq-JOSQbiQYa1UjTHuKA7HhwnI",  # 20
    "yoto:#QMBnIEfJfklqsPD46osBnrCc_5xvrs9_w-zM_4kB8Tc",  # 21
    "yoto:#fNopSe_PsqnSzVbH2abysJAWgLgIwtMlD-aij4RMFuU",  # 22
    "yoto:#nPdAreUXVMhEoLlUNk8tC78ZbPds2F6hVtYVFnukEu0",  # 23
    "yoto:#5WxPsNpiueObJMYngnWvveoBl2YHFWqutJ_fn6Ty26o",  # 24
    "yoto:#5ndmduxNXvQlUq4ToPr8-VcHit_QWbFeqWQlg1PqweI",  # 25
    "yoto:#6rRWGLFT9DqA3uc4a3vRkuWcddKpgk-ZpEipeB-4qYg",  # 26
    "yoto:#Xupe_ZGYuZZ2I617gp3sYcgLG32-T5-107PdD1SdKAM",  # 27
    "yoto:#C3HF_APFyYVfTqsHijZ7Crus7H3GOqRdz2wIgoPhwNI",  # 28
    "yoto:#XJiJiGBImKJ6tuubNu_jB6U1vGwIWL2q3VttEAFOKFY",  # 29
    "yoto:#nQ8uyd1lYELI554L1HFRx3Jhl5Br5HdLWwPELKAUKK4",  # 30
    "yoto:#TKguQJT18n3xodGRrEtIqKy2FXtv2HkueuU1c81WSE4",  # 31
    "yoto:#qVZED88zteFESMqfqRHCKSPkXJa7ZN7ITvLhMx8nqUQ",  # 32
    "yoto:#D4me1xBZ202RVD-nlG2pv8CxQ4fPgWIJ5twub1RaJA4",  # 33
    "yoto:#PyZ4sy1rj5gg-O7qlvxw-ueaLI0Mo7LK6kcMiuevXWE",  # 34
    "yoto:#e_SB-j82Yyl25FWcBX7Y369d27qEoUD9pI8mYgTBdB8",  # 35
    "yoto:#9DHU0SH88D2HAHioeCsDojp7FBtEZT5b8z8hvXcs9qo",  # 36
    "yoto:#GOvYK66EirMuZZBf-cgperAd9TjqorhWsxnXofPzpaQ",  # 37
    "yoto:#TFqMSYQ0sszmAcuJgc1hAELCO7cRS-KTTFDJq1Y7ngE",  # 38
    "yoto:#UUEnrCA7HWT0-N4M7AEx8N1M6pMOeeXiEHFv_7UgLkA",  # 39
    "yoto:#cBs9k5qezUmzaUjeQBAfOT3aOEdQGLkl3Xyt78quRv4",  # 40
    "yoto:#Bf2KinalM8Tm9dxZmTwbT5rm66ZgkvbbWu6m6Eg51RI",  # 41
    "yoto:#F16_BtfmNXWxA49oNsCyK-owK3dZFaNEn229ev_nyfE",  # 42
    "yoto:#oSJHb5J_Hw8Wo2bjqmYFdC8HwWEchnp4uRSwD0EBQIQ",  # 43
    "yoto:#b60MQGw-lLFUwzRvkNRfshqEnmFf1Y_dlRGvhZfY4AA",  # 44
    "yoto:#rakEvKpPHFv8xg8UiWLY5i0Y3uTqe0BSJOMgc2iA8PY",  # 45
    "yoto:#nLHX057zSDLftA4DOgqESfB9SKMwvJvybpVnIfx4xLk",  # 46
    "yoto:#K0NbRHGqGwLdQKrxj_6c5tIWuLKpbvsoVpRCpCEJgPA",  # 47
    "yoto:#odr6b3bzarP7x7d5B4W-AwBVQjQTtwY_vPF0wCzOT6k",  # 48
    "yoto:#WY7j4aubI0RpZ8oEnQR1rl5HdlnOSqr86ZLVE7qVuF4",  # 49
    "yoto:#-4BjBxyg_G8O1e9m-MwOgCijWfznbS5pTi1vT425p1U",  # 50
    "yoto:#7RBHLQ5ZyKgCTnmFOgXzJ04vRZOAibRETL2Q7ImYXOk",  # 51
    "yoto:#Jcd19H0aC83iCPKN346-5gWRUUhJLOzIlTdcofB5ZWc",  # 52
    "yoto:#LlU3Zei-iwQtBTjqpD5NUBOI40TmrMi0avkcPWQB-_c",  # 53
    "yoto:#tgTFWlBATonYBfl--ywn41viZzeH9beoObL3g2HwE9k",  # 54
    "yoto:#pPzPv-N_T6vFaXE7qfHDn08cYOADu-OBTociw4dYk_A",  # 55
]


def enrich_payload_icons(payload: dict[str, Any]) -> bool:
    """Add standard numbered chapter icons where missing. Returns True if modified."""
    modified = False
    for idx, chapter in enumerate(payload.get("content", {}).get("chapters", [])):
        if idx >= len(STANDARD_CHAPTER_ICONS):
            break
        icon = STANDARD_CHAPTER_ICONS[idx]
        if not chapter.get("display", {}).get("icon16x16"):
            chapter.setdefault("display", {})["icon16x16"] = icon
            modified = True
        for track in chapter.get("tracks", []):
            if not track.get("display", {}).get("icon16x16"):
                track.setdefault("display", {})["icon16x16"] = icon
                modified = True
    return modified


# ------------------------------------------------------------------
# Primitive extractors
# ------------------------------------------------------------------


def metadata_block(card: dict[str, Any]) -> dict[str, Any]:
    """Return the ``metadata`` sub-dict (or empty dict)."""
    raw = card.get("metadata")
    return raw if isinstance(raw, dict) else {}


def ensure_metadata(card: dict[str, Any]) -> dict[str, Any]:
    """Return the ``metadata`` sub-dict, creating it if absent or non-dict."""
    meta = card.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
        card["metadata"] = meta
    return meta


def preserve_yoto_media(existing: dict[str, Any] | None, payload: dict[str, Any]) -> bool:
    """Copy uploaded Yoto media refs from an older payload into a refreshed one.

    ABS metadata edits rebuild card payloads from source data, where audio
    ``trackUrl`` values are intentionally blank until upload time. If the card
    already has Yoto-hosted media and the chapter/track shape still matches,
    preserve those refs so metadata-only edits do not re-upload the audiobook.
    """
    if not isinstance(existing, dict):
        return False

    modified = False
    raw_existing_meta = existing.get("metadata")
    existing_meta = raw_existing_meta if isinstance(raw_existing_meta, dict) else {}
    raw_existing_cover = existing_meta.get("cover")
    existing_cover = raw_existing_cover if isinstance(raw_existing_cover, dict) else {}
    existing_image = existing_cover.get("imageL") if isinstance(existing_cover, dict) else None
    if isinstance(existing_image, str) and existing_image and "yotoplay.com" in existing_image:
        meta = ensure_metadata(payload)
        cover = meta.get("cover") if isinstance(meta.get("cover"), dict) else None
        if cover is None:
            cover = {}
            meta["cover"] = cover
        if not cover.get("imageL"):
            cover["imageL"] = existing_image
            modified = True

    existing_chapters = (existing.get("content") or {}).get("chapters") or []
    new_chapters = (payload.get("content") or {}).get("chapters") or []
    if not isinstance(existing_chapters, list) or not isinstance(new_chapters, list):
        return modified

    existing_by_key = {
        str(chapter.get("key")): chapter
        for chapter in existing_chapters
        if isinstance(chapter, dict) and chapter.get("key") not in (None, "")
    }
    new_chapters_have_keys = all(
        isinstance(chapter, dict) and chapter.get("key") not in (None, "")
        for chapter in new_chapters
    )
    if existing_by_key and new_chapters_have_keys:
        chapter_pairs = (
            (existing_by_key.get(str(chapter.get("key"))), chapter)
            for chapter in new_chapters
        )
    elif len(existing_chapters) == len(new_chapters):
        chapter_pairs = zip(existing_chapters, new_chapters)
    else:
        return modified

    for existing_chapter, new_chapter in chapter_pairs:
        if not isinstance(existing_chapter, dict) or not isinstance(new_chapter, dict):
            continue

        existing_display = existing_chapter.get("display")
        if isinstance(existing_display, dict) and existing_display and not new_chapter.get("display"):
            new_chapter["display"] = dict(existing_display)
            modified = True

        existing_tracks = existing_chapter.get("tracks") or []
        new_tracks = new_chapter.get("tracks") or []
        if not isinstance(existing_tracks, list) or not isinstance(new_tracks, list):
            continue
        if len(existing_tracks) != len(new_tracks):
            continue

        for existing_track, new_track in zip(existing_tracks, new_tracks):
            if not isinstance(existing_track, dict) or not isinstance(new_track, dict):
                continue
            if new_track.get("trackUrl"):
                continue
            existing_url = existing_track.get("trackUrl")
            if not isinstance(existing_url, str) or not existing_url:
                continue

            for key in ("trackUrl", "duration", "fileSize", "channels", "format", "display"):
                if key in existing_track and existing_track[key] not in (None, ""):
                    value = existing_track[key]
                    new_track[key] = dict(value) if isinstance(value, dict) else value
                    modified = True

    return modified


def resolve_card_id(card: dict[str, Any]) -> str:
    card_id = card.get("cardId") or card.get("contentId") or card.get("id")
    return str(card_id).strip() if card_id else ""


def resolve_title(card: dict[str, Any]) -> str:
    meta = metadata_block(card)
    title = meta.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    for key in ("title", "name", "cardTitle"):
        value = card.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Untitled"


def resolve_author(card: dict[str, Any]) -> str | None:
    meta = metadata_block(card)
    author = meta.get("author")
    if isinstance(author, str):
        cleaned = author.strip()
        return cleaned or None
    return None


def resolve_authors(card: dict[str, Any]) -> list[str]:
    meta = metadata_block(card)
    raw = (
        meta.get("authors")
        or meta.get("author")
        or card.get("authors")
        or card.get("author")
        or card.get("authorsList")
        or []
    )
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(v) for v in raw]
    return []


_NOTE_SERIES_RE = re.compile(r"^(.+?)(?:\s+#(\d+))?\s*$")


def _parse_series_from_note(meta: dict[str, Any]) -> tuple[str | None, int | None]:
    """Extract series name and optional sequence from ``metadata.note``.

    Returns ``(series_name, sequence_number)`` or ``(None, None)``.
    """
    note = meta.get("note")
    if not isinstance(note, str) or not note.strip():
        return None, None
    m = _NOTE_SERIES_RE.match(note.strip())
    if not m:
        return None, None
    series = m.group(1).strip() or None
    seq = int(m.group(2)) if m.group(2) else None
    return series, seq


def resolve_series(card: dict[str, Any]) -> str | None:
    meta = metadata_block(card)
    raw = meta.get("series") or card.get("series")
    if isinstance(raw, str):
        return raw
    # Fall back to parsing the Yoto-persisted note field
    series, _ = _parse_series_from_note(meta)
    return series


def resolve_description(card: dict[str, Any]) -> str | None:
    meta = metadata_block(card)
    raw = meta.get("description") or card.get("description")
    if not isinstance(raw, str):
        return None
    return sanitize_description(raw)


def resolve_sequence_number(card: dict[str, Any]) -> int | None:
    meta = metadata_block(card)
    raw = (
        meta.get("sequence_number")
        or card.get("sequence_number")
        or card.get("book_number")
        or card.get("order")
    )
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    # Fall back to parsing the Yoto-persisted note field
    _, seq = _parse_series_from_note(meta)
    return seq


# ------------------------------------------------------------------
# Title normalization (single implementation for all callers)
# ------------------------------------------------------------------


def normalize_title(value: str) -> str:
    """Normalize a title for fuzzy matching.

    Lower-cases, strips non-alphanumeric characters, collapses whitespace.
    """
    filtered = [char.lower() if char.isalnum() else " " for char in value]
    return " ".join("".join(filtered).split())


_STOPWORDS = frozenset({"the", "a", "an", "and", "of", "in", "on", "by", "is", "not"})


def word_set(text: str) -> set[str]:
    """Lowercase word set for fuzzy/Jaccard matching, minus common stopwords."""
    return set(normalize_title(text).split()) - _STOPWORDS


def normalize_title_for_dedup(value: str) -> str:
    """Normalize a title for exact-match deduplication.

    Like ``normalize_title`` but also strips leading articles (the/a/an)
    so that "The Borrowers" and "Borrowers" are treated as equal.
    """
    import re

    t = normalize_title(value)
    return re.sub(r"^(the|a|an)\s+", "", t)


def sanitize_description(value: str) -> str:
    """Normalize HTML-rich descriptions into plain text with paragraph breaks."""
    unescaped = html.unescape(value).replace("\r\n", "\n").replace("\r", "\n")
    with_breaks = re.sub(r"(?i)<br\s*/?>", "\n", unescaped)
    with_breaks = re.sub(r"(?i)</p\s*>", "\n\n", with_breaks)
    stripped_tags = re.sub(r"<[^>]+>", "", with_breaks)
    stripped_tags = stripped_tags.replace("\xa0", " ")
    stripped_tags = re.sub(r"[ \t]+\n", "\n", stripped_tags)
    stripped_tags = re.sub(r"\n[ \t]+", "\n", stripped_tags)
    stripped_tags = re.sub(r"\n{3,}", "\n\n", stripped_tags)
    return stripped_tags.strip()


# ------------------------------------------------------------------
# Payload sanitisation for the Yoto API
# ------------------------------------------------------------------

# Valid metadata fields per https://yoto.dev/reference/card-content-schema/
_VALID_METADATA_KEYS = frozenset({
    "media", "accent", "author", "copyright", "readBy",
    "accents", "authors", "copyrights", "narrators",
    "category", "cover", "description", "error",
    "feedUrl", "genres", "languages", "maxAge", "minAge",
    "musicTypes", "note",
})

# Valid top-level card fields per the schema
_VALID_TOP_LEVEL_KEYS = frozenset({
    "cardId", "title", "slug", "metadata", "content",
})

# Valid content fields per the schema
_VALID_CONTENT_KEYS = frozenset({
    "activity", "chapters", "config", "playbackType", "version",
})


def truncate_for_api(payload: dict[str, Any]) -> dict[str, Any]:
    """Truncate title and description fields to Yoto API limits in-place."""
    meta = payload.get("metadata")
    if isinstance(meta, dict):
        desc = meta.get("description")
        if isinstance(desc, str):
            desc = sanitize_description(desc)
            if len(desc) > MAX_DESCRIPTION_LENGTH:
                desc = desc[:MAX_DESCRIPTION_LENGTH]
            meta["description"] = desc

    top_title = payload.get("title")
    if isinstance(top_title, str) and len(top_title) > MAX_TITLE_LENGTH:
        payload["title"] = top_title[:MAX_TITLE_LENGTH]

    return payload


def _build_note_from_series(meta: dict[str, Any]) -> str | None:
    """Build a note string from local-only series/sequence_number fields."""
    series = meta.get("series")
    seq = meta.get("sequence_number")
    if not series:
        return None
    if seq is not None:
        return f"{series} #{seq}"
    return series


def sanitize_for_yoto_api(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a deep-ish copy of *payload* with only Yoto-valid fields.

    Local-only fields (``series``, ``sequence_number``, ``metadata.title``)
    are stripped.  Series info is written to ``metadata.note`` so it
    persists on the Yoto side.

    Reference: https://yoto.dev/reference/card-content-schema/
    """
    out = {k: v for k, v in payload.items() if k in _VALID_TOP_LEVEL_KEYS}

    # Sanitise metadata
    meta = payload.get("metadata")
    if isinstance(meta, dict):
        clean_meta = {k: v for k, v in meta.items() if k in _VALID_METADATA_KEYS}
        # Write series info into the note field
        note = _build_note_from_series(meta)
        if note:
            clean_meta["note"] = note
        out["metadata"] = clean_meta

    # Sanitise content (strip unknown keys like 'restricted')
    content = payload.get("content")
    if isinstance(content, dict):
        out["content"] = {k: v for k, v in content.items() if k in _VALID_CONTENT_KEYS}

    return out


def card_to_yoto_book(card_id: str, payload: dict[str, Any]) -> YotoBook:
    """Convert a raw card payload dict to a ``YotoBook``."""
    return YotoBook(
        id=card_id,
        title=resolve_title(payload),
        authors=resolve_authors(payload),
        series=resolve_series(payload),
        sequence_number=resolve_sequence_number(payload),
        description=resolve_description(payload),
    )
