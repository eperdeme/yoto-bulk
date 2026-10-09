"""Authenticated BorrowBox client — login, loans, reservations, and downloads.

Extends :class:`BorrowBoxCatalog` with authenticated operations by managing
a session that carries the XSRF token and siteId required by the
``/api/v1/`` endpoints.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from borrowbox.catalog import BorrowBoxCatalog, _extract_next_data

logger = logging.getLogger(__name__)


def _normalize_download_url(raw_url: str, *, base_url: str | None = None) -> str:
    """Normalize BorrowBox/MDP download URLs into absolute HTTPS URLs."""
    candidate = (raw_url or "").strip()
    if not candidate:
        return ""

    if candidate.startswith("//"):
        return f"https:{candidate}"

    parsed = urlparse(candidate)
    if parsed.scheme and parsed.netloc:
        return candidate

    if candidate.startswith("/") and base_url:
        base = urlparse(base_url)
        origin = f"{base.scheme}://{base.netloc}" if base.scheme and base.netloc else base_url.rstrip("/")
        return f"{origin}{candidate}"

    if "://" not in candidate and "." in candidate.split("/")[0]:
        return f"https://{candidate}"

    return candidate


def _extract_mdp_next_url(response: httpx.Response) -> str:
    """Extract the next URL from an MDP response, if present."""
    location = _normalize_download_url(
        response.headers.get("Location", ""),
        base_url=str(response.request.url),
    )
    if location:
        return location

    try:
        data = response.json()
    except ValueError:
        data = {}

    if isinstance(data, dict):
        for key in ("url", "progressUrl", "progressURL"):
            candidate = _normalize_download_url(
                str(data.get(key) or ""),
                base_url=str(response.request.url),
            )
            if candidate:
                return candidate

    return ""


class BorrowBoxClient(BorrowBoxCatalog):
    """Authenticated BorrowBox client.

    Inherits all unauthenticated catalog operations (search, series, product)
    and adds login, loan management, and reservation capabilities.

    Usage::

        with BorrowBoxClient("hounslow") as client:
            client.login("user@example.com", "password")
            loans = client.get_current_loans()
            client.borrow("BOL_12345")
    """

    def __init__(self, library: str, timeout: float = 30.0) -> None:
        super().__init__(library, timeout)
        self._site_id: str | None = None
        self._patron_id: str | None = None
        self._logged_in = False
        self._direct_api_ok: bool | None = None  # None = unknown, try it

    # ------------------------------------------------------------------
    # Session export / restore (for Redis persistence)
    # ------------------------------------------------------------------

    def export_session(self) -> dict[str, Any]:
        """Export session state as a JSON-serializable dict for caching."""
        cookies = {c.name: c.value for c in self._client.cookies.jar}
        return {
            "cookies": cookies,
            "site_id": self._site_id,
            "patron_id": self._patron_id,
            "build_id": self._build_id,
            "logged_in": self._logged_in,
            "direct_api_ok": self._direct_api_ok,
        }

    def restore_session(self, session: dict[str, Any]) -> bool:
        """Restore session state from a cached dict.

        Returns True if the session was restored, False if it was invalid.
        """
        cookies = session.get("cookies", {})
        if not cookies or "XSRF-TOKEN" not in cookies:
            return False

        for name, value in cookies.items():
            self._client.cookies.set(name, value, domain=f"{self.library}.borrowbox.com")
        self._site_id = session.get("site_id")
        self._patron_id = session.get("patron_id")
        self._build_id = session.get("build_id")
        self._logged_in = session.get("logged_in", False)
        self._direct_api_ok = session.get("direct_api_ok")
        logger.debug("Restored cached session for %s", self.library)
        return self._logged_in

    # ------------------------------------------------------------------
    # Session bootstrap
    # ------------------------------------------------------------------

    def _ensure_site_id(self) -> str:
        """Discover siteId from __NEXT_DATA__, cached for the session."""
        if self._site_id:
            return self._site_id

        resp = self._client.get(
            "/login",
            headers={
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "none",
                "sec-fetch-user": "?1",
            },
        )
        resp.raise_for_status()

        next_data = _extract_next_data(resp.text)
        if not next_data:
            raise RuntimeError("Could not extract __NEXT_DATA__ from login page")

        site_context = next_data.get("props", {}).get("siteContext", {})
        self._site_id = site_context.get("siteId")
        if not self._site_id:
            raise RuntimeError("Could not find siteId in __NEXT_DATA__")

        # Also grab buildId while we're here
        if not self._build_id and "buildId" in next_data:
            self._build_id = next_data["buildId"]

        logger.debug("Discovered siteId: %s", self._site_id)
        return self._site_id

    def _auth_headers(self) -> dict[str, str]:
        """Return headers required for authenticated /api/v1/ requests."""
        xsrf = self._client.cookies.get("XSRF-TOKEN")
        if not xsrf:
            raise RuntimeError("No XSRF-TOKEN cookie — call login() first")
        headers = {
            "app-name": "ppui",
            "app-siteid": self._ensure_site_id(),
            "X-XSRF-TOKEN": xsrf,
            "origin": self.base_url,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._patron_id:
            headers["app-patronid"] = self._patron_id
        return headers

    def _discover_patron_id(self) -> str | None:
        """Extract patronId from the my-loans SSR page dehydrated state."""
        if self._patron_id:
            return self._patron_id

        try:
            page_props = self._next_data_get("home/my-loans.json")
            queries = (
                page_props
                .get("dehydratedState", {})
                .get("queries", [])
            )
            for q in queries:
                for item in q.get("queryKey", []):
                    if isinstance(item, dict) and "userId" in item:
                        self._patron_id = str(item["userId"])
                        logger.debug("Discovered patronId: %s", self._patron_id)
                        return self._patron_id
        except Exception:
            logger.debug("Could not discover patronId from SSR page")

        return None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _login_headers(self) -> dict[str, str]:
        """Auth headers enriched with sec-fetch-* for the login POST."""
        headers = self._auth_headers()
        # sec-fetch-* headers tell WAF this is a same-origin XHR, not a bot
        headers.update({
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "Referer": f"{self.base_url}/login",
        })
        return headers

    def login(self, username: str, password: str) -> dict[str, Any]:
        """Authenticate with BorrowBox.

        Returns the login response data on success.
        Raises ``httpx.HTTPStatusError`` on auth failure (401).
        """
        # Hit login page to get XSRF token and siteId
        self._ensure_site_id()

        resp = self._client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password},
            headers=self._login_headers(),
        )

        # AWS WAF may return 202 with x-amzn-waf-action: challenge.
        # Retry with progressive backoff — the WAF token cookie set on the
        # first response is carried automatically by httpx on subsequent
        # requests, which is often enough to satisfy the challenge.
        _WAF_BACKOFF = [3, 5, 8]
        for delay in _WAF_BACKOFF:
            if resp.status_code != 202:
                break
            waf_cookie = self._client.cookies.get("aws-waf-token")
            logger.warning(
                "WAF challenge on login (202) — retrying in %ds%s",
                delay,
                " (with aws-waf-token)" if waf_cookie else "",
            )
            time.sleep(delay)
            resp = self._client.post(
                "/api/v1/auth/login",
                json={"username": username, "password": password},
                headers=self._login_headers(),
            )

        if resp.status_code == 202:
            # Still challenged — login may work for SSR pages via cookies
            logger.warning("Login returned 202 after retries — API calls may be limited")
            self._logged_in = True
            self._discover_patron_id()
            return {}

        resp.raise_for_status()
        self._logged_in = True
        self._discover_patron_id()
        logger.info("Logged in as %s", username)
        return resp.json() if resp.text else {}

    def logout(self) -> None:
        """End the authenticated session."""
        if self._logged_in:
            try:
                self._client.post(
                    "/api/v1/auth/logout",
                    headers=self._auth_headers(),
                )
            except httpx.HTTPError:
                logger.debug("Logout request failed (session may already be expired)")
            self._logged_in = False

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in

    # ------------------------------------------------------------------
    # Current loans
    # ------------------------------------------------------------------

    def _fetch_my_loans_page(self) -> dict[str, Any]:
        """Fetch the my-loans SSR page and return the dehydrated queries."""
        self._require_login()
        page_props = self._next_data_get("home/my-loans.json")
        return page_props

    def get_current_loans(self, format_filter: str = "all") -> list[dict[str, Any]]:
        """Fetch the user's current active loans and reserves."""
        self._require_login()

        # Try the direct API first — it reliably returns both loans and reserves
        # Skip if we already know the direct API doesn't work for this session
        if self._direct_api_ok is not False:
            try:
                h = self._auth_headers()
                h.update({"sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin"})
                resp = self._client.get(
                    "/api/v1/loans",
                    headers=h,
                )
                if resp.status_code == 200:
                    self._direct_api_ok = True
                    data = resp.json()
                    loans = data.get("loans", [])
                    if format_filter != "all":
                        loans = [
                            ln for ln in loans
                            if (ln.get("product", {}).get("formatSpecifics", {}).get("loanFormat", ""))
                            .lower() == format_filter.lower()
                        ]
                    return loans
                self._direct_api_ok = False
                logger.info(
                    "Direct /api/v1/loans returned %s, falling back to SSR", resp.status_code
                )
            except Exception:
                self._direct_api_ok = False
                logger.info("Direct /api/v1/loans failed, falling back to SSR page")

        # Fallback: parse from SSR page (works even when WAF blocks direct API)
        page_props = self._fetch_my_loans_page()

        loans: list[dict[str, Any]] = []
        queries = (
            page_props
            .get("dehydratedState", {})
            .get("queries", [])
        )
        for q in queries:
            qk = q.get("queryKey", [])
            if not qk or qk[0] not in ("activeLoansAndReserves", "currentLoans", "loans"):
                continue
            data = q.get("state", {}).get("data")
            if isinstance(data, dict) and "loans" in data:
                loans.extend(data["loans"])
            elif isinstance(data, dict) and "products" in data:
                loans.extend(data["products"])

        if format_filter != "all":
            loans = [
                ln for ln in loans
                if (ln.get("product", {}).get("formatSpecifics", {}).get("loanFormat", ""))
                .lower() == format_filter.lower()
            ]

        return loans

    # ------------------------------------------------------------------
    # Loan history
    # ------------------------------------------------------------------

    def get_loan_history(
        self,
        loan_format: str = "eAudiobooks",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch historical loans.

        First tries the direct API endpoint. If that returns 401 (e.g. due
        to WAF challenges), falls back to the SSR my-loans page which embeds
        the most recent history in its dehydrated state.
        """
        self._require_login()

        # Try direct API first
        try:
            h = self._auth_headers()
            h.update({"sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin"})
            resp = self._client.post(
                "/api/v1/loans/historical/products",
                params={"loanFormat": loan_format, "limit": str(limit)},
                headers=h,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("loans", [])
        except Exception:
            pass

        # Fallback: extract from SSR page
        logger.info("Falling back to SSR page for loan history")
        page_props = self._fetch_my_loans_page()
        queries = (
            page_props
            .get("dehydratedState", {})
            .get("queries", [])
        )

        loans: list[dict[str, Any]] = []
        for q in queries:
            qk = q.get("queryKey", [])
            if not qk or qk[0] != "loanHistory":
                continue
            # Match format if specified in queryKey params
            if len(qk) > 1 and isinstance(qk[1], dict):
                fmt = qk[1].get("loanFormat", "")
                if loan_format != "all" and fmt.lower() != loan_format.lower():
                    continue
            data = q.get("state", {}).get("data")
            if isinstance(data, dict) and "loans" in data:
                loans.extend(data["loans"])

        return loans[:limit]

    # ------------------------------------------------------------------
    # Credits
    # ------------------------------------------------------------------

    def _extract_credits_from_ssr(self, page_props: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Try to extract credits from an SSR page's dehydrated state."""
        queries = (
            page_props
            .get("dehydratedState", {})
            .get("queries", [])
        )
        for q in queries:
            qk = q.get("queryKey", [])
            data = q.get("state", {}).get("data")
            if not isinstance(data, dict):
                continue
            # Credits appear under queryKey ["patronInfo", ...] or ["credits", ...]
            if qk and qk[0] in ("patronInfo", "credits"):
                if "credits" in data and isinstance(data["credits"], list):
                    return data["credits"]
            # Also check any query that happens to have a credits list
            if "credits" in data and isinstance(data["credits"], list):
                return data["credits"]
        return None

    def get_credits(self) -> list[dict[str, Any]]:
        """Fetch remaining borrow credits.

        Returns a list of dicts with keys: loanFormat, amount, granted.
        """
        self._require_login()

        # Try direct API first
        try:
            h = self._auth_headers()
            h.update({"sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin"})
            resp = self._client.get(
                "/api/v1/credits",
                headers=h,
            )
            if resp.status_code == 200:
                data = resp.json() if resp.text else {}
                return data.get("credits", [])
            logger.info(
                "Direct /api/v1/credits returned %s, falling back to SSR", resp.status_code
            )
        except Exception:
            logger.info("Direct /api/v1/credits failed, falling back to SSR")

        # Fallback: try several SSR pages that may embed credits
        for ssr_page in ("home/my-loans.json", "home.json"):
            try:
                page_props = self._next_data_get(ssr_page)
                credits = self._extract_credits_from_ssr(page_props)
                if credits is not None:
                    logger.info("Found credits from SSR page %s", ssr_page)
                    return credits
            except Exception:
                logger.debug("SSR page %s unavailable for credits", ssr_page)

        logger.warning("Could not retrieve credits from API or SSR")
        return []

    # ------------------------------------------------------------------
    # Borrow / Reserve
    # ------------------------------------------------------------------

    def _api_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Make an authenticated API request, raising a clear error on WAF 401."""
        headers = self._auth_headers()
        headers.update({
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "Referer": f"{self.base_url}/home/my-loans",
        })
        resp = self._client.request(
            method,
            path,
            params=params,
            headers=headers,
        )
        if resp.status_code == 401:
            raise RuntimeError(
                f"API returned 401 for {method} {path} — "
                "this usually means the login was WAF-challenged (202). "
                "Try again later, or use the BorrowBox app/website directly."
            )
        if not resp.is_success:
            logger.debug(
                "API request failed: %s %s (status=%d, body=%r)",
                method, path, resp.status_code, resp.text[:500]
            )

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Try to extract a more useful error message from the body
            try:
                data = resp.json()
                if isinstance(data, dict) and "message" in data:
                    raise RuntimeError(f"BorrowBox API error: {data['message']}") from exc
                if isinstance(data, dict) and "detail" in data:
                    raise RuntimeError(f"BorrowBox API error: {data['detail']}") from exc
            except (ValueError, KeyError):
                pass
            raise
        return resp.json() if resp.text else {}

    def borrow(self, product_id: str, loan_type: str = "loan") -> dict[str, Any]:
        """Borrow an available item.

        Uses the ``/api/v1/loans/checkout`` endpoint with query parameters.

        Returns the API response dict (id, productId, startDate, endDate, etc.).
        Raises ``RuntimeError`` if WAF-blocked, ``httpx.HTTPStatusError`` otherwise.
        """
        self._require_login()
        return self._api_request(
            "PUT",
            "/api/v1/loans/checkout",
            params={"productId": product_id, "loanType": loan_type},
        )

    def reserve(self, product_id: str) -> dict[str, Any]:
        """Place a reservation (hold) on an unavailable item.

        Returns the API response dict.
        """
        self._require_login()
        return self._api_request(
            "PUT",
            "/api/v1/loans/checkout",
            params={"productId": product_id, "loanType": "reserve"},
        )

    def cancel_reservation(self, product_id: str) -> dict[str, Any]:
        """Cancel an existing reservation."""
        self._require_login()
        return self._api_request("DELETE", f"/api/v1/reservations/{product_id}")

    def return_loan(self, loan_id: str) -> dict[str, Any]:
        """Return a borrowed item early."""
        self._require_login()
        return self._api_request("DELETE", f"/api/v1/loans/{loan_id}")

    # ------------------------------------------------------------------
    # Reading lists
    # ------------------------------------------------------------------

    def get_reading_lists(self) -> list[dict[str, Any]]:
        """Fetch the user's reading lists (wishlists)."""
        self._require_login()
        page_props = self._next_data_get("home/my-reading-lists.json")
        queries = (
            page_props
            .get("dehydratedState", {})
            .get("queries", [])
        )
        for q in queries:
            data = q.get("state", {}).get("data")
            if isinstance(data, dict) and "products" in data:
                return data["products"]
        return []

    def add_to_reading_list(self, product_id: str) -> dict[str, Any]:
        """Add a product to the user's reading list."""
        self._require_login()
        return self._api_request("POST", f"/api/v1/reading-lists/{product_id}")

    def remove_from_reading_list(self, product_id: str) -> dict[str, Any]:
        """Remove a product from the user's reading list."""
        self._require_login()
        return self._api_request("DELETE", f"/api/v1/reading-lists/{product_id}")

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def _get_download_info(self, loan_id: str) -> dict[str, Any]:
        """Fetch download info for a loan from the SSR access page.

        Returns a dict with keys like ``completePartRange``, ``parts``, etc.
        The ``completePartRange`` (e.g. ``"1-7"``) tells us how many tracks
        the audiobook has.
        """
        try:
            page_props = self._next_data_get(
                f"home/my-loans/access/{loan_id}.json",
                params={"id": loan_id},
            )
            access_info = page_props.get("accessInfo", {})
            return access_info.get("downloadInfo", {})
        except Exception:
            logger.warning("Could not fetch download info from SSR for loan %s", loan_id, exc_info=True)
            return {}

    def _get_total_tracks_from_loans(self, loan_id: str) -> int | None:
        """Fallback lookup for total tracks when SSR access info is unavailable."""
        try:
            for loan in self.get_current_loans():
                if str(loan.get("id") or "") != str(loan_id):
                    continue
                product = loan.get("product", {}) or {}
                specifics = product.get("formatSpecifics") or {}
                total_tracks = specifics.get("totalTrackNumber") or specifics.get("totalTracks")
                if total_tracks:
                    return int(total_tracks)
        except Exception:
            logger.warning("Could not infer total tracks from loans for %s", loan_id, exc_info=True)
        return None

    def get_download_url(self, loan_id: str, total_tracks: int | None = None) -> str:
        """Resolve a signed download URL for an audiobook loan.

        The BorrowBox download flow is:
          1. Discover the track range from the SSR access page
             (``completePartRange``, e.g. ``"1-7"``).
          2. ``/api/v1/audiobooks/{loanId}/parts/{range}/urls`` → ``downloadUrl``
          3. GET ``downloadUrl`` (Bolinda MDP) → JSON ``{"url": "..."}``
          4. The final URL points to a signed CloudFront zip on
             ``download-eu.borrowbox.com``.

        Returns the final signed download URL.
        Raises ``RuntimeError`` if WAF-blocked or download URLs unavailable.
        """
        self._require_login()

        # Discover the complete part range from the access page
        track_range = None
        if not total_tracks:
            dl_info = self._get_download_info(loan_id)
            track_range = dl_info.get("completePartRange")
            if not track_range:
                # Fallback: check product totalTrackNumber
                product = dl_info.get("product", {})
                total_n = product.get("totalTrackNumber")
                if total_n:
                    track_range = f"1-{total_n}"

        if not track_range:
            fallback_total_tracks = total_tracks or self._get_total_tracks_from_loans(loan_id)
            track_range = f"1-{fallback_total_tracks or 1}"

        logger.info("Downloading loan %s with track range %s", loan_id, track_range)

        def _fetch_mdp_url() -> str:
            data = self._api_request(
                "GET", f"/api/v1/audiobooks/{loan_id}/parts/{track_range}/urls"
            )
            mdp_url = data.get("downloadUrl")
            if not mdp_url:
                raise RuntimeError(f"No downloadUrl in response for loan {loan_id}")
            return str(mdp_url)

        # Step 2: resolve the MDP redirect/progress flow to get the signed CloudFront URL.
        #
        # Important: if the downstream MDP service returns 429, stop immediately.
        # Repeatedly retrying the same loan download just burns through the provider's
        # rate limit window and makes the user wait longer for the same failure.
        # In practice the useful action is to surface a clear "try again later" error
        # and let the user retry once the provider-side cooldown has passed.
        current_url = _fetch_mdp_url()
        polling_progress = False
        for attempt in range(10):
            mdp_resp = self._client.get(current_url)
            if mdp_resp.status_code == 429:
                raise RuntimeError(
                    "BorrowBox download provider rate-limited this title. "
                    "The file was requested too often; please try again later."
                )

            mdp_resp.raise_for_status()

            next_url = _extract_mdp_next_url(mdp_resp)
            if next_url:
                parsed = urlparse(next_url)
                current_host = urlparse(str(mdp_resp.request.url)).netloc
                if parsed.netloc == current_host and "/progress" in parsed.path:
                    current_url = next_url
                    polling_progress = True
                    time.sleep(1)
                    continue
                return next_url

            # The /progress endpoint signals "ready" with a 200 that often does
            # not include the final signed URL in its body. In that case, re-issue
            # the original tracks request to obtain the now-ready CloudFront URL.
            # While progress is still 202 (preparing), keep polling the same URL.
            if polling_progress:
                if mdp_resp.status_code == 202:
                    time.sleep(1)
                    continue
                if mdp_resp.status_code == 200:
                    current_url = _fetch_mdp_url()
                    polling_progress = False
                    time.sleep(1)
                    continue

            body_preview = mdp_resp.text[:200] if mdp_resp.text else ""
            raise RuntimeError(
                f"No usable url in MDP response for loan {loan_id} "
                f"(status={mdp_resp.status_code}, body={body_preview!r})"
            )

        raise RuntimeError(f"MDP did not produce a final download URL for loan {loan_id}")

    def get_loan_status(self) -> dict[str, dict[str, Any]]:
        """Fetch loan status for all known products.

        Returns a dict mapping productId to status info (active, reserved, etc.).
        """
        self._require_login()
        try:
            data = self._api_request("GET", "/api/v1/loan-status")
            return data.get("loans", {})
        except RuntimeError:
            # WAF-blocked — fall back to SSR-based loan data
            logger.info("loan-status API blocked, falling back to SSR")
            loans = self.get_current_loans()
            return {
                ln.get("product", {}).get("productId", ""): {
                    "active": ln.get("loanType") == "loan",
                    "reserved": ln.get("loanType") == "reserve",
                }
                for ln in loans
                if ln.get("product", {}).get("productId")
            }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _require_login(self) -> None:
        if not self._logged_in:
            raise RuntimeError("Not logged in — call login() first")
