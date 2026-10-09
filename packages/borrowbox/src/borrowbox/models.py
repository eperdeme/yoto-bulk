"""Data models for BorrowBox catalog items."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Format(str, Enum):
    AUDIOBOOK = "eAudiobooks"
    EBOOK = "eBooks"
    ALL = "all"


class Availability(str, Enum):
    AVAILABLE = "available"
    ON_LOAN = "on_loan"
    UNKNOWN = "unknown"


@dataclass
class CatalogItem:
    """A single search result or series entry from BorrowBox."""

    product_id: str
    title: str
    authors: list[str] = field(default_factory=list)
    narrators: list[str] = field(default_factory=list)
    series_name: str | None = None
    series_number: int | None = None
    series_id: str | None = None
    availability: Availability = Availability.UNKNOWN
    available_date: str | None = None
    format: str | None = None
    isbn: str | None = None
    description: str | None = None
    cover_url: str | None = None
    product_url: str | None = None


@dataclass
class ProductDetail:
    """Detailed metadata from a BorrowBox product page."""

    product_id: str
    title: str
    authors: list[str] = field(default_factory=list)
    narrators: list[str] = field(default_factory=list)
    description: str | None = None
    isbn: str | None = None
    duration_seconds: int | None = None
    format: str | None = None
    file_type: str | None = None
    language: str | None = None
    publisher: str | None = None
    released: str | None = None
    series_name: str | None = None
    series_number: int | None = None
    series_id: str | None = None
    availability: Availability = Availability.UNKNOWN
    available_date: str | None = None
    cover_url: str | None = None
    product_url: str | None = None
