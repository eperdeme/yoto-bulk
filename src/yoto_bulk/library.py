"""Local library storage for offline card management."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from yoto_bulk.card_utils import (
    card_to_yoto_book,
    normalize_title,
    normalize_title_for_dedup,
    resolve_author,
    resolve_authors,
    resolve_series,
    resolve_sequence_number,
    resolve_title,
    word_set,
)
from yoto_bulk.models import YotoBook

logger = logging.getLogger(__name__)


class LocalLibrary:
    """Manages local card storage with a human-readable filesystem layout.

    Layout::

        library/
          .yoto-bulk/
            manifest.json            # card_id -> {path, pull_hash, …}
          Author Name/
            Book Title [cardId]/
              card.json              # full Yoto API payload
              cover.{ext}
              audio/
                01 - Chapter 1.{ext}
              icons/
                01 - Chapter 1.png
            Series Name/             # when series metadata exists
              01 - Book Title [cardId]/
                card.json
                …

    The ``[cardId]`` suffix on each book folder makes the mapping back to
    Yoto unambiguous and enables round-trip recovery.
    """

    def __init__(self, library_dir: Path) -> None:
        self.library_dir = library_dir
        self._meta_dir = library_dir / ".yoto-bulk"
        self.manifest_path = self._meta_dir / "manifest.json"

    # ------------------------------------------------------------------
    # Directory / manifest helpers
    # ------------------------------------------------------------------

    def ensure_directories(self) -> None:
        self.library_dir.mkdir(parents=True, exist_ok=True)
        self._meta_dir.mkdir(parents=True, exist_ok=True)

    def load_manifest(self) -> dict[str, Any]:
        if self.manifest_path.exists():
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        return {"cards": {}}

    def save_manifest(self, manifest: dict[str, Any]) -> None:
        self._meta_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # ------------------------------------------------------------------
    # Path building
    # ------------------------------------------------------------------

    def book_dir_for_card(self, card_id: str, payload: dict[str, Any]) -> Path:
        """Compute the human-readable directory path for a card.

        Returns a path like ``library/Author/Title [cardId]`` or
        ``library/Author/Series/01 - Title [cardId]``.
        """
        author = resolve_author(payload)
        authors = resolve_authors(payload)
        author_name = _sanitise_dirname(author or (authors[0] if authors else "Unknown Author"))

        title = resolve_title(payload)
        series = resolve_series(payload)
        seq = resolve_sequence_number(payload)

        if series:
            series_dir = _sanitise_dirname(series)
            prefix = f"{seq:02d} - " if seq else ""
            book_folder = f"{prefix}{_sanitise_dirname(title)} [{card_id}]"
            return self.library_dir / author_name / series_dir / book_folder
        else:
            book_folder = f"{_sanitise_dirname(title)} [{card_id}]"
            return self.library_dir / author_name / book_folder

    def _resolve_book_dir(self, card_id: str, payload: dict[str, Any] | None = None) -> Path:
        """Find the actual book directory for a card_id.

        Checks the manifest first (fast); falls back to computing from payload.
        """
        manifest = self.load_manifest()
        entry = manifest.get("cards", {}).get(card_id)
        if entry and "path" in entry:
            candidate = self.library_dir / entry["path"]
            if candidate.exists():
                return candidate

        if payload is not None:
            return self.book_dir_for_card(card_id, payload)

        # Last resort: scan for [cardId] suffix
        for p in self.library_dir.rglob(f"* [{card_id}]"):
            if p.is_dir() and (p / "card.json").exists():
                return p

        raise FileNotFoundError(f"Cannot locate book directory for card {card_id}")

    # ------------------------------------------------------------------
    # Card I/O
    # ------------------------------------------------------------------

    @property
    def versions_dir(self) -> Path:
        """Directory where pre-push card backups are stored."""
        return self._meta_dir / "versions"

    def backup_card(self, card_id: str) -> Path | None:
        """Save a timestamped backup of a card's payload before pushing.

        Returns the backup file path, or ``None`` when the card cannot be
        loaded.  Backups are stored under
        ``.yoto-bulk/versions/{cardId}/{timestamp}.json``.
        """
        payload = self.load_card(card_id)
        if payload is None:
            return None
        dest_dir = self.versions_dir / card_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        dest = dest_dir / f"{ts}.json"
        dest.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return dest

    def list_backups(self, card_id: str) -> list[Path]:
        """Return saved backups for a card (newest first)."""
        dest_dir = self.versions_dir / card_id
        if not dest_dir.exists():
            return []
        return sorted(dest_dir.glob("*.json"), reverse=True)

    def card_path(self, card_id: str, payload: dict[str, Any] | None = None) -> Path:
        """Return the path to the card.json file for a given card."""
        book_dir = self._resolve_book_dir(card_id, payload)
        return book_dir / "card.json"

    def save_card(
        self,
        card_id: str,
        payload: dict[str, Any],
        *,
        update_pull_hash: bool = False,
    ) -> None:
        """Write a card payload to its human-readable directory.

        Creates the ``Author/Title [cardId]/`` folder structure and writes
        ``card.json``.  When *update_pull_hash* is ``True`` the manifest
        entry is (re)set so the current content is the clean baseline.
        """
        book_dir = self.book_dir_for_card(card_id, payload)

        # If the card already exists at a different path, move it
        manifest = self.load_manifest()
        old_entry = manifest.get("cards", {}).get(card_id)
        if old_entry and "path" in old_entry:
            old_dir = self.library_dir / old_entry["path"]
            if old_dir.exists() and old_dir != book_dir:
                logger.info("Moving %s → %s", old_dir, book_dir)
                book_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_dir), str(book_dir))
                _cleanup_empty_parents(old_dir, self.library_dir)

        book_dir.mkdir(parents=True, exist_ok=True)
        (book_dir / "card.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Update manifest — preserve existing fields (e.g. source, pull_hash)
        rel_path = book_dir.relative_to(self.library_dir).as_posix()
        existing = manifest.get("cards", {}).get(card_id, {})
        existing.update({
            "title": resolve_title(payload),
            "path": rel_path,
            "updated_at": datetime.now(UTC).isoformat(),
        })
        manifest.setdefault("cards", {})[card_id] = existing
        if update_pull_hash:
            manifest["cards"][card_id]["pull_hash"] = content_hash(payload)
            manifest["cards"][card_id]["pulled_at"] = datetime.now(UTC).isoformat()
            manifest["cards"][card_id].pop("source", None)
        self.save_manifest(manifest)

    def load_card(self, card_id: str) -> dict[str, Any] | None:
        try:
            path = self.card_path(card_id)
        except FileNotFoundError:
            return None
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_card_ids(self) -> list[str]:
        """Return all card IDs known to the manifest."""
        manifest = self.load_manifest()
        return sorted(manifest.get("cards", {}).keys())

    def load_all_cards(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            (cid, card)
            for cid in self.list_card_ids()
            if (card := self.load_card(cid)) is not None
        ]

    # ------------------------------------------------------------------
    # Change detection
    # ------------------------------------------------------------------

    def modified_cards(self) -> list[tuple[str, dict[str, Any], str]]:
        """Return ``(card_id, payload, title)`` for cards that differ from their pull hash.

        Excludes imported cards (no pull hash, ``source: import``) — use
        :meth:`imported_cards` for those.
        """
        manifest = self.load_manifest()
        result: list[tuple[str, dict[str, Any], str]] = []
        for card_id, payload in self.load_all_cards():
            entry = manifest.get("cards", {}).get(card_id)
            if entry is None:
                continue
            if entry.get("source") == "import":
                continue
            if content_hash(payload) != entry.get("pull_hash"):
                result.append((card_id, payload, resolve_title(payload)))
        return result

    def imported_cards(self) -> list[tuple[str, dict[str, Any], str]]:
        """Return ``(card_id, payload, title)`` for cards imported from zip files.

        These are cards with ``source: import`` in the manifest that have
        not yet been pushed to Yoto.
        """
        manifest = self.load_manifest()
        result: list[tuple[str, dict[str, Any], str]] = []
        for card_id, payload in self.load_all_cards():
            entry = manifest.get("cards", {}).get(card_id)
            if entry and entry.get("source") == "import":
                result.append((card_id, payload, resolve_title(payload)))
        return result

    def _mark_imported(self, card_id: str) -> None:
        """Tag a manifest entry as an import (no pull hash)."""
        manifest = self.load_manifest()
        entry = manifest.get("cards", {}).get(card_id)
        if entry:
            entry["source"] = "import"
            entry.pop("pull_hash", None)
            entry.pop("pulled_at", None)
            self.save_manifest(manifest)

    def remove_card(self, card_id: str) -> bool:
        """Delete a card's directory and manifest entry.

        Returns ``True`` if the card was found and removed.
        """
        manifest = self.load_manifest()
        entry = manifest.get("cards", {}).get(card_id)
        if entry is None:
            return False

        # Remove directory
        if "path" in entry:
            book_dir = self.library_dir / entry["path"]
            if book_dir.exists():
                shutil.rmtree(book_dir)
                _cleanup_empty_parents(book_dir, self.library_dir)

        del manifest["cards"][card_id]
        self.save_manifest(manifest)
        return True

    def rename_card(
        self, old_card_id: str, new_card_id: str, payload: dict[str, Any]
    ) -> None:
        """Re-save a card under a new ID, moving its directory (with media).

        This preserves audio, cover, and icon files that live alongside
        ``card.json``.  The old manifest entry is removed.
        """
        manifest = self.load_manifest()
        old_entry = manifest.get("cards", {}).get(old_card_id)

        new_book_dir = self.book_dir_for_card(new_card_id, payload)

        # Move the old directory to the new location (preserving media)
        if old_entry and "path" in old_entry:
            old_dir = self.library_dir / old_entry["path"]
            if old_dir.exists() and old_dir != new_book_dir:
                new_book_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_dir), str(new_book_dir))
                _cleanup_empty_parents(old_dir, self.library_dir)

        # Remove old manifest entry
        manifest.get("cards", {}).pop(old_card_id, None)
        self.save_manifest(manifest)

        # Write updated card.json and create new manifest entry
        self.save_card(new_card_id, payload, update_pull_hash=True)

    def fix_imports(self) -> dict[str, Any]:
        """One-shot repair of existing imports.

        1. Removes non-audiobook junk (VS Code zips, etc.).
        2. Removes imports that duplicate a pulled card (Jaccard word overlap).
        3. Re-parses remaining import titles through ``_parse_album_series``
           to populate ``metadata.series`` and ``metadata.sequence_number``.
        4. Re-saves cards so folders move to the correct layout.

        Returns a summary dict.
        """
        manifest = self.load_manifest()
        cards = manifest.get("cards", {})

        # Partition into pulled and imported
        pulled_titles: dict[str, str] = {}  # card_id -> title
        import_ids: list[str] = []
        for cid, entry in cards.items():
            if entry.get("source") == "import":
                import_ids.append(cid)
            else:
                card = self.load_card(cid)
                if card:
                    pulled_titles[cid] = resolve_title(card)

        removed_junk: list[str] = []
        removed_dupes: list[dict[str, str]] = []
        updated: list[dict[str, Any]] = []

        def _is_dupe(imp_words: set[str], pull_title: str) -> tuple[bool, float]:
            """Check Jaccard overlap ≥ 0.5 OR subset match (min 2 words)."""
            pull_words = word_set(pull_title)
            if not pull_words or not imp_words:
                return False, 0.0
            overlap = len(imp_words & pull_words)
            union = len(imp_words | pull_words)
            jaccard = overlap / union if union else 0.0
            smaller = min(len(imp_words), len(pull_words))
            is_subset = smaller >= 2 and (imp_words <= pull_words or pull_words <= imp_words)
            return (jaccard >= 0.5 or is_subset), jaccard

        for cid in list(import_ids):
            card = self.load_card(cid)
            if card is None:
                self.remove_card(cid)
                removed_junk.append(cid)
                continue

            raw_title = resolve_title(card)

            # 1. Remove junk (no chapters or clearly non-audiobook title)
            chapters = (card.get("content") or {}).get("chapters") or []
            is_junk = (
                not chapters
                or "vs code" in raw_title.lower()
                or "visual studio" in raw_title.lower()
            )
            if is_junk:
                self.remove_card(cid)
                removed_junk.append(raw_title)
                continue

            # 2. Parse title for series metadata first (clean title for matching)
            parsed = _parse_album_series(raw_title)
            clean_title = parsed["title"]

            # 3. Check for duplicate against pulled library using clean title
            imp_words = word_set(clean_title)
            best_score = 0.0
            best_pull_title = ""
            best_pull_id = ""
            is_dupe = False
            for pull_id, pull_title in pulled_titles.items():
                matched, jaccard = _is_dupe(imp_words, pull_title)
                if jaccard > best_score or (matched and not is_dupe):
                    best_score = jaccard
                    best_pull_title = pull_title
                    best_pull_id = pull_id
                    if matched:
                        is_dupe = True

            if is_dupe:
                self.remove_card(cid)
                removed_dupes.append({
                    "imported_title": raw_title,
                    "pulled_title": best_pull_title,
                    "pulled_id": best_pull_id,
                    "score": f"{best_score:.0%}",
                })
                continue

            # 4. Apply series metadata to kept cards
            from yoto_bulk.card_utils import MAX_TITLE_LENGTH

            changed = False
            capped_title = clean_title[:MAX_TITLE_LENGTH]

            if capped_title != raw_title:
                card["title"] = capped_title
                if "metadata" in card and isinstance(card["metadata"], dict):
                    card["metadata"]["title"] = capped_title
                changed = True

            if "series" in parsed:
                card.setdefault("metadata", {})["series"] = parsed["series"]
                changed = True
            if "sequence_number" in parsed:
                card.setdefault("metadata", {})["sequence_number"] = parsed["sequence_number"]
                changed = True

            if changed:
                self.save_card(cid, card)
                self._mark_imported(cid)
                updated.append({
                    "card_id": cid,
                    "title": clean_title,
                    "series": parsed.get("series"),
                    "sequence_number": parsed.get("sequence_number"),
                })

        return {
            "removed_junk": removed_junk,
            "removed_dupes": removed_dupes,
            "updated": updated,
            "summary": {
                "junk_removed": len(removed_junk),
                "dupes_removed": len(removed_dupes),
                "updated_with_series": len(updated),
            },
        }

    # ------------------------------------------------------------------
    # YotoBook bridge
    # ------------------------------------------------------------------

    def as_yoto_books(self) -> list[YotoBook]:
        """Parse every local card into a ``YotoBook`` for LLM/enrichment use."""
        return [card_to_yoto_book(cid, card) for cid, card in self.load_all_cards()]

    # ------------------------------------------------------------------
    # Media download
    # ------------------------------------------------------------------

    def _media_dirs(self, card_id: str, payload: dict[str, Any]) -> tuple[Path, Path, Path]:
        """Return (book_dir, audio_dir, icons_dir) for a card."""
        book_dir = self._resolve_book_dir(card_id, payload)
        return book_dir, book_dir / "audio", book_dir / "icons"

    def audio_files_for_card(self, card_id: str) -> list[Path]:
        """Return sorted list of local audio files for a card."""
        manifest = self.load_manifest()
        entry = manifest.get("cards", {}).get(card_id)
        if not entry or "path" not in entry:
            return []
        audio_dir = self.library_dir / entry["path"] / "audio"
        if not audio_dir.is_dir():
            return []
        exts = {".mp3", ".m4a", ".ogg", ".wav", ".flac", ".aac"}
        return sorted(f for f in audio_dir.iterdir() if f.suffix.lower() in exts)

    def cover_file_for_card(self, card_id: str) -> Path | None:
        """Return path to local cover image file, or None."""
        manifest = self.load_manifest()
        entry = manifest.get("cards", {}).get(card_id)
        if not entry or "path" not in entry:
            return None
        book_dir = self.library_dir / entry["path"]
        for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            cover = book_dir / f"cover{ext}"
            if cover.exists():
                return cover
        return None

    def enrich_cards(self) -> dict[str, Any]:
        """Add missing display icons to all local cards.

        Writes card.json directly (without updating the manifest) so cards
        with changed content show as modified for the next push.
        """
        from yoto_bulk.card_utils import enrich_payload_icons

        enriched: list[dict[str, str]] = []
        for card_id, payload in self.load_all_cards():
            if enrich_payload_icons(payload):
                path = self.card_path(card_id)
                path.write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                enriched.append({"card_id": card_id, "title": resolve_title(payload)})
        return {"enriched": len(enriched), "cards": enriched}

    async def download_media(
        self,
        card_id: str,
        resolved_payload: dict[str, Any],
        client: httpx.AsyncClient,
    ) -> dict[str, Any]:
        """Download cover, icons, and audio for a card using resolved URLs.

        *resolved_payload* should come from the Yoto API with
        ``playable=True`` so that ``yoto:#`` references have been expanded
        to signed download URLs.

        Media files are stored alongside ``card.json`` in the book's
        human-readable directory.
        """
        book_dir, audio_dir, icons_dir = self._media_dirs(card_id, resolved_payload)
        stats: dict[str, Any] = {"cover": False, "audio": 0, "icons": 0, "errors": []}

        # --- Cover ---
        cover_url = (
            resolved_payload.get("metadata", {}).get("cover", {}).get("imageL")
        )
        if cover_url and cover_url.startswith("http"):
            ext = _ext_from_url(cover_url, default=".png")
            dest = book_dir / f"cover{ext}"
            if not dest.exists():
                book_dir.mkdir(parents=True, exist_ok=True)
                ok = await _download_file(client, cover_url, dest)
                if ok:
                    stats["cover"] = True
                else:
                    stats["errors"].append(f"cover: {cover_url}")
            else:
                stats["cover"] = True

        # --- Chapters: audio + icons ---
        chapters = resolved_payload.get("content", {}).get("chapters", [])
        for ch_idx, chapter in enumerate(chapters):
            ch_key = chapter.get("key", f"{ch_idx:02d}")
            ch_num = f"{ch_idx + 1:02d}"
            ch_title = _sanitise_filename(chapter.get("title", f"Chapter {ch_idx + 1}"))

            # Icon
            icon_url = chapter.get("display", {}).get("icon16x16", "")
            if icon_url and icon_url.startswith("http"):
                ext = _ext_from_url(icon_url, default=".png")
                icon_dest = icons_dir / f"{ch_num} - {ch_title}{ext}"
                if not icon_dest.exists():
                    icons_dir.mkdir(parents=True, exist_ok=True)
                    ok = await _download_file(client, icon_url, icon_dest)
                    if ok:
                        stats["icons"] += 1
                    else:
                        stats["errors"].append(f"icon ch{ch_key}: {icon_url}")
                else:
                    stats["icons"] += 1

            # Audio tracks
            for track in chapter.get("tracks", []):
                track_url = track.get("trackUrl", "")
                if not track_url or not track_url.startswith("http"):
                    continue
                fmt = track.get("format", "")
                ext = _format_to_ext(fmt) or _ext_from_url(track_url, default=".m4a")
                audio_dest = audio_dir / f"{ch_num} - {ch_title}{ext}"
                if not audio_dest.exists():
                    audio_dir.mkdir(parents=True, exist_ok=True)
                    ok = await _download_file(client, track_url, audio_dest)
                    if ok:
                        stats["audio"] += 1
                    else:
                        stats["errors"].append(f"audio ch{ch_key}: {track_url[:80]}")
                else:
                    stats["audio"] += 1

        return stats

    # ------------------------------------------------------------------
    # Migration from old layout
    # ------------------------------------------------------------------

    def migrate_from_old_layout(self) -> dict[str, Any]:
        """Migrate from the old ``cards/{id}.json`` + ``media/{id}/`` layout.

        Returns a summary of what was migrated.
        """
        old_cards_dir = self.library_dir / "cards"
        old_media_dir = self.library_dir / "media"
        old_manifest = self.library_dir / "manifest.json"

        if not old_cards_dir.exists():
            return {"status": "no_old_layout", "migrated": 0}

        summary: dict[str, Any] = {"migrated": 0, "failed": 0, "results": []}

        for card_file in sorted(old_cards_dir.glob("*.json")):
            card_id = card_file.stem
            try:
                payload = json.loads(card_file.read_text(encoding="utf-8"))
                book_dir = self.book_dir_for_card(card_id, payload)
                book_dir.mkdir(parents=True, exist_ok=True)

                # Move card.json
                shutil.copy2(str(card_file), str(book_dir / "card.json"))

                # Move media if it exists
                old_media = old_media_dir / card_id
                if old_media.exists():
                    # Cover
                    for cover in old_media.glob("cover.*"):
                        shutil.copy2(str(cover), str(book_dir / cover.name))

                    # Audio — rename from old ch_key-based to sequential
                    old_audio = old_media / "audio"
                    if old_audio.exists():
                        new_audio = book_dir / "audio"
                        new_audio.mkdir(exist_ok=True)
                        for f in sorted(old_audio.iterdir()):
                            if f.is_file():
                                new_name = _remap_chapter_filename(f.name)
                                shutil.copy2(str(f), str(new_audio / new_name))

                    # Icons — same remapping
                    old_icons = old_media / "icons"
                    if old_icons.exists():
                        new_icons = book_dir / "icons"
                        new_icons.mkdir(exist_ok=True)
                        for f in sorted(old_icons.iterdir()):
                            if f.is_file():
                                new_name = _remap_chapter_filename(f.name)
                                shutil.copy2(str(f), str(new_icons / new_name))

                # Update manifest
                self.save_card(card_id, payload, update_pull_hash=True)
                summary["migrated"] += 1
                summary["results"].append({
                    "card_id": card_id,
                    "title": resolve_title(payload),
                    "path": book_dir.relative_to(self.library_dir).as_posix(),
                })
            except Exception as exc:
                logger.warning("Failed to migrate %s: %s", card_id, exc)
                summary["failed"] += 1
                summary["results"].append({"card_id": card_id, "status": "failed", "error": str(exc)})

        # Clean up old layout after successful migration
        if summary["failed"] == 0:
            shutil.rmtree(old_cards_dir, ignore_errors=True)
            shutil.rmtree(old_media_dir, ignore_errors=True)
            if old_manifest.exists():
                old_manifest.unlink(missing_ok=True)
            summary["status"] = "completed"
            summary["old_layout_removed"] = True
        else:
            summary["status"] = "partial"
            summary["old_layout_removed"] = False

        return summary

    # ------------------------------------------------------------------
    # Zip import
    # ------------------------------------------------------------------

    def import_from_zip(self, zip_path: Path, title: str | None = None) -> dict[str, Any]:
        """Import an audiobook zip file into the local library.

        Supports two zip formats:
        1. **Yoto export** — contains ``data/card-data.json`` with the full
           Yoto API payload plus ``audio/``, ``icons/``, ``cover/`` folders.
        2. **Publisher/raw** — contains MP3/M4A files (flat or in a
           subfolder), optionally with a PDF user guide.

        Returns a summary dict with the card_id and import details.
        """
        import zipfile

        if not zip_path.exists():
            raise FileNotFoundError(f"Zip file not found: {zip_path}")

        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()

            # Detect format
            card_data_entry = next((n for n in names if n.endswith("card-data.json")), None)
            if card_data_entry:
                return self._import_yoto_export(zf, names, card_data_entry)
            else:
                return self._import_raw_audio(zf, names, zip_path, title)

    def _find_existing_by_title(self, title: str) -> str | None:
        """Return an existing card_id whose title normalises to the same string."""
        target = _normalise_for_dedup(title)
        if not target:
            return None
        for card_id, payload in self.load_all_cards():
            existing = _normalise_for_dedup(resolve_title(payload))
            if existing == target:
                return card_id
        return None

    def _import_yoto_export(
        self,
        zf: zipfile.ZipFile,
        names: list[str],
        card_data_entry: str,
    ) -> dict[str, Any]:
        """Import a zip that was exported from the Yoto app/tools."""
        payload = json.loads(zf.read(card_data_entry))
        card_id = payload.get("cardId", "")
        if not card_id:
            raise ValueError("card-data.json missing cardId field")

        # Dupe detection — same cardId already in library
        if card_id in self.list_card_ids():
            return {
                "status": "skipped",
                "reason": "duplicate",
                "card_id": card_id,
                "title": resolve_title(payload),
            }

        book_dir = self.book_dir_for_card(card_id, payload)
        book_dir.mkdir(parents=True, exist_ok=True)

        # Write card.json
        (book_dir / "card.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Extract audio files
        audio_entries = sorted(n for n in names if _is_audio_file(n))
        if audio_entries:
            audio_dir = book_dir / "audio"
            audio_dir.mkdir(exist_ok=True)
            for idx, entry in enumerate(audio_entries):
                ext = Path(entry).suffix
                src_name = Path(entry).name
                dest_name = f"{idx + 1:02d} - {_sanitise_filename(Path(src_name).stem)}{ext}"
                dest = audio_dir / dest_name
                if not dest.exists():
                    dest.write_bytes(zf.read(entry))

        # Extract icons
        icon_entries = sorted(n for n in names if _is_icon_file(n))
        if icon_entries:
            icons_dir = book_dir / "icons"
            icons_dir.mkdir(exist_ok=True)
            for idx, entry in enumerate(icon_entries):
                ext = Path(entry).suffix
                src_name = Path(entry).name
                dest_name = f"{idx + 1:02d} - {_sanitise_filename(Path(src_name).stem)}{ext}"
                dest = icons_dir / dest_name
                if not dest.exists():
                    dest.write_bytes(zf.read(entry))

        # Extract cover
        cover_entries = [n for n in names if "cover" in n.lower() and _is_image_file(n)]
        if cover_entries:
            cover_entry = cover_entries[0]
            ext = Path(cover_entry).suffix
            dest = book_dir / f"cover{ext}"
            if not dest.exists():
                dest.write_bytes(zf.read(cover_entry))

        self.save_card(card_id, payload)
        self._mark_imported(card_id)

        return {
            "status": "imported",
            "format": "yoto_export",
            "card_id": card_id,
            "title": resolve_title(payload),
            "path": book_dir.relative_to(self.library_dir).as_posix(),
            "audio_files": len(audio_entries),
            "icon_files": len(icon_entries),
            "has_cover": bool(cover_entries),
        }

    def _import_raw_audio(
        self,
        zf: "zipfile.ZipFile",
        names: list[str],
        zip_path: Path,
        title_override: str | None,
    ) -> dict[str, Any]:
        """Import a zip containing raw audio files (publisher format).

        Reads ID3 tags from the first MP3 to extract album title, author,
        track titles, total track count, and embedded cover art.  Falls
        back to filename-based heuristics when tags are missing.
        """
        audio_entries = sorted(n for n in names if _is_audio_file(n))
        if not audio_entries:
            raise ValueError(f"No audio files found in {zip_path.name}")

        # --- Read ID3 metadata from the first MP3 ---
        id3 = _extract_id3_metadata(zf, audio_entries[0])

        # Reject zips that don't look like audiobooks: no album tag AND no
        # audiobook genre tag means this is probably an app installer or
        # other non-audiobook archive.  A --title override forces import.
        if not title_override and not id3.get("album") and not id3.get("genre"):
            raise ValueError(
                f"No ID3 album or genre tag in {zip_path.name} — "
                "doesn't look like an audiobook. Use --title to force import."
            )

        # Title priority: CLI override > ID3 album > zip filename
        raw_album = title_override or id3.get("album") or _title_from_zip_name(zip_path.name)
        author = id3.get("author", "")

        # Parse series info from the album tag (e.g. "Hilda and the Ghost Ship: Hilda 5")
        parsed = _parse_album_series(raw_album)
        raw_title = parsed["title"]
        series_name = parsed.get("series")
        sequence_number = parsed.get("sequence_number")

        # Dupe detection — check if a card with this title already exists
        existing_id = self._find_existing_by_title(raw_title)
        if existing_id:
            return {
                "status": "skipped",
                "reason": "duplicate",
                "card_id": existing_id,
                "title": raw_title,
                "matched_by": "title",
            }

        # Build chapter list, preferring ID3 track titles over filenames
        from yoto_bulk.card_utils import STANDARD_CHAPTER_ICONS

        chapters = []
        for idx, entry in enumerate(audio_entries):
            ch_title = _chapter_title_from_filename(Path(entry).stem, idx)
            # For the first track we already have ID3 data; for others
            # we'd need to read each one.  Read all if the zip isn't huge.
            if idx == 0 and id3.get("track_title"):
                ch_title = id3["track_title"]
            display = {}
            if idx < len(STANDARD_CHAPTER_ICONS):
                display = {"icon16x16": STANDARD_CHAPTER_ICONS[idx]}
            chapters.append({
                "key": f"{idx:02d}",
                "title": ch_title,
                "overlayLabel": str(idx + 1),
                "display": display,
                "tracks": [{
                    "key": "01",
                    "title": ch_title,
                    "type": "audio",
                    "format": _guess_format(Path(entry).suffix),
                    "trackUrl": "",
                    "overlayLabel": str(idx + 1),
                    "display": display,
                }],
            })

        # Read ID3 track titles for remaining tracks
        if len(audio_entries) > 1:
            for idx, entry in enumerate(audio_entries[1:], start=1):
                trk = _extract_id3_metadata(zf, entry)
                if trk.get("track_title"):
                    chapters[idx]["title"] = trk["track_title"]
                    chapters[idx]["tracks"][0]["title"] = trk["track_title"]

        from yoto_bulk.card_utils import MAX_TITLE_LENGTH

        payload: dict[str, Any] = {
            "title": raw_title[:MAX_TITLE_LENGTH],
            "metadata": {
                "author": author,
            },
            "content": {
                "activity": "yoto_Player",
                "chapters": chapters,
                "config": {"resumeTimeout": 2592000, "onlineOnly": False},
                "playbackType": "linear",
                "restricted": True,
                "version": "1",
            },
        }

        if series_name:
            payload["metadata"]["series"] = series_name
        if sequence_number is not None:
            payload["metadata"]["sequence_number"] = sequence_number

        # Use a temporary ID until pushed to Yoto
        import secrets
        temp_id = f"import-{secrets.token_urlsafe(6)}"
        payload["cardId"] = temp_id

        book_dir = self.book_dir_for_card(temp_id, payload)
        book_dir.mkdir(parents=True, exist_ok=True)

        # Write card.json
        (book_dir / "card.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Extract audio files
        audio_dir = book_dir / "audio"
        audio_dir.mkdir(exist_ok=True)
        for idx, entry in enumerate(audio_entries):
            ext = Path(entry).suffix
            ch_title = chapters[idx]["title"]
            dest_name = f"{idx + 1:02d} - {_sanitise_filename(ch_title)}{ext}"
            dest = audio_dir / dest_name
            if not dest.exists():
                dest.write_bytes(zf.read(entry))

        # Extract embedded cover art from ID3 if present
        cover_data = id3.get("cover_data")
        cover_mime = id3.get("cover_mime", "")
        if cover_data:
            ext = ".jpg" if "jpeg" in cover_mime else ".png"
            cover_dest = book_dir / f"cover{ext}"
            if not cover_dest.exists():
                cover_dest.write_bytes(cover_data)

        self.save_card(temp_id, payload)
        self._mark_imported(temp_id)

        return {
            "status": "imported",
            "format": "raw_audio",
            "card_id": temp_id,
            "title": raw_title,
            "author": author,
            "series": series_name,
            "sequence_number": sequence_number,
            "path": book_dir.relative_to(self.library_dir).as_posix(),
            "audio_files": len(audio_entries),
            "has_cover": bool(cover_data),
            "metadata_source": "id3" if id3.get("album") else "filename",
        }


# ------------------------------------------------------------------
# Pure helpers (module-level so they're easy to test)
# ------------------------------------------------------------------


def content_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _sanitise_filename(name: str) -> str:
    """Remove characters that are invalid in filenames."""
    return "".join(c for c in name if c not in r'\/:*?"<>|').strip()


def _sanitise_dirname(name: str) -> str:
    """Sanitise a string for use as a directory name.

    Removes filesystem-unsafe characters, collapses whitespace, and caps
    length to avoid OS path limits.
    """
    cleaned = "".join(c for c in name if c not in r'\/:*?"<>|').strip()
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        cleaned = "Unknown"
    return cleaned[:120]


def _parse_album_series(album: str) -> dict[str, Any]:
    """Extract title, series name, and sequence number from an ID3 album tag.

    Publisher album tags follow the pattern ``Book Title: Series Name N``
    (sometimes with multiple colons).  The *last* colon-separated segment
    that matches ``Series Name N`` or ``Series Name -1`` is treated as the
    series suffix.

    Returns a dict with ``title``, and optionally ``series`` and
    ``sequence_number``.

    Examples::

        >>> _parse_album_series("Hilda and the Ghost Ship: Hilda 5")
        {'title': 'Hilda and the Ghost Ship', 'series': 'Hilda', 'sequence_number': 5}
        >>> _parse_album_series("Cross Bones")
        {'title': 'Cross Bones'}
        >>> _parse_album_series("The Collection: Funnybones -1")
        {'title': 'The Collection', 'series': 'Funnybones'}
        >>> _parse_album_series("Minecraft: The Island: Minecraft 1")
        {'title': 'Minecraft: The Island', 'series': 'Minecraft', 'sequence_number': 1}
    """
    # Try to match "Series Name <number>" at the end of the last colon segment
    parts = album.split(":")
    if len(parts) >= 2:
        last = parts[-1].strip()
        # Match: "Series Name 5" or "Series Name -1"
        m = re.match(r"^(.+?)\s+(-?\d+)$", last)
        if m:
            series_name = m.group(1).strip()
            seq = int(m.group(2))
            title = ":".join(parts[:-1]).strip()
            result: dict[str, Any] = {"title": title, "series": series_name}
            if seq > 0:
                result["sequence_number"] = seq
            return result

    return {"title": album}


def _cleanup_empty_parents(path: Path, stop_at: Path) -> None:
    """Remove empty parent directories up to (not including) *stop_at*."""
    parent = path.parent
    while parent != stop_at and parent.exists():
        try:
            next(parent.iterdir())
            break
        except StopIteration:
            parent.rmdir()
            parent = parent.parent


def _remap_chapter_filename(old_name: str) -> str:
    """Remap old-style ``00-Chapter 1.m4a`` to ``01 - Chapter 1.m4a``.

    Converts the 0-based chapter key prefix to a 1-based sequential number.
    """
    m = re.match(r"^(\d+)-(.+)$", old_name)
    if m:
        old_idx = int(m.group(1))
        rest = m.group(2)
        return f"{old_idx + 1:02d} - {rest}"
    return old_name


def _ext_from_url(url: str, *, default: str = "") -> str:
    """Extract file extension from a URL path, ignoring query params."""
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    return suffix if suffix else default


def _format_to_ext(fmt: str) -> str:
    """Map a Yoto track ``format`` field to a file extension."""
    mapping = {
        "aac": ".m4a",
        "mp3": ".mp3",
        "ogg": ".ogg",
        "wav": ".wav",
        "flac": ".flac",
        "opus": ".opus",
    }
    return mapping.get(fmt.lower(), "")


def _guess_format(ext: str) -> str:
    """Guess the Yoto track format from a file extension."""
    mapping = {
        ".mp3": "mp3",
        ".m4a": "aac",
        ".ogg": "ogg",
        ".opus": "opus",
        ".wav": "wav",
        ".flac": "flac",
    }
    return mapping.get(ext.lower(), "mp3")


# Thin alias – kept for backward compatibility with callers inside this module.
_normalise_for_dedup = normalize_title_for_dedup


def _extract_id3_metadata(zf: "zipfile.ZipFile", entry: str) -> dict[str, Any]:
    """Read ID3 tags from an MP3 inside a zip file.

    Returns a dict with optional keys: ``album``, ``author``, ``track_title``,
    ``track_number``, ``track_total``, ``year``, ``genre``, ``cover_data``,
    ``cover_mime``.  Non-MP3 files return an empty dict.
    """
    if not entry.lower().endswith(".mp3"):
        return {}

    try:
        from mutagen.mp3 import MP3
    except ImportError:
        logger.debug("mutagen not installed — skipping ID3 extraction")
        return {}

    result: dict[str, Any] = {}
    try:
        data = zf.read(entry)
        audio = MP3(io.BytesIO(data))
        tags = audio.tags
        if tags is None:
            return result

        # Album → book title
        if "TALB" in tags:
            result["album"] = str(tags["TALB"].text[0]).strip()

        # Artist (TPE1) or Album Artist (TPE2) → author
        for key in ("TPE1", "TPE2"):
            if key in tags:
                val = str(tags[key].text[0]).strip()
                if val:
                    result["author"] = val
                    break

        # Track title
        if "TIT2" in tags:
            result["track_title"] = str(tags["TIT2"].text[0]).strip()

        # Track number / total
        if "TRCK" in tags:
            raw = str(tags["TRCK"].text[0])
            if "/" in raw:
                num, total = raw.split("/", 1)
                result["track_number"] = int(num)
                result["track_total"] = int(total)
            else:
                result["track_number"] = int(raw)

        # Year
        if "TDRC" in tags:
            result["year"] = str(tags["TDRC"].text[0]).strip()

        # Genre (e.g. "Audiobooks")
        if "TCON" in tags:
            result["genre"] = str(tags["TCON"].text[0]).strip()

        # Embedded cover art (APIC)
        apic_frames = tags.getall("APIC")
        if apic_frames:
            result["cover_data"] = apic_frames[0].data
            result["cover_mime"] = apic_frames[0].mime

    except Exception as exc:
        logger.debug("ID3 read failed for %s: %s", entry, exc)

    return result


_AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".ogg", ".aac", ".wav", ".flac"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _is_audio_file(name: str) -> bool:
    return (
        Path(name).suffix.lower() in _AUDIO_EXTS
        and not name.startswith("__MACOSX")
    )


def _is_image_file(name: str) -> bool:
    return (
        Path(name).suffix.lower() in _IMAGE_EXTS
        and not name.startswith("__MACOSX")
    )


def _is_icon_file(name: str) -> bool:
    return _is_image_file(name) and "icon" in name.lower()


def _title_from_zip_name(filename: str) -> str:
    """Derive a human-readable title from a zip filename.

    Handles patterns like:
    - ``BunnyVsMonkey_1_51.zip`` → ``Bunny Vs Monkey``
    - ``143storeyTreehouse_1_13.zip`` → ``143 Storey Treehouse``
    - ``Cross Bones (1 of 1).zip`` → ``Cross Bones``
    """
    stem = Path(filename).stem

    # Strip trailing _N_N patterns (chapter ranges)
    stem = re.sub(r"_\d+_\d+$", "", stem)

    # Strip trailing "(N of N)" patterns
    stem = re.sub(r"\s*\(\d+\s+of\s+\d+\)$", "", stem)

    # Split CamelCase into words
    stem = re.sub(r"([a-z])([A-Z])", r"\1 \2", stem)
    stem = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", stem)

    # Replace underscores with spaces
    stem = stem.replace("_", " ")

    return " ".join(stem.split()).strip()


def _chapter_title_from_filename(stem: str, index: int) -> str:
    """Extract a chapter title from an audio file stem.

    Handles patterns like:
    - ``0001_BunnyVsMonkey`` → ``Chapter 1``
    - ``Cross Bones - Track 001`` → ``Track 1``
    - ``00-Toads`` → ``Toads``
    """
    # Pattern: NNNN_Name or PREFIX_NNNN Name
    m = re.match(r"^(?:[A-Z]+_\d+\s+)?(\d+)[-_](.+)$", stem)
    if m:
        name = m.group(2).strip()
        if name:
            return _sanitise_filename(name)

    # Pattern: Name - Track NNN
    m = re.match(r"^(.+?)\s*-\s*Track\s+(\d+)$", stem, re.IGNORECASE)
    if m:
        return f"Track {int(m.group(2))}"

    return f"Chapter {index + 1}"


async def _download_file(
    client: httpx.AsyncClient, url: str, dest: Path, *, max_attempts: int = 3
) -> bool:
    """Stream-download *url* to *dest* with retry for transient errors."""
    for attempt in range(1, max_attempts + 1):
        try:
            async with client.stream("GET", url) as resp:
                if resp.status_code in {400, 403, 404, 410}:
                    logger.warning("Permanent error %d for %s – skipping", resp.status_code, dest.name)
                    return False
                resp.raise_for_status()
                with dest.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                        fh.write(chunk)
            return True
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            if dest.exists():
                dest.unlink()
            if attempt < max_attempts:
                logger.debug("Download attempt %d/%d failed for %s: %s", attempt, max_attempts, dest.name, exc)
                continue
            logger.warning("Download failed after %d attempts: %s – %s", max_attempts, dest.name, exc)
            return False
    return False


