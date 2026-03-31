"""Shared helpers for extracting metadata from Yoto card payloads.

Every module that needs to read card titles, authors, series, etc. should
import from here so the extraction logic lives in one place.
"""

from __future__ import annotations

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


def resolve_series(card: dict[str, Any]) -> str | None:
    meta = metadata_block(card)
    raw = meta.get("series") or card.get("series")
    return raw if isinstance(raw, str) else None


def resolve_description(card: dict[str, Any]) -> str | None:
    meta = metadata_block(card)
    raw = meta.get("description") or card.get("description")
    return raw if isinstance(raw, str) else None


def resolve_sequence_number(card: dict[str, Any]) -> int | None:
    meta = metadata_block(card)
    raw = (
        meta.get("sequence_number")
        or card.get("sequence_number")
        or card.get("book_number")
        or card.get("order")
    )
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------
# Title normalization (single implementation for all callers)
# ------------------------------------------------------------------


def normalize_title(value: str) -> str:
    """Normalize a title for fuzzy matching.

    Lower-cases, strips non-alphanumeric characters, collapses whitespace.
    """
    filtered = [char.lower() if char.isalnum() else " " for char in value]
    return " ".join("".join(filtered).split())


# ------------------------------------------------------------------
# Payload sanitisation for the Yoto API
# ------------------------------------------------------------------


def truncate_for_api(payload: dict[str, Any]) -> dict[str, Any]:
    """Truncate title and description fields to Yoto API limits in-place."""
    meta = payload.get("metadata")
    if isinstance(meta, dict):
        title = meta.get("title")
        if isinstance(title, str) and len(title) > MAX_TITLE_LENGTH:
            meta["title"] = title[:MAX_TITLE_LENGTH]
        desc = meta.get("description")
        if isinstance(desc, str) and len(desc) > MAX_DESCRIPTION_LENGTH:
            meta["description"] = desc[:MAX_DESCRIPTION_LENGTH]

    top_title = payload.get("title")
    if isinstance(top_title, str) and len(top_title) > MAX_TITLE_LENGTH:
        payload["title"] = top_title[:MAX_TITLE_LENGTH]

    return payload


# ------------------------------------------------------------------
# YotoBook conversion
# ------------------------------------------------------------------


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
