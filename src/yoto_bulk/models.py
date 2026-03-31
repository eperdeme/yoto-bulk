"""Typed domain models for Yoto and local inventory data."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


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
