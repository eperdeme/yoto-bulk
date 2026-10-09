"""Pydantic models for the Yoto server API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class YotoCardSummary(BaseModel):
    """Compact card info for list views."""
    card_id: str
    yoto_card_id: str | None = None
    source: str | None = None
    media_type: str = "book"
    needs_push: bool = False
    title: str
    authors: list[str] = Field(default_factory=list)
    series: str | None = None
    sequence_number: int | None = None
    description: str | None = None
    cover_url: str | None = None
    chapter_count: int = 0
    last_played_at: str | None = None
    updated_at: str | None = None
    last_pushed_at: str | None = None


class SeriesSuggestion(BaseModel):
    """Suggested series for a set of books."""
    series_name: str
    card_ids: list[str]
    sequence_numbers: dict[str, int] = Field(default_factory=dict)
    # "abs" when grouped from Audiobookshelf metadata, "llm" when inferred
    # by OpenAI. Optional for backwards compatibility with existing payloads.
    provenance: str | None = None


class SeriesDiscoveryResponse(BaseModel):
    """Response from series discovery endpoint."""
    series: list[SeriesSuggestion]
    unmatched_card_ids: list[str]
    abs_group_count: int = 0
    llm_group_count: int = 0
    used_llm: bool = False
    # Copy-paste prompt for grouping the residue (cards ABS could not group)
    # with an external LLM tool. Present only when there is ungrouped residue
    # and the server did not group it automatically.
    manual_prompt: str | None = None


class ManualSeriesRequest(BaseModel):
    """Raw grouping JSON pasted back from an external LLM tool."""
    response: str


class SeriesCatalogItem(BaseModel):
    """One known title in an upstream series catalog."""
    title: str
    authors: list[str] = Field(default_factory=list)
    series_name: str | None = None
    series_number: float | None = None
    series_id: str | None = None
    source: str = "abs"
    provider: str | None = None
    provider_id: str | None = None
    abs_item_id: str | None = None
    product_id: str | None = None
    availability: str | None = None
    available_date: str | None = None
    format: str | None = None
    cover_url: str | None = None


class SeriesCatalogInfo(BaseModel):
    """Series catalog data from ABS or another metadata source."""
    series_id: str
    name: str
    source: str = "abs"
    items: list[SeriesCatalogItem] = Field(default_factory=list)


class BulkSeriesUpdate(BaseModel):
    """Payload for applying series names to multiple cards."""
    suggestions: list[SeriesSuggestion]


class BulkSeriesApplyResponse(BaseModel):
    """Result of applying series suggestions to cards."""
    status: str = "applied"
    count: int = 0
    skipped_yoto_source: list[str] = Field(default_factory=list)
    skipped_missing_external: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)


class CardMetadataUpdate(BaseModel):
    """Per-card metadata edit, applied to ABS for ABS-backed cards."""
    title: str | None = None
    author: str | None = None  # comma-separated for multi-author
    series: str | None = None  # empty string clears the series
    sequence_number: int | None = None
    description: str | None = None


class ChapterInfo(BaseModel):
    """Chapter summary."""
    title: str = ""
    key: str | None = None
    track_count: int = 0
    duration: float | None = None


class YotoCardDetail(BaseModel):
    """Full card detail for viewing."""
    card_id: str
    yoto_card_id: str | None = None
    source: str | None = None
    needs_push: bool = False
    media_type: str = "book"
    title: str
    authors: list[str] = Field(default_factory=list)
    series: str | None = None
    sequence_number: int | None = None
    description: str | None = None
    cover_url: str | None = None
    external_url: str | None = None
    diff_fields: list[str] = Field(default_factory=list)
    chapter_count: int = 0
    chapters: list[ChapterInfo] = Field(default_factory=list)
    raw_metadata: dict = Field(default_factory=dict)
    updated_at: str | None = None
    last_pushed_at: str | None = None


class PushResult(BaseModel):
    """Result of pushing a card to the Yoto API."""
    card_id: str
    title: str
    status: str  # "pushed" | "error"
    message: str = ""
    new_card_id: str | None = None


class AuthStatus(BaseModel):
    """Yoto authentication status."""
    authenticated: bool
    token_path: str = ""
    user_id: str | None = None


class FamilyLibraryCard(BaseModel):
    """A card from the Yoto family library (purchased / shared)."""
    card_id: str
    title: str = ""
    author: str | None = None
    description: str | None = None
    cover_url: str | None = None
    category: str | None = None
    share_type: str | None = None
    last_played_at: str | None = None
    added_at: str | None = None
    slug: str | None = None
    reason: str | None = None
    languages: list[str] = Field(default_factory=list)
    media: dict | None = None
    audio_preview_url: str | None = None
    playback_type: str | None = None
