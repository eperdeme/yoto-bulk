"""Pydantic models for API request/response schemas."""

from __future__ import annotations

from pydantic import BaseModel


class LibraryInfo(BaseModel):
    name: str
    subdomain: str
    is_authenticated: bool = False


class SearchResult(BaseModel):
    product_id: str
    title: str
    authors: list[str] = []
    series_name: str | None = None
    series_number: int | None = None
    series_id: str | None = None
    availability: str = "unknown"
    available_date: str | None = None
    format: str | None = None
    isbn: str | None = None
    description: str | None = None
    cover_url: str | None = None
    product_url: str | None = None


class SeriesInfo(BaseModel):
    series_id: str
    name: str
    items: list[SearchResult] = []


class AvailabilityInfo(BaseModel):
    product_id: str
    library: str
    status: str
    available_date: str | None = None
    checked_at: float | None = None


class MultiLibraryAvailability(BaseModel):
    product_id: str
    title: str
    libraries: dict[str, AvailabilityInfo] = {}


class LoanInfo(BaseModel):
    product_id: str
    title: str
    authors: list[str] = []
    start_date: str | None = None
    end_date: str | None = None
    format: str | None = None
    cover_url: str | None = None
    loan_type: str | None = None
    loan_id: str | None = None
    total_tracks: int | None = None
    file_size: int | None = None
    in_abs: bool = False
    abs_item_id: str | None = None


class BorrowRequest(BaseModel):
    product_id: str
    library: str
    loan_id: str | None = None


class BorrowResponse(BaseModel):
    success: bool
    message: str = ""
    product_id: str = ""
    library: str = ""


class CrossLibraryResult(BaseModel):
    """Search result with availability across all libraries."""
    product_id: str
    title: str
    authors: list[str] = []
    series_name: str | None = None
    series_number: int | None = None
    series_id: str | None = None
    format: str | None = None
    isbn: str | None = None
    description: str | None = None
    cover_url: str | None = None
    product_url: str | None = None
    libraries: dict[str, AvailabilityInfo] = {}
    best_library: str | None = None
    best_status: str = "unknown"


class YotoCard(BaseModel):
    """A card from the local Yoto library."""
    card_id: str
    title: str
    authors: list[str] = []
    series: str | None = None
    sequence_number: int | None = None
    cover_url: str | None = None
    chapter_count: int = 0


class MissingBook(BaseModel):
    series_name: str
    series_number: int | None = None
    title: str
    authors: list[str] = []
    availability: dict[str, AvailabilityInfo] = {}


class MissingSummary(BaseModel):
    total_local_titles: int = 0
    series_discovered: int = 0
    api_calls: int = 0
    missing_books: list[MissingBook] = []
