"""BorrowBox catalog client — searches and browses library audiobook catalogs.

Uses the Next.js ``/_next/data/`` JSON endpoints exposed by BorrowBox for
search, product detail, and series listing. The ``buildId`` required for these
endpoints is discovered once from the homepage ``__NEXT_DATA__`` payload
and cached for the session. No authentication is required.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from borrowbox.models import Availability, CatalogItem, Format, ProductDetail

logger = logging.getLogger(__name__)

# BorrowBox requires a modern browser User-Agent; otherwise it redirects to
# /outdated.html or returns a 202 with an empty body.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Chrome-like headers that help avoid AWS WAF Bot Control challenges.
# WAF uses these to distinguish real browsers from automated clients.
_BROWSER_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
    "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}


class BorrowBoxCatalog:
    """Read-only BorrowBox catalog client using Next.js JSON endpoints."""

    def __init__(self, library: str, timeout: float = 30.0) -> None:
        self.library = library
        self.base_url = f"https://{library}.borrowbox.com"
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers=_BROWSER_HEADERS,
            follow_redirects=True,
        )
        self._build_id: str | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> BorrowBoxCatalog:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Build ID discovery (needed for /_next/data/ JSON endpoints)
    # ------------------------------------------------------------------

    def _ensure_build_id(self) -> str:
        """Discover the Next.js buildId from any SSR page, cached for the session."""
        if self._build_id:
            return self._build_id

        # Hit a lightweight page to grab buildId from __NEXT_DATA__
        resp = self._client.get("/")
        resp.raise_for_status()
        next_data = _extract_next_data(resp.text)
        if next_data and "buildId" in next_data:
            self._build_id = next_data["buildId"]
            logger.debug("Discovered buildId: %s", self._build_id)
            return self._build_id

        raise RuntimeError("Could not discover BorrowBox buildId from homepage")

    def _next_data_get(
        self, path: str, params: dict[str, str] | None = None, _depth: int = 0,
    ) -> dict[str, Any]:
        """GET a /_next/data/{buildId}/... JSON endpoint, returning pageProps.

        Handles Next.js JSON-level redirects (``__N_REDIRECT`` in pageProps)
        which occur when a slug in the URL doesn't match the canonical slug.
        """
        if _depth > 3:
            raise RuntimeError(f"Too many Next.js redirects (last: {path})")

        build_id = self._ensure_build_id()
        url = f"/_next/data/{build_id}/{path}"
        resp = self._client.get(
            url,
            params=params,
            headers={"Accept": "*/*", "x-nextjs-data": "1"},
        )
        # A 404 here usually means the cached buildId is stale (BorrowBox
        # redeployed). Refresh it from the homepage and retry once.
        if resp.status_code == 404 and _depth == 0:
            logger.info(
                "Next.js data 404 for %s (buildId=%s); refreshing buildId and retrying",
                path,
                build_id,
            )
            self._build_id = None
            new_build_id = self._ensure_build_id()
            if new_build_id != build_id:
                resp = self._client.get(
                    f"/_next/data/{new_build_id}/{path}",
                    params=params,
                    headers={"Accept": "*/*", "x-nextjs-data": "1"},
                )
        resp.raise_for_status()
        data = resp.json()
        page_props = data.get("pageProps", data)

        # Next.js may return a JSON-level redirect instead of an HTTP 3xx
        redirect = page_props.get("__N_REDIRECT")
        if redirect:
            # Detect 404 redirects — don't follow them
            if "/404" in redirect:
                raise RuntimeError(f"Page not found (redirected to 404): {path}")
            redirect_path = redirect.lstrip("/") + ".json"
            logger.debug("Following Next.js redirect: %s", redirect_path)
            return self._next_data_get(redirect_path, _depth=_depth + 1)

        return page_props

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        format_filter: Format = Format.ALL,
    ) -> list[CatalogItem]:
        """Search the BorrowBox catalog and return matching items."""
        page_props = self._next_data_get("search.json", params={"q": query})
        products = _dig_products_from_page_props(page_props)
        items = [self._product_to_item(p) for p in products]

        if format_filter != Format.ALL:
            items = [
                it for it in items
                if it.format and format_filter.value.lower() in it.format.lower()
            ]

        return items

    # ------------------------------------------------------------------
    # Series listing
    # ------------------------------------------------------------------

    def get_series(self, series_id: str | int, slug: str = "s") -> list[CatalogItem]:
        """Fetch all items in a BorrowBox series."""
        page_props = self._next_data_get(
            f"series/{series_id}/{slug}.json",
            params={"seriesId": str(series_id), "slug": slug},
        )
        products = _dig_products_from_page_props(page_props)
        return [self._product_to_item(p) for p in products]

    # ------------------------------------------------------------------
    # Product detail
    # ------------------------------------------------------------------

    def get_product(self, product_id: str) -> ProductDetail | None:
        """Fetch detailed metadata for a single product."""
        page_props = self._next_data_get(
            f"product/{product_id}/title/p.json",
            params={"productId": product_id, "slug": "p"},
        )
        products = _dig_products_from_page_props(page_props)
        if not products:
            return None

        p = products[0]
        return self._product_to_detail(p)

    # ------------------------------------------------------------------
    # Internal mapping helpers
    # ------------------------------------------------------------------

    def _product_to_item(self, p: dict[str, Any]) -> CatalogItem:
        """Map a raw product JSON dict to a CatalogItem."""
        avail, avail_date = _parse_availability(p.get("availability"))
        loan_format = (p.get("formatSpecifics") or {}).get("loanFormat", "")
        series_num = p.get("seriesNumber")

        # Extract teaser description from supplementaries
        description: str | None = None
        for supp in p.get("supplementaries") or []:
            if supp.get("type") == "Teaser":
                description = supp.get("information")
                break

        return CatalogItem(
            product_id=p.get("productId", ""),
            title=p.get("title", ""),
            authors=[a["name"] for a in (p.get("authors") or []) if a.get("name")],
            narrators=[n["name"] for n in (p.get("narrators") or []) if n.get("name")],
            series_name=p.get("seriesName"),
            series_number=int(series_num) if series_num is not None else None,
            series_id=p.get("seriesId"),
            availability=avail,
            available_date=avail_date,
            format=loan_format,
            isbn=p.get("isbn"),
            description=description,
            cover_url=p.get("coverUrl"),
            product_url=f"{self.base_url}/product/{p.get('productId', '')}/title/p",
        )

    def _product_to_detail(self, p: dict[str, Any]) -> ProductDetail:
        """Map a raw product JSON dict to a ProductDetail."""
        avail, avail_date = _parse_availability(p.get("availability"))
        specs = p.get("formatSpecifics") or {}
        loan_format = specs.get("loanFormat", "")
        series_num = p.get("seriesNumber")

        # Description from supplementaries
        description: str | None = None
        for supp in p.get("supplementaries") or []:
            if supp.get("type") == "Teaser":
                description = supp.get("information")
                break

        # Released date
        released: str | None = None
        ts = p.get("releaseDate")
        if isinstance(ts, (int, float)):
            released = datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%d %B %Y")

        return ProductDetail(
            product_id=p.get("productId", ""),
            title=p.get("title", ""),
            authors=[a["name"] for a in (p.get("authors") or []) if a.get("name")],
            narrators=[n["name"] for n in (p.get("narrators") or []) if n.get("name")],
            description=description,
            isbn=p.get("isbn"),
            duration_seconds=specs.get("totalDuration"),
            format=loan_format,
            file_type=p.get("format"),
            language=(p.get("languages") or [None])[0],
            publisher=None,  # not in list-level JSON
            released=released,
            series_name=p.get("seriesName"),
            series_number=int(series_num) if series_num is not None else None,
            series_id=p.get("seriesId"),
            availability=avail,
            available_date=avail_date,
            cover_url=p.get("coverUrl"),
            product_url=f"{self.base_url}/product/{p.get('productId', '')}/title/p",
        )


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

_NEXT_DATA_RE = re.compile(
    r'<script\s+id="__NEXT_DATA__"\s+type="application/json">(.*?)</script>',
    re.DOTALL,
)


def _extract_next_data(html: str) -> dict[str, Any] | None:
    """Extract the __NEXT_DATA__ JSON blob from an SSR page."""
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        logger.warning("Failed to parse __NEXT_DATA__ JSON")
        return None


def _dig_products_from_page_props(page_props: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk the dehydrated React-Query state in pageProps to find the product list."""
    # Product detail pages put the product directly at pageProps.product
    if "product" in page_props and isinstance(page_props["product"], dict):
        return [page_props["product"]]

    queries = (
        page_props
        .get("dehydratedState", {})
        .get("queries", [])
    )
    for q in queries:
        data = q.get("state", {}).get("data")
        if isinstance(data, dict) and "products" in data:
            return data["products"]
        # Single-product pages sometimes embed the product at data level
        if isinstance(data, dict) and "productId" in data:
            return [data]
    return []


def _parse_availability(
    avail: dict[str, Any] | None,
) -> tuple[Availability, str | None]:
    """Parse the availability dict from BorrowBox product JSON.

    Search/series pages use ``{"status": "ON_LOAN", "nextAvailableDate": ...}``
    while product detail pages use ``{"availabilityStatus": "ON_LOAN", ...}``.
    """
    if avail is None:
        return Availability.UNKNOWN, None

    status = (avail.get("status") or avail.get("availabilityStatus") or "").upper()
    date_str: str | None = None

    ts = avail.get("nextAvailableDate")
    if isinstance(ts, (int, float)):
        date_str = datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%d %B %Y")

    if status == "AVAILABLE":
        return Availability.AVAILABLE, None
    elif status == "ON_LOAN":
        return Availability.ON_LOAN, date_str
    return Availability.UNKNOWN, date_str
