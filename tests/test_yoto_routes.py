from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from yoto_server.routes import (
    _card_summary,
    _save_partial_abs_push,
    _upload_audio_from_abs,
)


def test_card_summary_marks_abs_cards_without_yoto_card_id_as_needing_push() -> None:
    request = SimpleNamespace(url_for=lambda name, card_id: f"http://test/{card_id}/cover")
    payload = {"metadata": {"title": "Harry Potter"}, "content": {"chapters": []}}
    meta = {"source": "abs", "external_id": "abs-1"}

    summary = _card_summary(request, "abs-1", payload, meta)

    assert summary.needs_push is True


def test_card_summary_marks_abs_cards_with_yoto_card_id_as_synced() -> None:
    request = SimpleNamespace(url_for=lambda name, card_id: f"http://test/{card_id}/cover")
    payload = {"metadata": {"title": "Harry Potter"}, "content": {"chapters": []}}
    meta = {"source": "abs", "external_id": "abs-1", "yoto_card_id": "real-yoto-id"}

    summary = _card_summary(request, "abs-1", payload, meta)

    assert summary.needs_push is False


def test_card_summary_marks_abs_cards_changed_since_push_as_needing_push() -> None:
    request = SimpleNamespace(url_for=lambda name, card_id: f"http://test/{card_id}/cover")
    payload = {"title": "The Test Show", "metadata": {}, "content": {"chapters": []}}
    meta = {
        "source": "abs",
        "external_id": "podcast-1",
        "yoto_card_id": "real-yoto-id",
        "push_hash": "previous-content",
    }

    summary = _card_summary(request, "abs-podcast-1", payload, meta)

    assert summary.needs_push is True


def test_card_summary_identifies_podcast_media_type() -> None:
    request = SimpleNamespace(url_for=lambda name, card_id: f"http://test/{card_id}/cover")
    payload = {
        "title": "The Test Show",
        "metadata": {"category": "podcast"},
        "content": {"chapters": []},
    }

    summary = _card_summary(
        request,
        "abs-podcast-1",
        payload,
        {"source": "abs", "external_id": "podcast-1"},
    )

    assert summary.media_type == "podcast"


def test_card_summary_exposes_actor_specific_timestamps() -> None:
    request = SimpleNamespace(url_for=lambda name, card_id: f"http://test/{card_id}/cover")
    payload = {"title": "Test Book", "content": {"chapters": []}}
    summary = _card_summary(request, "abs-1", payload, {
        "source": "abs", "yoto_card_id": "yoto-1",
        "external_updated_at": "1791500000000",
        "updated_at": "2026-10-09T10:00:00Z",
        "pushed_at": "2026-10-09T09:00:00Z",
    })
    assert summary.yoto_card_id == "yoto-1"
    assert summary.updated_at == "1791500000000"
    assert summary.last_pushed_at == "2026-10-09T09:00:00Z"


def test_sync_timestamps_are_user_scoped() -> None:
    from tests.test_yoto_store import _FakeRedis
    from yoto_bulk.store import YotoStore
    from yoto_server.routes_jobs import sync_status

    async def run():
        redis = _FakeRedis()
        await redis.hset("ui:sync:user-1", mapping={"last_abs_sync": "2026-10-09T09:00:00Z"})
        store = YotoStore(redis)
        owned = await sync_status(store=store, user_id="user-1")
        other = await sync_status(store=store, user_id="user-2")
        return owned, other

    owned, other = asyncio.run(run())
    assert owned["last_abs_sync"] == "2026-10-09T09:00:00Z"
    assert other["last_abs_sync"] is None


def test_cover_uses_uploaded_yoto_art_when_abs_certificate_is_expired(monkeypatch) -> None:
    from tests.test_yoto_store import _FakeRedis
    from yoto_bulk.store import YotoStore
    from yoto_server import routes

    class AbsClient:
        async def fetch_cover(self, item_id):
            raise RuntimeError("certificate has expired")

        async def aclose(self):
            pass

    class CoverClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url):
            assert url == "https://card-content.yotoplay.com/cover"
            return SimpleNamespace(
                content=b"uploaded cover", headers={"content-type": "image/jpeg"},
                raise_for_status=lambda: None,
            )

    monkeypatch.setattr(routes, "get_abs_client", lambda request: AbsClient())
    monkeypatch.setattr(routes.httpx, "AsyncClient", lambda **kwargs: CoverClient())

    async def run():
        store = YotoStore(_FakeRedis())
        await store.save_card("user-1", "abs-1", {
            "title": "Test", "metadata": {"cover": {"imageL": "https://card-content.yotoplay.com/cover"}},
        }, source="abs", external_id="item-1")
        return await routes.get_card_cover("abs-1", SimpleNamespace(), store, "user-1")

    response = asyncio.run(run())
    assert response.body == b"uploaded cover"
    assert response.headers["x-cover-source"] == "yoto-cache"
    assert response.headers["cache-control"] == "private, max-age=300"


def test_interrupted_abs_push_saves_completed_audio_references(monkeypatch) -> None:
    from tests.test_yoto_store import _FakeRedis
    from yoto_bulk.store import YotoStore
    from yoto_server import routes

    async def noop(*args, **kwargs):
        return {}

    async def interrupted(client, abs_client, item_id, payload, title):
        payload["content"]["chapters"][0]["tracks"][0]["trackUrl"] = "yoto:#completed"
        raise asyncio.CancelledError()

    monkeypatch.setattr(routes, "YotoAuthService", lambda settings: SimpleNamespace(
        token_store=SimpleNamespace(load=lambda: object()),
    ))
    monkeypatch.setattr(routes, "YotoClient", lambda *args: SimpleNamespace(
        get_content_card=noop,
    ))
    monkeypatch.setattr(routes, "get_abs_client", lambda request: SimpleNamespace(aclose=noop))
    monkeypatch.setattr(routes, "_upload_cover_from_abs", noop)
    monkeypatch.setattr(routes, "_upload_audio_from_abs", interrupted)

    async def run():
        store = YotoStore(_FakeRedis())
        store.backup_card = noop
        await store.save_card("user-1", "abs-1", {
            "title": "Test Book", "metadata": {},
            "content": {"chapters": [{"title": "Opening", "tracks": [{"trackUrl": ""}]}]},
        }, source="abs", external_id="item-1")
        await store._r.hset(store._meta_key("user-1", "abs-1"), mapping={"yoto_card_id": "yoto-1"})
        with pytest.raises(asyncio.CancelledError):
            await routes.push_card("abs-1", SimpleNamespace(), store, "user-1", SimpleNamespace())
        saved = await store.load_card("user-1", "abs-1")
        meta = await store.get_meta("user-1", "abs-1")
        assert saved["content"]["chapters"][0]["tracks"][0]["trackUrl"] == "yoto:#completed"
        assert meta["source"] == "abs"
        assert meta["external_id"] == "item-1"

    asyncio.run(run())


def test_upload_audio_from_abs_processes_tracks_concurrently() -> None:
    class FakeAbsClient:
        def __init__(self) -> None:
            self.urls: list[str] = []

        async def get_playback_session(self, item_id: str) -> dict:
            return {
                "audioTracks": [
                    {"contentUrl": "https://abs/1.mp3"},
                    {"contentUrl": "https://abs/2.mp3"},
                    {"contentUrl": "https://abs/3.mp3"},
                ]
            }

        @asynccontextmanager
        async def download_audio_track_stream(self, content_url: str):
            self.urls.append(content_url)

            async def _stream():
                yield b"audio"

            yield _stream(), "audio/mpeg", 5

    class FakeYotoClient:
        def __init__(self) -> None:
            self.inflight = 0
            self.max_inflight = 0

        async def upload_stream_and_transcode(
            self,
            stream,
            content_type: str,
            content_length: int | None = None,
        ) -> dict:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            await asyncio.sleep(0.02)
            self.inflight -= 1
            return {
                "transcodedSha256": f"sha-{self.max_inflight}",
                "transcodedInfo": {
                    "duration": 12,
                    "fileSize": 34,
                    "channels": "stereo",
                    "format": "mp3",
                },
            }

    payload = {
        "content": {
            "chapters": [
                {"tracks": [{}]},
                {"tracks": [{}]},
                {"tracks": [{}]},
            ]
        }
    }
    abs_client = FakeAbsClient()
    yoto_client = FakeYotoClient()

    asyncio.run(_upload_audio_from_abs(yoto_client, abs_client, "abs-1", payload, "Test Book"))

    assert yoto_client.max_inflight > 1
    for chapter in payload["content"]["chapters"]:
        track = chapter["tracks"][0]
        assert track["trackUrl"].startswith("yoto:#")
        assert track["format"] == "mp3"


def test_upload_audio_from_abs_preserves_successes_when_one_track_fails() -> None:
    class FakeAbsClient:
        async def get_playback_session(self, item_id: str) -> dict:
            return {
                "audioTracks": [
                    {"contentUrl": "https://abs/1.mp3"},
                    {"contentUrl": "https://abs/2.mp3"},
                    {"contentUrl": "https://abs/3.mp3"},
                ]
            }

        @asynccontextmanager
        async def download_audio_track_stream(self, content_url: str):
            track_number = int(content_url.rsplit("/", 1)[1].split(".", 1)[0])

            async def _stream():
                yield f"audio-{track_number}".encode()

            yield _stream(), "audio/mpeg", track_number

    class FakeYotoClient:
        async def upload_stream_and_transcode(
            self,
            stream,
            content_type: str,
            content_length: int | None = None,
        ) -> dict:
            await asyncio.sleep(0.01)
            if content_length == 2:
                raise RuntimeError("transcode timeout")
            return {
                "transcodedSha256": f"sha-{content_length}",
                "transcodedInfo": {
                    "duration": content_length,
                    "fileSize": content_length,
                    "channels": "stereo",
                    "format": "mp3",
                },
            }

    payload = {
        "content": {
            "chapters": [
                {"tracks": [{}]},
                {"tracks": [{}]},
                {"tracks": [{}]},
            ]
        }
    }

    with pytest.raises(RuntimeError, match="transcode timeout"):
        asyncio.run(
            _upload_audio_from_abs(
                FakeYotoClient(),
                FakeAbsClient(),
                "abs-1",
                payload,
                "Test Book",
            )
        )

    tracks = [chapter["tracks"][0] for chapter in payload["content"]["chapters"]]
    assert tracks[0]["trackUrl"] == "yoto:#sha-1"
    assert "trackUrl" not in tracks[1]
    assert tracks[2]["trackUrl"] == "yoto:#sha-3"


def test_upload_audio_from_abs_uses_episode_playback_for_podcast_chapters() -> None:
    class FakeAbsClient:
        def __init__(self) -> None:
            self.episode_ids: list[str] = []

        async def get_episode_playback_session(self, item_id: str, episode_id: str) -> dict:
            self.episode_ids.append(episode_id)
            return {"audioTracks": [{"contentUrl": f"https://abs/{episode_id}.mp3"}]}

        @asynccontextmanager
        async def download_audio_track_stream(self, content_url: str):
            async def _stream():
                yield b"audio"

            yield _stream(), "audio/mpeg", 5

    class FakeYotoClient:
        async def upload_stream_and_transcode(self, stream, content_type, content_length=None) -> dict:
            return {
                "transcodedSha256": "episode-audio",
                "transcodedInfo": {
                    "duration": 12,
                    "fileSize": 34,
                    "channels": "stereo",
                    "format": "mp3",
                },
            }

    payload = {
        "metadata": {"category": "podcast"},
        "content": {
            "chapters": [
                {"key": "episode-2", "tracks": [{"trackUrl": ""}]},
                {"key": "episode-1", "tracks": [{"trackUrl": "yoto:#existing"}]},
            ]
        },
    }
    abs_client = FakeAbsClient()

    asyncio.run(
        _upload_audio_from_abs(FakeYotoClient(), abs_client, "podcast-1", payload, "Test Show")
    )

    assert abs_client.episode_ids == ["episode-2"]
    assert payload["content"]["chapters"][0]["tracks"][0]["trackUrl"] == "yoto:#episode-audio"
    assert payload["content"]["chapters"][1]["tracks"][0]["trackUrl"] == "yoto:#existing"


def test_save_partial_abs_push_does_not_mark_card_as_synced() -> None:
    class FakeStore:
        def __init__(self) -> None:
            self.calls = []

        async def save_card(self, *args, **kwargs) -> None:
            self.calls.append((args, kwargs))

    payload = {"metadata": {"title": "Test Book"}, "content": {"chapters": []}}
    store = FakeStore()

    asyncio.run(_save_partial_abs_push(store, "user-1", "abs-1", payload, "abs-1"))

    assert store.calls == [
        (("user-1", "abs-1", payload), {"source": "abs", "external_id": "abs-1"})
    ]
