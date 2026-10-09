"""Audiobookshelf client helpers used by the microservices."""

from __future__ import annotations

import asyncio
import mimetypes
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx

from yoto_bulk.card_utils import MAX_DESCRIPTION_LENGTH, MAX_TITLE_LENGTH, sanitize_description


def _split_people(value: Any) -> list[str]:
    if isinstance(value, list):
        result: list[str] = []
        for entry in value:
            if isinstance(entry, dict):
                name = str(entry.get("name") or "").strip()
            else:
                name = str(entry).strip()
            if name:
                result.append(name)
        return result
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _normalize_sequence(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value or "").strip()
    if text.isdigit():
        return int(text)
    return None


def _clean_track_title(title: str) -> str:
    cleaned = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", title).strip()
    return cleaned or title


def _normalize_match_text(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"\(unabridged\)", "", value)
    value = re.sub(r"[^a-z0-9]+", "", value)
    return value


def _abs_sanitize_filename(filename: str) -> str:
    """Replicate ABS server/utils/fileUtils.js sanitizeFilename().

    This must stay in sync with the ABS implementation so that we can predict
    the on-disk folder path ABS will create for an uploaded book.
    """
    if not filename:
        return filename
    # Replace first colon with " - ", remaining illegal chars with ""
    filename = filename.replace(":", " - ", 1)
    filename = re.sub(r'[/\\?<>*|"]', "", filename)
    # Remove colons that remain after the first replacement
    filename = filename.replace(":", "")
    # Control characters
    filename = re.sub(r"[\x00-\x1f\x80-\x9f]", "", filename)
    # Line breaks
    filename = re.sub(r"[\n\r]", "", filename)
    # Trailing dots/spaces
    filename = re.sub(r"[. ]+$", "", filename)
    # Collapse whitespace
    filename = re.sub(r"\s+", " ", filename)
    return filename


def _title_variants(title: str) -> list[str]:
    variants = {
        title.strip(),
        title.replace(":", " ").strip(),
        title.replace("!", "").strip(),
        title.replace(":", " ").replace("!", "").strip(),
    }
    return [variant for variant in variants if variant]


def _titles_loosely_match(expected: str, candidate: str) -> bool:
    if not expected or not candidate:
        return False
    if expected == candidate:
        return True
    shorter = min(len(expected), len(candidate))
    longer = max(len(expected), len(candidate))
    if shorter == 0:
        return False
    if shorter / longer < 0.6:
        return False
    return expected in candidate or candidate in expected


# Metadata fields we let a high-confidence provider match fill on import.
# Deliberately excludes title/author/series/cover, which we curate from the
# source (BorrowBox catalog / Yoto card) and patch separately.
_DEFAULT_MATCH_FIELDS: tuple[str, ...] = (
    "description",
    "narrators",
    "publishedYear",
    "publisher",
    "genres",
    "language",
    "subtitle",
)


def _match_series_to_metadata(series: Any) -> list[dict[str, Any]]:
    """Map an ABS search-result ``series`` list to ABS media-metadata shape.

    Search results use ``{"series": name, "sequence": n}`` while the media
    PATCH endpoint expects ``{"name": name, "sequence": n}``.
    """
    if not isinstance(series, list):
        return []
    result: list[dict[str, Any]] = []
    for entry in series:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("series") or entry.get("name") or "").strip()
        if not name:
            continue
        mapped: dict[str, Any] = {"name": name}
        sequence = entry.get("sequence")
        if sequence not in (None, ""):
            mapped["sequence"] = str(sequence)
        result.append(mapped)
    return result


def _match_result_to_metadata(book: dict[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    """Build an ABS media-metadata PATCH body from a provider search result.

    Only non-empty values for the requested *fields* are included so we never
    wipe existing metadata by patching blanks.
    """
    requested = set(fields)
    metadata: dict[str, Any] = {}

    if "description" in requested:
        description = str(book.get("description") or "").strip()
        if description:
            metadata["description"] = description

    if "narrators" in requested:
        narrators = _split_people(book.get("narrator"))
        if narrators:
            metadata["narrators"] = narrators

    if "genres" in requested:
        raw_genres = book.get("genres")
        if isinstance(raw_genres, str):
            raw_genres = raw_genres.split(",")
        if isinstance(raw_genres, list):
            genres = [str(g).strip() for g in raw_genres if str(g).strip()]
            if genres:
                metadata["genres"] = genres

    if "series" in requested:
        series_payload = _match_series_to_metadata(book.get("series"))
        if series_payload:
            metadata["series"] = series_payload

    for key in ("publishedYear", "publisher", "language", "subtitle"):
        if key in requested:
            value = str(book.get(key) or "").strip()
            if value:
                metadata[key] = value

    return metadata


def build_yoto_payload_from_abs(item: dict[str, Any], playback: dict[str, Any]) -> dict[str, Any]:
    """Map an ABS library item + playback session to a Yoto-compatible payload."""
    media = item.get("media") or {}
    metadata = media.get("metadata") or {}
    audio_tracks = playback.get("audioTracks") or []
    chapters = playback.get("chapters") or media.get("chapters") or []

    title = str(metadata.get("title") or item.get("media", {}).get("title") or item.get("title") or "Untitled")
    authors = _split_people(metadata.get("authors"))
    author = ", ".join(authors) if authors else ""

    series_entries = metadata.get("series") or []
    series_name = None
    sequence_number = None
    if series_entries:
        first = series_entries[0] or {}
        series_name = str(first.get("name") or "").strip() or None
        sequence_number = _normalize_sequence(first.get("sequence"))

    if not chapters:
        chapters = [
            {
                "id": idx,
                "start": track.get("startOffset", 0),
                "end": (track.get("startOffset", 0) or 0) + (track.get("duration", 0) or 0),
                "title": _clean_track_title(str(track.get("title") or f"Track {idx + 1}")),
            }
            for idx, track in enumerate(audio_tracks)
        ]

    yoto_chapters = []
    for idx, track in enumerate(audio_tracks):
        chapter = chapters[idx] if idx < len(chapters) else {}
        title_text = str(chapter.get("title") or track.get("title") or f"Track {idx + 1}")
        yoto_chapters.append(
            {
                "title": _clean_track_title(title_text),
                "key": str(chapter.get("id", idx)),
                "duration": track.get("duration") or chapter.get("duration"),
                "tracks": [
                    {
                        "key": "01",
                        "title": _clean_track_title(title_text),
                        "type": "audio",
                        "format": "mp3",
                        "trackUrl": "",
                    }
                ],
            }
        )

    payload: dict[str, Any] = {
        "cardId": f"abs-{item['id']}",
        "title": title[:MAX_TITLE_LENGTH],
        "metadata": {},
        "content": {
            "activity": "yoto_Player",
            "chapters": yoto_chapters,
            "config": {"resumeTimeout": 2592000, "onlineOnly": False},
            "playbackType": "linear",
            "version": "1",
        },
    }

    if author:
        payload["metadata"]["author"] = author
    if series_name:
        payload["metadata"]["series"] = series_name
    if sequence_number is not None:
        payload["metadata"]["sequence_number"] = sequence_number

    description = sanitize_description(str(metadata.get("description") or ""))
    if description:
        payload["metadata"]["description"] = description[:MAX_DESCRIPTION_LENGTH]

    return payload


def build_yoto_payload_from_abs_podcast(item: dict[str, Any]) -> dict[str, Any]:
    """Map an expanded ABS podcast item to one Yoto chapter per downloaded episode."""
    media = item.get("media") or {}
    metadata = media.get("metadata") or {}
    title = str(metadata.get("title") or item.get("title") or "Untitled")

    yoto_chapters = []
    for episode in media.get("episodes") or []:
        episode_id = str(episode.get("id") or "").strip()
        audio_track = episode.get("audioTrack") or {}
        if not episode_id or not audio_track.get("contentUrl"):
            continue
        episode_title = str(episode.get("title") or "Untitled episode")
        yoto_chapters.append(
            {
                "title": episode_title,
                "key": episode_id,
                "duration": episode.get("duration") or audio_track.get("duration"),
                "tracks": [
                    {
                        "key": "01",
                        "title": episode_title,
                        "type": "audio",
                        "format": "mp3",
                        "trackUrl": "",
                    }
                ],
            }
        )

    payload: dict[str, Any] = {
        "cardId": f"abs-{item['id']}",
        "title": title[:MAX_TITLE_LENGTH],
        "metadata": {"category": "podcast"},
        "content": {
            "activity": "yoto_Player",
            "chapters": yoto_chapters,
            "config": {"resumeTimeout": 2592000, "onlineOnly": False},
            "playbackType": "linear",
            "version": "1",
        },
    }

    author = str(metadata.get("author") or "").strip()
    if author:
        payload["metadata"]["author"] = author
    description = sanitize_description(str(metadata.get("description") or ""))
    if description:
        payload["metadata"]["description"] = description[:MAX_DESCRIPTION_LENGTH]

    return payload


class ABSClient:
    """Minimal Audiobookshelf API client."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 120.0,
        cf_access_client_id: str | None = None,
        cf_access_client_secret: str | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("ABS base URL is not configured")
        if not token:
            raise ValueError("ABS token is not configured")
        if bool(cf_access_client_id) != bool(cf_access_client_secret):
            raise ValueError("Cloudflare Access client ID and secret must be configured together")
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"}
        if cf_access_client_id and cf_access_client_secret:
            headers["CF-Access-Client-Id"] = cf_access_client_id
            headers["CF-Access-Client-Secret"] = cf_access_client_secret
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers=headers,
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        response = await self._client.request(method, path, **kwargs)
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()

    async def get_libraries(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/libraries")
        return data.get("libraries", [])

    async def get_book_library(self, preferred_name: str | None = None) -> dict[str, Any]:
        libraries = await self.get_libraries()
        if preferred_name:
            for library in libraries:
                if library.get("name") == preferred_name and library.get("mediaType") == "book":
                    return library
        for library in libraries:
            if library.get("mediaType") == "book":
                return library
        raise ValueError("No book library found in Audiobookshelf")

    async def get_podcast_libraries(self) -> list[dict[str, Any]]:
        libraries = await self.get_libraries()
        return [library for library in libraries if library.get("mediaType") == "podcast"]

    async def list_library_items(self, library_id: str) -> list[dict[str, Any]]:
        data = await self._request(
            "GET",
            f"/api/libraries/{library_id}/items",
            params={"limit": 0, "minified": 0, "sort": "media.metadata.title"},
        )
        return data.get("results", [])

    async def get_item(self, item_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/items/{item_id}", params={"expanded": 1})

    async def list_library_series(self, library_id: str) -> list[dict[str, Any]]:
        """Return the canonical series ABS knows about for a library.

        Each entry typically includes ``id``, ``name``, and ``books`` (a list of
        item dicts that belong to the series). ABS is the source of truth for
        ABS-backed cards, so we prefer this over LLM-based grouping.
        """
        data = await self._request(
            "GET",
            f"/api/libraries/{library_id}/series",
            params={"limit": 0, "minified": 0, "include": "rssfeed,progress"},
        )
        if isinstance(data, dict):
            return data.get("results", []) or []
        return []

    async def search_library_items(self, library_id: str, query: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/api/libraries/{library_id}/search", params={"q": query})
        return data.get("book", [])

    async def search_metadata_books(
        self,
        *,
        title: str,
        author: str | None = None,
        provider: str = "audible.uk",
        item_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search ABS metadata providers for books without mutating ABS items."""
        params: dict[str, str] = {"provider": provider, "title": title}
        if author:
            params["author"] = author
        if item_id:
            params["id"] = item_id
        data = await self._request("GET", "/api/search/books", params=params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            results = data.get("results") or data.get("books") or []
            return results if isinstance(results, list) else []
        return []

    async def scan_library(self, library_id: str) -> None:
        response = await self._client.post(f"/api/libraries/{library_id}/scan", params={"force": 1}, timeout=120)
        response.raise_for_status()

    @staticmethod
    def _playback_request_payload() -> dict[str, Any]:
        return {
            "deviceInfo": {
                "deviceId": "yoto-server",
                "clientName": "yoto-server",
                "clientVersion": "0.1.0",
                "manufacturer": "openai",
                "model": "codex",
            },
            "forceDirectPlay": True,
            "supportedMimeTypes": ["audio/mpeg", "audio/mp4", "audio/aac", "audio/flac", "audio/ogg", "audio/wav"],
            "mediaPlayer": "unknown",
        }

    async def get_playback_session(self, item_id: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/api/items/{item_id}/play", json=self._playback_request_payload()
        )

    async def get_episode_playback_session(
        self, item_id: str, episode_id: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/items/{item_id}/play/{episode_id}",
            json=self._playback_request_payload(),
        )

    async def fetch_cover(self, item_id: str) -> tuple[bytes, str]:
        response = await self._client.get(f"/api/items/{item_id}/cover", params={"raw": 1})
        response.raise_for_status()
        return response.content, response.headers.get("content-type", "image/jpeg")

    async def download_audio_track(self, content_url: str) -> tuple[bytes, str]:
        response = await self._client.get(content_url)
        response.raise_for_status()
        return response.content, response.headers.get("content-type", "audio/mpeg")

    @asynccontextmanager
    async def download_audio_track_stream(self, content_url: str) -> AsyncIterator[tuple[AsyncIterator[bytes], str, int | None]]:
        """Download an audio track from ABS as a stream.

        Returns an async context manager that yields (stream, content_type, content_length).
        """
        async with self._client.stream("GET", content_url) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "audio/mpeg")
            content_length = response.headers.get("content-length")
            cl = int(content_length) if content_length and content_length.isdigit() else None
            yield response.aiter_bytes(), content_type, cl

    async def upload_local_book(
        self,
        *,
        library_id: str,
        folder_id: str,
        title: str,
        author: str,
        audio_files: list[Path],
        cover_file: Path | None,
        series: str | None,
        sequence: int | None,
        description: str | None = None,
        auto_match: bool = True,
        match_min_confidence: float = 0.7,
        match_providers: Sequence[str] = ("audible.uk", "audible"),
    ) -> dict[str, Any]:
        """Upload a book to ABS, streaming files to keep memory usage low."""
        upload_files = []
        file_handles = []
        try:
            for idx, audio_file in enumerate(audio_files):
                content_type = mimetypes.guess_type(audio_file.name)[0] or "application/octet-stream"
                fh = audio_file.open("rb")
                file_handles.append(fh)
                upload_files.append((str(idx), (audio_file.name, fh, content_type)))

            if cover_file:
                content_type = mimetypes.guess_type(cover_file.name)[0] or "image/jpeg"
                fh = cover_file.open("rb")
                file_handles.append(fh)
                upload_files.append((str(len(upload_files)), (cover_file.name, fh, content_type)))

            response = await self._client.post(
                "/api/upload",
                data={
                    "title": title,
                    "author": author,
                    # ABS uses `series` from the form to build the folder layout
                    # author/series/title. Omit when empty so we don't end up
                    # with an extra empty segment.
                    **({"series": series} if series else {}),
                    "library": library_id,
                    "folder": folder_id,
                },
                files=upload_files,
                timeout=600,
            )
            response.raise_for_status()
        finally:
            for fh in file_handles:
                fh.close()

        # ABS builds the on-disk folder as folder.path / author / series / title
        # (empty parts filtered, each part sanitized).  Compute the expected
        # relative directory suffix so we can locate the item by path.
        expected_rel_suffix = "/".join(
            _abs_sanitize_filename(p) for p in [author, series, title] if p
        )

        item_id = await self._resolve_uploaded_item_by_path(
            library_id=library_id,
            expected_rel_suffix=expected_rel_suffix,
        )
        if not item_id:
            # Fallback: ABS sometimes ignores the series form field (older
            # versions, mis-permissions on the folder, etc.) and falls back to
            # author/title only. Try that suffix before giving up so the import
            # doesn't 500 just because of a folder-layout mismatch.
            if series:
                fallback_suffix = "/".join(
                    _abs_sanitize_filename(p) for p in [author, title] if p
                )
                item_id = await self._resolve_uploaded_item_by_path(
                    library_id=library_id,
                    expected_rel_suffix=fallback_suffix,
                )
        if not item_id:
            raise ValueError("Audiobookshelf upload succeeded but the uploaded item could not be resolved")

        # Enrich metadata before applying our curated fields. This runs first so
        # the curated PATCH below stays authoritative for title/author/series
        # (ABS merges metadata keys, so enrichment fields like description are
        # preserved).
        #
        # 1. If we didn't upload a cover, let a general provider fill only the
        #    missing cover/details (non-destructive).
        if cover_file is None:
            try:
                await self.quick_match_item(
                    item_id,
                    provider="google",
                    override_defaults=False,
                    title=title,
                    author=author,
                )
            except Exception:
                # Matching is enrichment only; upload success should not depend on it.
                pass

        # 2. Uploaded audio often carries only a generic ID3 blurb (narrator +
        #    copyright notice). Ask ABS to match against an online provider and,
        #    when it reports high confidence, adopt the richer metadata
        #    (description, narrators, year, genres, ...). This mirrors the ABS
        #    UI "Match" flow but gated on confidence so weak matches are ignored.
        if auto_match:
            try:
                await self.auto_match_item(
                    item_id,
                    title=title,
                    author=author or None,
                    min_confidence=match_min_confidence,
                    providers=tuple(match_providers),
                )
            except Exception:
                # Matching is enrichment only; upload success should not depend on it.
                pass

        metadata: dict[str, Any] = {}
        metadata["title"] = title[:MAX_TITLE_LENGTH]
        if author:
            metadata["author"] = author
            metadata["authors"] = [a.strip() for a in author.split(",") if a.strip()]
        if description:
            metadata["description"] = sanitize_description(description)[:MAX_DESCRIPTION_LENGTH]
        if series:
            metadata["series"] = [{"name": series, "sequence": str(sequence) if sequence is not None else None}]
        elif sequence is not None:
            metadata["series"] = []

        import logging
        logging.getLogger(__name__).info("Patching ABS item %s with metadata: %r", item_id, metadata)

        await self._request("PATCH", f"/api/items/{item_id}/media", json={"metadata": metadata})

        return await self.get_item(item_id)

    async def _resolve_uploaded_item_by_path(
        self, *, library_id: str, expected_rel_suffix: str,
    ) -> str | None:
        """Find the library item whose path ends with *expected_rel_suffix*.

        ABS creates the item asynchronously via its scanner, so we trigger a
        forced scan then poll the library items list looking for a path match.
        """
        import logging
        logger = logging.getLogger(__name__)

        def _find_in_items(items: list[dict[str, Any]]) -> str | None:
            for item in items:
                item_path: str = item.get("path") or item.get("relPath") or ""
                # Normalise to forward-slash for comparison
                item_path = item_path.replace("\\", "/")
                if item_path.endswith(expected_rel_suffix):
                    return item.get("id")
            return None

        # First check — the upload may have auto-scanned already.
        items = await self.list_library_items(library_id)
        item_id = _find_in_items(items)
        if item_id:
            return item_id

        # Trigger a forced scan and retry.
        await self.scan_library(library_id)
        for attempt in range(6):
            await asyncio.sleep(3)
            items = await self.list_library_items(library_id)
            item_id = _find_in_items(items)
            if item_id:
                return item_id
            logger.debug(
                "Path resolve attempt %d: no match for suffix %r among %d items",
                attempt + 1, expected_rel_suffix, len(items),
            )
        return None

    async def update_item_metadata(
        self,
        item_id: str,
        *,
        title: str | None = None,
        author: str | None = None,
        description: str | None = None,
        series: str | None = None,
        sequence: int | None = None,
    ) -> dict[str, Any]:
        """Patch ABS media metadata for an existing item and return the refreshed item."""
        metadata: dict[str, Any] = {}
        if title is not None:
            metadata["title"] = title[:MAX_TITLE_LENGTH]
        if author is not None:
            metadata["author"] = author
            metadata["authors"] = _split_people(author)
        if description is not None:
            metadata["description"] = sanitize_description(description)[:MAX_DESCRIPTION_LENGTH] if description else ""
        if series is not None:
            if series == "":
                metadata["series"] = []
            else:
                entry: dict[str, Any] = {"name": series}
                if sequence is not None:
                    entry["sequence"] = str(sequence)
                metadata["series"] = [entry]
        elif sequence is not None:
            # Sequence without a series name is not useful to ABS; leave series unchanged.
            pass

        if metadata:
            await self._request("PATCH", f"/api/items/{item_id}/media", json={"metadata": metadata})
        return await self.get_item(item_id)

    async def quick_match_item(
        self,
        item_id: str,
        *,
        provider: str = "google",
        override_defaults: bool = False,
        title: str | None = None,
        author: str | None = None,
    ) -> dict[str, Any]:
        """Run ABS quick match for a single item.

        By default this only fills missing details and cover art.
        """
        payload: dict[str, Any] = {
            "provider": provider,
            "overrideDefaults": override_defaults,
        }
        if title:
            payload["title"] = title
        if author:
            payload["author"] = author
        return await self._request("POST", f"/api/items/{item_id}/match", json=payload)

    async def auto_match_item(
        self,
        item_id: str,
        *,
        title: str,
        author: str | None = None,
        min_confidence: float = 0.7,
        providers: Sequence[str] = ("audible.uk", "audible"),
        apply_fields: Sequence[str] = _DEFAULT_MATCH_FIELDS,
    ) -> dict[str, Any]:
        """Match an item against online providers and adopt a confident result.

        Searches each provider in *providers* (passing ``item_id`` so ABS can
        compare audio duration) and, when the best candidate's
        ``matchConfidence`` meets *min_confidence*, PATCHes the item with the
        matched metadata (``apply_fields`` only, non-empty values).

        ABS only computes ``matchConfidence`` for audible providers, so the
        defaults are audible regions. Matching is enrichment; callers may ignore
        the returned summary. Returns::

            {"applied", "confidence", "provider", "matched_title", "fields"}
        """
        best_book: dict[str, Any] | None = None
        best_confidence: float = -1.0
        best_provider: str | None = None

        for provider in providers:
            try:
                candidates = await self.search_metadata_books(
                    title=title,
                    author=author,
                    provider=provider,
                    item_id=item_id,
                )
            except Exception:
                continue

            for book in candidates:
                if not isinstance(book, dict) or not book.get("title"):
                    continue
                confidence = book.get("matchConfidence")
                if not isinstance(confidence, (int, float)):
                    continue
                if float(confidence) > best_confidence:
                    best_confidence = float(confidence)
                    best_book = book
                    best_provider = provider

            # Stop as soon as one provider yields a confident enough match.
            if best_book is not None and best_confidence >= min_confidence:
                break

        if best_book is None or best_confidence < min_confidence:
            return {
                "applied": False,
                "confidence": best_confidence if best_book is not None else None,
                "provider": best_provider,
                "matched_title": best_book.get("title") if best_book else None,
                "fields": [],
            }

        metadata = _match_result_to_metadata(best_book, apply_fields)
        if metadata:
            await self._request("PATCH", f"/api/items/{item_id}/media", json={"metadata": metadata})

        import logging

        logging.getLogger(__name__).info(
            "Auto-matched ABS item %s via %s (confidence %.2f); applied fields: %s",
            item_id,
            best_provider,
            best_confidence,
            ", ".join(sorted(metadata.keys())) or "none",
        )

        return {
            "applied": bool(metadata),
            "confidence": best_confidence,
            "provider": best_provider,
            "matched_title": best_book.get("title"),
            "fields": sorted(metadata.keys()),
        }
