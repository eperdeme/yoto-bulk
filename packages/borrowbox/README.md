# borrowbox

Python client for [BorrowBox](https://www.borrowbox.com/) library audiobook catalogs.

Provides read-only access to search, series listing, and product detail via
the Next.js `/_next/data/` JSON endpoints exposed by BorrowBox. No
authentication is required for catalog browsing.

## Installation

```bash
pip install borrowbox
# or
uv add borrowbox
```

## Usage

```python
from borrowbox import BorrowBoxCatalog, Format

with BorrowBoxCatalog("hounslow") as catalog:
    # Search for audiobooks
    results = catalog.search("Tom Gates", format_filter=Format.AUDIOBOOK)
    for item in results:
        print(f"{item.title} by {', '.join(item.authors)}")
        print(f"  Series: {item.series_name} #{item.series_number}")
        print(f"  Status: {item.availability.value}")

    # List all books in a series
    series = catalog.get_series(series_id=8234)
    for book in series:
        print(f"  #{book.series_number}: {book.title}")

    # Get detailed product info
    detail = catalog.get_product("12345")
    if detail:
        print(f"{detail.title} ({detail.duration_seconds}s)")
```

## Library parameter

The `library` parameter is the subdomain of your BorrowBox library
(e.g. `"hounslow"` for `hounslow.borrowbox.com`).
