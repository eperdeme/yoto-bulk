"""Reconciliation between local audiobook files and Yoto library entries."""

from __future__ import annotations

from pathlib import Path
from zipfile import BadZipFile, ZipFile

from yoto_bulk.card_utils import normalize_title
from yoto_bulk.models import LocalAudiobook, ReconcileMatch, YotoBook


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


class ReconcileService:
    def reconcile(
        self,
        local_items: list[LocalAudiobook],
        yoto_books: list[YotoBook],
    ) -> list[ReconcileMatch]:
        books_by_normalized_title = {
            normalize_title(book.title): book for book in yoto_books
        }
        matches: list[ReconcileMatch] = []
        for item in local_items:
            normalized = normalize_title(item.stem)
            matched = books_by_normalized_title.get(normalized)
            matches.append(
                ReconcileMatch(
                    local_item=item,
                    yoto_book=matched,
                    confidence=1.0 if matched else 0.0,
                    reason="exact-normalized-title" if matched else "missing-on-yoto",
                )
            )
        return matches

