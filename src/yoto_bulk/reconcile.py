"""Reconciliation between local audiobook files and Yoto library entries."""

from __future__ import annotations

import logging
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from yoto_bulk.card_utils import normalize_title, word_set
from yoto_bulk.library import _extract_id3_metadata, _title_from_zip_name
from yoto_bulk.models import LocalAudiobook, ReconcileMatch, YotoBook

logger = logging.getLogger(__name__)


class LocalLibraryScanner:
    def scan(self, root: Path) -> list[LocalAudiobook]:
        items: list[LocalAudiobook] = []
        for path in sorted(root.rglob("*.zip")):
            stat = path.stat()
            items.append(
                LocalAudiobook(
                    path=path,
                    stem=path.stem,
                    size_bytes=stat.st_size,
                )
            )
        return items

    def inspect_zip_members(self, path: Path) -> list[str]:
        try:
            with ZipFile(path) as archive:
                return sorted(archive.namelist())
        except BadZipFile:
            return []


def _read_id3_album(zip_path: Path) -> str | None:
    """Read the ID3 album tag from the first MP3 inside a zip file."""
    try:
        with ZipFile(zip_path) as zf:
            mp3_entries = sorted(n for n in zf.namelist() if n.lower().endswith(".mp3"))
            if not mp3_entries:
                return None
            id3 = _extract_id3_metadata(zf, mp3_entries[0])
            album = id3.get("album")
            return album if album else None
    except (BadZipFile, Exception) as exc:
        logger.debug("Failed to read ID3 from %s: %s", zip_path.name, exc)
        return None


def _best_title_for_item(item: LocalAudiobook) -> str:
    """Return the best available title for a local audiobook.

    Priority: ID3 album tag > CamelCase-parsed zip filename.
    """
    id3_album = _read_id3_album(item.path)
    if id3_album:
        return id3_album
    return _title_from_zip_name(item.stem + ".zip")


class ReconcileService:
    def reconcile(
        self,
        local_items: list[LocalAudiobook],
        yoto_books: list[YotoBook],
    ) -> list[ReconcileMatch]:
        books_by_normalized_title = {
            normalize_title(book.title): book for book in yoto_books
        }
        # Pre-compute word sets for fuzzy matching
        books_by_words: list[tuple[set[str], YotoBook]] = [
            (word_set(book.title), book) for book in yoto_books
        ]

        matches: list[ReconcileMatch] = []
        for item in local_items:
            readable = _best_title_for_item(item)
            normalized = normalize_title(readable)

            # Exact normalized match
            matched = books_by_normalized_title.get(normalized)
            if matched:
                matches.append(
                    ReconcileMatch(
                        local_item=item,
                        yoto_book=matched,
                        confidence=1.0,
                        reason="exact-normalized-title",
                    )
                )
                continue

            # Fuzzy word-overlap match
            local_words = word_set(readable)
            best_match: YotoBook | None = None
            best_score = 0.0
            if len(local_words) >= 2:
                for yoto_words, book in books_by_words:
                    if not yoto_words:
                        continue
                    overlap = len(local_words & yoto_words)
                    union = len(local_words | yoto_words)
                    jaccard = overlap / union if union else 0.0
                    if jaccard > best_score:
                        best_score = jaccard
                        best_match = book

            if best_match and best_score >= 0.6:
                matches.append(
                    ReconcileMatch(
                        local_item=item,
                        yoto_book=best_match,
                        confidence=round(best_score, 2),
                        reason="fuzzy-word-overlap",
                    )
                )
            else:
                matches.append(
                    ReconcileMatch(
                        local_item=item,
                        yoto_book=None,
                        confidence=0.0,
                        reason="missing-on-yoto",
                    )
                )

        return matches
        return matches

