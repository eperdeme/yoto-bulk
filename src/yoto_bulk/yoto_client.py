"""Typed async client for the Yoto API."""

from __future__ import annotations

import asyncio
from yoto_bulk.workflows import report_workflow
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import hashlib
import logging
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from yoto_bulk.auth import YotoAuthService
from yoto_bulk.card_utils import (
    metadata_block,
    resolve_card_id,
    resolve_description,
    resolve_series,
    resolve_title,
    resolve_authors,
    resolve_sequence_number,
)
from yoto_bulk.config import Settings
from yoto_bulk.models import OAuthTokenSet, YotoBook


logger = logging.getLogger(__name__)


class YotoApiError(RuntimeError):
    """Raised when the Yoto API returns an invalid response."""


class RateLimitError(YotoApiError):
    """Raised on HTTP 429 so tenacity can retry with back-off."""

    def __init__(self, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(f"Rate limited (retry after {retry_after}s)")


class YotoClient:
    def __init__(self, settings: Settings, auth_service: YotoAuthService) -> None:
        self.settings = settings
        self.auth_service = auth_service

    @asynccontextmanager
    async def session(self) -> AsyncIterator[httpx.AsyncClient]:
        token_set = self.auth_service.token_store.load()
        if token_set is None:
            raise YotoApiError(
                "No saved OAuth tokens. Configure Yoto OAuth tokens before calling the Yoto API."
            )

        headers = self._headers(token_set)
        async with httpx.AsyncClient(
            base_url=self.settings.yoto_api_base_url,
            timeout=self.settings.request_timeout_seconds,
            headers=headers,
        ) as client:
            yield client

    def _headers(self, token_set: OAuthTokenSet) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token_set.access_token}",
            "Accept": "application/json",
        }

    @staticmethod
    def _content_id_from_item(item: dict[str, Any]) -> str:
        return resolve_card_id(item)

    @staticmethod
    def _metadata(item: dict[str, Any]) -> dict[str, Any]:
        return metadata_block(item)

    def _resolve_title(self, item: dict[str, Any]) -> str:
        return resolve_title(item)

    def _resolve_description(self, item: dict[str, Any]) -> str | None:
        return resolve_description(item)

    def _resolve_series(self, item: dict[str, Any]) -> str | None:
        return resolve_series(item)

    # ------------------------------------------------------------------
    # Shared response handling
    # ------------------------------------------------------------------

    async def _handle_response(self, response: httpx.Response, client: httpx.AsyncClient) -> None:
        """Handle 401/403 (refresh + retry) and 429 (rate limit) before raise_for_status."""
        if response.status_code in (401, 403):
            new_tokens = await self.auth_service.refresh_access_token()
            client.headers["Authorization"] = f"Bearer {new_tokens.access_token}"
            raise YotoApiError("Token refreshed, retrying request")

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else None
            if delay:
                logger.info("Rate limited, sleeping %ss", delay)
                await asyncio.sleep(delay)
            raise RateLimitError(retry_after=delay)

    # ------------------------------------------------------------------
    # HTTP verbs
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=15),
        retry=retry_if_exception_type(
            (httpx.TimeoutException, httpx.NetworkError, YotoApiError)
        ),
        reraise=True,
    )
    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self.session() as client:
            response = await client.get(path, params=params)
            await self._handle_response(response, client)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise YotoApiError(f"Expected object response for {path}")
            return payload

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=15),
        retry=retry_if_exception_type(
            (httpx.TimeoutException, httpx.NetworkError, YotoApiError)
        ),
        reraise=True,
    )
    async def get_json_list(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        async with self.session() as client:
            response = await client.get(path, params=params)
            await self._handle_response(response, client)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise YotoApiError(f"Expected array response for {path}")
            return [item for item in payload if isinstance(item, dict)]

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=15),
        retry=retry_if_exception_type(
            (httpx.TimeoutException, httpx.NetworkError, YotoApiError)
        ),
        reraise=True,
    )
    async def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self.session() as client:
            response = await client.post(path, json=payload)
            await self._handle_response(response, client)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if response.status_code == 400:
                    logger.error(
                        "Yoto API rejected POST request with HTTP 400. Response body: %s",
                        exc.response.text,
                    )
                raise
            data = response.json()
            if not isinstance(data, dict):
                raise YotoApiError(f"Expected object response for {path}")
            return data

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=15),
        retry=retry_if_exception_type(
            (httpx.TimeoutException, httpx.NetworkError, YotoApiError)
        ),
        reraise=True,
    )
    async def put_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self.session() as client:
            response = await client.put(path, json=payload)
            await self._handle_response(response, client)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise YotoApiError(f"Expected object response for {path}")
            return data

    async def get_content_mine(self, show_deleted: bool = False) -> dict[str, Any]:
        """Return the authenticated user's MYO content listing.

        Per the Yoto API reference, the documented collection endpoint for a
        user's owned cards is [`GET /content/mine`](src/yoto_bulk/yoto_client.py:65).
        """
        return await self.get_json("/content/mine", params={"showdeleted": str(show_deleted).lower()})

    async def get_content_card(self, card_id: str, *, playable: bool = False) -> dict[str, Any]:
        """Return the full payload for a specific library card.

        When *playable* is ``True`` the API resolves ``yoto:#`` references into
        signed download URLs (``?playable=true&signingType=s3``).
        """
        params: dict[str, str] = {}
        if playable:
            params["playable"] = "true"
            params["signingType"] = "s3"
        payload = await self.get_json(f"/content/{card_id}", params=params or None)
        card = payload.get("card")
        if not isinstance(card, dict):
            raise YotoApiError(f"get_content_card({card_id}): response missing 'card' key")
        return card

    async def save_content(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist a full content payload back to the Yoto API.

        Applies ``truncate_for_api`` to the original payload (for local
        storage limits) then ``sanitize_for_yoto_api`` to produce a clean
        copy with only Yoto-valid fields before posting.
        """
        from yoto_bulk.card_utils import sanitize_for_yoto_api, truncate_for_api

        truncate_for_api(payload)
        api_payload = sanitize_for_yoto_api(payload)
        return await self.post_json("/content", api_payload)

    async def delete_content(self, card_id: str) -> None:
        """Delete a MYO card from the Yoto API.

        ``DELETE /content/{cardId}`` — only works for user-created MYO cards.
        """
        async with self.session() as client:
            response = await client.delete(f"/content/{card_id}")
            await self._handle_response(response, client)
            response.raise_for_status()

    # ------------------------------------------------------------------
    # Audio upload & transcoding
    # ------------------------------------------------------------------

    async def get_upload_url(self, *, sha256: str | None = None) -> dict[str, Any]:
        """Request a signed upload URL for an audio file.

        When *sha256* is provided the API can short-circuit if the file
        already exists server-side, returning a null ``uploadUrl`` but a
        valid ``uploadId``.

        Returns ``{"uploadUrl": "<url>"|None, "uploadId": "<id>"}``.
        """
        params: dict[str, str] = {}
        if sha256:
            params["sha256"] = sha256
        data = await self.get_json(
            "/media/transcode/audio/uploadUrl", params=params or None
        )
        upload = data.get("upload", data)
        return {
            "uploadUrl": upload.get("uploadUrl"),
            "uploadId": upload.get("uploadId"),
        }

    async def upload_audio_file(self, upload_url: str, file_data: bytes, content_type: str) -> None:
        """PUT raw audio bytes to the signed upload URL."""
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.put(
                upload_url,
                content=file_data,
                headers={"Content-Type": content_type},
                extensions={"expect_header": False},
            )
            resp.raise_for_status()

    async def upload_audio_stream(
        self,
        upload_url: str,
        stream: AsyncIterator[bytes],
        content_type: str,
        content_length: int | None = None,
    ) -> None:
        """PUT raw audio stream to the signed upload URL."""
        headers = {"Content-Type": content_type}
        if content_length is not None:
            headers["Content-Length"] = str(content_length)

        async with httpx.AsyncClient(timeout=600) as client:
            resp = await client.put(
                upload_url,
                content=stream,
                headers=headers,
                extensions={"expect_header": False},
            )
            resp.raise_for_status()

    async def poll_transcode(
        self,
        upload_id: str,
        *,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float | None = None,
        max_attempts: int | None = None,
    ) -> dict[str, Any]:
        """Poll until transcoding completes, returning transcode info.

        Returns a dict with ``transcodedSha256`` and ``transcodedInfo``
        (containing ``duration``, ``fileSize``, ``channels``, ``format``).

        Raises :class:`YotoApiError` on timeout.
        """
        interval = poll_interval_seconds
        if interval is None:
            interval = getattr(self.settings, "yoto_transcode_poll_interval_seconds", 2.0)
        timeout = timeout_seconds
        if timeout is None:
            if max_attempts is not None:
                timeout = max_attempts * interval
            else:
                timeout = getattr(self.settings, "yoto_transcode_timeout_seconds", 600.0)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        await report_workflow("Transcoding audio")
        while True:
            try:
                data = await self.get_json(
                    f"/media/upload/{upload_id}/transcoded",
                    params={"loudnorm": "false"},
                )
                transcode = data.get("transcode", data)
                if transcode.get("transcodedSha256"):
                    return transcode
            except Exception:
                pass

            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(interval, remaining))
        raise YotoApiError(f"Transcoding timed out after {timeout:.1f}s for upload {upload_id}")

    async def upload_and_transcode(self, file_data: bytes, content_type: str) -> dict[str, Any]:
        """Upload an audio file and wait for transcoding.

        Computes the SHA256 of *file_data* and passes it to the upload-URL
        endpoint.  If the file already exists server-side the upload step
        is skipped entirely (dedup).

        Returns the transcode result with ``transcodedSha256`` and
        ``transcodedInfo``.
        """
        sha256 = hashlib.sha256(file_data).hexdigest()
        upload = await self.get_upload_url(sha256=sha256)
        upload_url = upload["uploadUrl"]
        upload_id = upload["uploadId"]

        if upload_url is None:
            if upload_id:
                logger.info("Audio already on server (SHA256 match), skipping upload")
                return await self.poll_transcode(upload_id)
            raise YotoApiError("API returned null uploadUrl and no uploadId")

        await self.upload_audio_file(upload_url, file_data, content_type)
        return await self.poll_transcode(upload_id)

    async def upload_stream_and_transcode(
        self,
        stream: AsyncIterator[bytes],
        content_type: str,
        sha256: str | None = None,
        content_length: int | None = None,
    ) -> dict[str, Any]:
        """Upload an audio stream and wait for transcoding.

        If *sha256* is provided, the API can short-circuit if the file
        already exists server-side (dedup).
        """
        upload = await self.get_upload_url(sha256=sha256)
        upload_url = upload["uploadUrl"]
        upload_id = upload["uploadId"]

        if upload_url is None:
            if upload_id:
                logger.info("Audio already on server (SHA256 match), skipping upload")
                return await self.poll_transcode(upload_id)
            raise YotoApiError("API returned null uploadUrl and no uploadId")

        await report_workflow("Uploading audio")
        await self.upload_audio_stream(
            upload_url, stream, content_type, content_length=content_length
        )
        return await self.poll_transcode(upload_id)

    async def upload_cover_image(self, file_data: bytes, content_type: str) -> str:
        """Upload a cover image and return its mediaUrl.

        Uses ``POST /media/coverImage/user/me/upload?autoconvert=true``.
        """
        async with self.session() as client:
            resp = await client.post(
                "/media/coverImage/user/me/upload",
                params={"autoconvert": "true", "coverType": "default"},
                content=file_data,
                headers={"Content-Type": content_type},
            )
            await self._handle_response(resp, client)
            resp.raise_for_status()
            data = resp.json()
            cover = data.get("coverImage", data)
            media_url = cover.get("mediaUrl")
            if not media_url:
                raise YotoApiError("Cover upload returned no mediaUrl")
            return media_url

    async def list_family_library_groups(self) -> list[dict[str, Any]]:
        """Return the family's current library groups."""
        return await self.get_json_list("/card/family/library/groups")

    async def get_family_library_group(self, group_id: str) -> dict[str, Any]:
        """Return the full payload for a family library group."""
        return await self.get_json(f"/card/family/library/groups/{group_id}")

    async def create_family_library_group(
        self,
        name: str,
        items: list[dict[str, Any]],
        image_id: str = "fp-cards",
    ) -> dict[str, Any]:
        """Create a family library group with the supplied item references.

        Per the API docs, ``items`` must be an array of ``{"contentId": "..."}``
        objects.
        """
        return await self.post_json(
            "/card/family/library/groups",
            {
                "name": name,
                "imageId": image_id,
                "items": items,
            },
        )

    async def update_family_library_group(
        self,
        group_id: str,
        name: str,
        items: list[dict[str, Any]],
        image_id: str = "fp-cards",
    ) -> dict[str, Any]:
        """Replace the contents of an existing family library group.

        Per the API docs, ``items`` must be an array of ``{"contentId": "..."}``
        objects.
        """
        return await self.put_json(
            f"/card/family/library/groups/{group_id}",
            {
                "name": name,
                "imageId": image_id,
                "items": items,
            },
        )

    def family_group_content_ids(self, group: dict[str, Any]) -> set[str]:
        """Extract content ids from a family library group payload."""
        items = group.get("items", [])
        content_ids = {
            content_id
            for item in items
            if isinstance(item, dict)
            for content_id in [self._content_id_from_item(item)]
            if content_id
        }
        if content_ids:
            return content_ids

        cards = group.get("cards", [])
        return {
            content_id
            for card in cards
            if isinstance(card, dict)
            for content_id in [self._content_id_from_item(card)]
            if content_id
        }

    async def list_library_books(self) -> list[YotoBook]:
        """Return books from the authenticated user's Yoto library.

        The Yoto docs expose owned content via [`GET /content/mine`](src/yoto_bulk/yoto_client.py:72),
        which returns a top-level `cards` array rather than the guessed
        [`GET /v1/library`](src/yoto_bulk/yoto_client.py:72) endpoint used previously.
        """
        payload = await self.get_content_mine()
        items = payload.get("cards", [])
        books: list[YotoBook] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            books.append(
                YotoBook(
                    id=str(item.get("cardId") or item.get("id") or ""),
                    title=resolve_title(item),
                    authors=resolve_authors(item),
                    series=resolve_series(item),
                    sequence_number=resolve_sequence_number(item),
                    description=resolve_description(item),
                )
            )
        return books

    def _coerce_authors(self, item: dict[str, Any]) -> list[str]:
        return resolve_authors(item)

    def _coerce_sequence(self, item: dict[str, Any]) -> int | None:
        return resolve_sequence_number(item)
