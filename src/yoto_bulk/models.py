"""Typed domain models for Yoto and local inventory data."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OAuthTokenSet(BaseModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_in: int | None = None
    scope: str | None = None


class YotoBook(BaseModel):
    id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    series: str | None = None
    sequence_number: int | None = None
    description: str | None = None


class LocalAudiobook(BaseModel):
    path: Path
    stem: str
    size_bytes: int


class ReconcileMatch(BaseModel):
    local_item: LocalAudiobook
    yoto_book: YotoBook | None = None
    confidence: float = 0.0
    reason: str = "unmatched"


# ------------------------------------------------------------------
# Yoto API card model hierarchy
#
# These Pydantic models represent the full Yoto card payload as
# returned and accepted by the REST API.  They are used for
# validation at API boundaries and typed access in new code.
#
# Every model uses ``extra="allow"`` so unknown fields from the API
# are preserved during round-trips  (parse → modify → serialize).
# ------------------------------------------------------------------


class TrackDisplay(BaseModel):
    model_config = ConfigDict(extra="allow")
    icon16x16: str | None = None


class Track(BaseModel):
    model_config = ConfigDict(extra="allow")
    title: str = ""
    trackUrl: str = ""
    key: str = "01"
    format: str = "mp3"
    type: str = "audio"
    display: TrackDisplay | None = None
    overlayLabel: str | None = None
    duration: float | None = None
    fileSize: float | None = None
    channels: str | None = None
    uid: str | None = None


class ChapterDisplay(BaseModel):
    model_config = ConfigDict(extra="allow")
    icon16x16: str | None = None


class Chapter(BaseModel):
    model_config = ConfigDict(extra="allow")
    title: str = ""
    key: str | None = None
    overlayLabel: str | None = None
    tracks: list[Track] = Field(default_factory=list)
    display: ChapterDisplay | None = None
    duration: float | None = None
    fileSize: float | None = None


class CardCover(BaseModel):
    model_config = ConfigDict(extra="allow")
    imageL: str | None = None


class CardMedia(BaseModel):
    model_config = ConfigDict(extra="allow")
    duration: float | None = None
    fileSize: float | None = None


class CardConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    resumeTimeout: int | None = None
    onlineOnly: bool | None = None
    autoadvance: str | None = None


class CardStatus(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str | None = None
    updatedAt: str | None = None


class CardMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")
    author: str | None = None
    category: str | None = None
    cover: CardCover | None = None
    description: str | None = None
    genre: list[str] | None = None
    languages: list[str] | None = None
    media: CardMedia | None = None
    series: str | None = None
    sequence_number: int | None = None
    status: CardStatus | None = None
    tags: list[str] | None = None
    title: str | None = None
    note: str | None = None


class CardContent(BaseModel):
    model_config = ConfigDict(extra="allow")
    chapters: list[Chapter] | None = None
    config: CardConfig | None = None
    activity: str | None = None
    playbackType: str | None = None
    version: str | None = None


class CardPayload(BaseModel):
    """Full Yoto card payload as returned/accepted by the API.

    Use ``CardPayload.model_validate(data)`` to parse an API response
    and ``card.model_dump(exclude_none=True)`` to serialize for upload.
    """

    model_config = ConfigDict(extra="allow")

    cardId: str | None = None
    title: str = "Untitled"
    metadata: CardMetadata | None = None
    content: CardContent | None = None
    tags: list[str] | None = None
    slug: str | None = None
    deleted: bool = False
    createdAt: str | None = None
    updatedAt: str | None = None
    userId: str | None = None

    def get_chapters(self) -> list[Chapter]:
        if self.content and self.content.chapters:
            return self.content.chapters
        return []

    def get_metadata(self) -> CardMetadata:
        if self.metadata is None:
            self.metadata = CardMetadata()
        return self.metadata

    def get_cover_url(self) -> str | None:
        meta = self.get_metadata()
        if meta.cover:
            return meta.cover.imageL
        return None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CardPayload:
        """Parse a raw API dict into a validated ``CardPayload``."""
        return cls.model_validate(data)

    def to_dict(self) -> dict[str, Any]:
        """Serialize back to a plain dict suitable for the API or JSON storage."""
        return self.model_dump(exclude_none=True)
