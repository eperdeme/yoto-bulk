from __future__ import annotations

from yoto_bulk.audiobookshelf import (
    build_yoto_payload_from_abs,
    build_yoto_payload_from_abs_podcast,
)
from yoto_bulk.card_utils import preserve_yoto_media
from yoto_server.routes import _ensure_track_shape


def test_build_yoto_payload_from_abs_includes_required_track_fields() -> None:
    item = {
        "id": "abs-1",
        "media": {"metadata": {"title": "Test Title", "authorName": "Test Author"}},
    }
    playback = {
        "audioTracks": [
            {"title": "Track One", "duration": 12, "startOffset": 0},
            {"title": "Track Two", "duration": 34, "startOffset": 12},
        ],
        "chapters": [
            {"id": "intro", "title": "Intro"},
            {"id": "main", "title": "Main"},
        ],
    }

    payload = build_yoto_payload_from_abs(item, playback)
    chapters = payload["content"]["chapters"]

    assert chapters[0]["tracks"][0]["key"] == "01"
    assert chapters[0]["tracks"][0]["type"] == "audio"
    assert chapters[0]["tracks"][0]["format"] == "mp3"
    assert chapters[0]["tracks"][0]["trackUrl"] == ""
    assert chapters[1]["tracks"][0]["key"] == "01"
    assert chapters[1]["tracks"][0]["type"] == "audio"


def test_build_yoto_payload_from_abs_podcast_maps_downloaded_episodes_to_chapters() -> None:
    item = {
        "id": "podcast-1",
        "media": {
            "metadata": {
                "title": "The Test Show",
                "author": "Test Author",
                "description": "A useful show.",
            },
            "episodes": [
                {
                    "id": "episode-2",
                    "title": "Second episode",
                    "duration": 34,
                    "audioTrack": {"contentUrl": "/api/items/podcast-1/file/2"},
                },
                {
                    "id": "episode-1",
                    "title": "First episode",
                    "duration": 12,
                    "audioTrack": {"contentUrl": "/api/items/podcast-1/file/1"},
                },
                {"id": "not-downloaded", "title": "Remote only"},
            ],
        },
    }

    payload = build_yoto_payload_from_abs_podcast(item)

    assert payload["title"] == "The Test Show"
    assert payload["metadata"] == {
        "author": "Test Author",
        "category": "podcast",
        "description": "A useful show.",
    }
    chapters = payload["content"]["chapters"]
    assert [chapter["key"] for chapter in chapters] == ["episode-2", "episode-1"]
    assert [chapter["title"] for chapter in chapters] == ["Second episode", "First episode"]
    assert chapters[0]["tracks"][0] == {
        "key": "01",
        "title": "Second episode",
        "type": "audio",
        "format": "mp3",
        "trackUrl": "",
    }


def test_ensure_track_shape_backfills_missing_track_key_and_type() -> None:
    payload = {
        "content": {
            "chapters": [
                {"tracks": [{"title": "One", "trackUrl": ""}]},
                {"tracks": [{"title": "Two", "trackUrl": "", "type": "stream"}]},
            ]
        }
    }

    _ensure_track_shape(payload)

    assert payload["content"]["chapters"][0]["tracks"][0]["key"] == "01"
    assert payload["content"]["chapters"][0]["tracks"][0]["type"] == "audio"
    assert payload["content"]["chapters"][1]["tracks"][0]["key"] == "01"
    assert payload["content"]["chapters"][1]["tracks"][0]["type"] == "stream"


def test_preserve_yoto_media_reuses_uploaded_refs_when_shape_matches() -> None:
    existing = {
        "metadata": {"cover": {"imageL": "https://card-content.yotoplay.com/cover-id"}},
        "content": {
            "chapters": [
                {
                    "display": {"icon16x16": "yoto:#icon-1"},
                    "tracks": [
                        {
                            "trackUrl": "yoto:#audio-1",
                            "duration": 12,
                            "fileSize": 34,
                            "channels": "stereo",
                            "format": "mp3",
                            "display": {"icon16x16": "yoto:#icon-1"},
                        }
                    ],
                }
            ]
        },
    }
    payload = {
        "metadata": {"description": "New description"},
        "content": {"chapters": [{"tracks": [{"trackUrl": "", "format": "mp3"}]}]},
    }

    assert preserve_yoto_media(existing, payload) is True

    assert payload["metadata"]["description"] == "New description"
    assert payload["metadata"]["cover"]["imageL"] == "https://card-content.yotoplay.com/cover-id"
    track = payload["content"]["chapters"][0]["tracks"][0]
    assert track["trackUrl"] == "yoto:#audio-1"
    assert track["fileSize"] == 34
    assert track["display"]["icon16x16"] == "yoto:#icon-1"


def test_preserve_yoto_media_matches_podcast_episodes_by_chapter_key() -> None:
    existing = {
        "content": {
            "chapters": [
                {
                    "key": "episode-1",
                    "tracks": [{"trackUrl": "yoto:#episode-1", "format": "mp3"}],
                }
            ]
        }
    }
    payload = {
        "content": {
            "chapters": [
                {
                    "key": "episode-2",
                    "tracks": [{"trackUrl": "", "format": "mp3"}],
                },
                {
                    "key": "episode-1",
                    "tracks": [{"trackUrl": "", "format": "mp3"}],
                },
            ]
        }
    }

    assert preserve_yoto_media(existing, payload) is True
    assert payload["content"]["chapters"][0]["tracks"][0]["trackUrl"] == ""
    assert payload["content"]["chapters"][1]["tracks"][0]["trackUrl"] == "yoto:#episode-1"
