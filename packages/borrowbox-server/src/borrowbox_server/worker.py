"""Background worker that periodically refreshes availability data."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict

from borrowbox import BorrowBoxCatalog
from borrowbox_server.config import Settings
from borrowbox_server.store import Store

logger = logging.getLogger(__name__)


class AvailabilityRefresher:
    """Periodically re-fetches series data to update availability in Redis."""

    def __init__(self, store: Store, settings: Settings) -> None:
        self._store = store
        self._settings = settings

    async def run(self) -> None:
        """Run the refresh loop forever (designed to be run as an asyncio task)."""
        interval = self._settings.refresh_interval
        logger.info("Availability refresher started (interval=%ds)", interval)

        while True:
            try:
                await self._refresh_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error during availability refresh")

            await asyncio.sleep(interval)

    async def _refresh_all(self) -> None:
        """Refresh availability for all tracked series across all libraries."""
        # Snapshot: the registry is mutable at runtime (libraries are added and
        # removed from the UI), and this loop awaits between iterations, so
        # iterating the live dict could raise "changed size during iteration".
        for lib_name, subdomain in list(self._settings.libraries.items()):
            series_ids = await self._store.get_all_series_ids(lib_name)
            if not series_ids:
                continue

            logger.info(
                "Refreshing %d series for library '%s'",
                len(series_ids),
                lib_name,
            )

            for series_id in series_ids:
                try:
                    await self._refresh_series(lib_name, subdomain, series_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "Failed to refresh series %s in %s",
                        series_id,
                        lib_name,
                        exc_info=True,
                    )
                # Be polite to BorrowBox
                await asyncio.sleep(1)

    async def _refresh_series(
        self, lib_name: str, subdomain: str, series_id: str
    ) -> None:
        """Fetch fresh series data and update availability in Redis."""

        def _fetch():
            with BorrowBoxCatalog(subdomain) as cat:
                return cat.get_series(series_id)

        items = await asyncio.to_thread(_fetch)
        if not items:
            return

        # Update series entry
        series_name = items[0].series_name or "Unknown"
        product_ids = [it.product_id for it in items]
        await self._store.set_series(lib_name, series_id, series_name, product_ids)

        # Update product cache
        await self._store.set_products_bulk(lib_name, [asdict(it) for it in items])

        # Update availability and notify on newly-available items
        avail_items = []
        for it in items:
            status = it.availability.value if it.availability else "unknown"
            avail_items.append({
                "product_id": it.product_id,
                "status": status,
                "available_date": it.available_date,
            })

            # Check if this was previously unavailable and is now available
            if status == "available":
                prev = await self._store.get_availability(lib_name, it.product_id)
                if prev and prev.get("status") != "available":
                    logger.info(
                        "NOW AVAILABLE: '%s' in %s", it.title, lib_name
                    )
                    await self._store.publish_available(
                        lib_name, it.product_id, it.title
                    )

        await self._store.set_availability_bulk(lib_name, avail_items)

        logger.debug(
            "Refreshed series '%s' (%d items) in %s",
            series_name,
            len(items),
            lib_name,
        )
