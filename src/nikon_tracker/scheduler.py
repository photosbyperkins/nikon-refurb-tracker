"""Background polling scheduler for NikonUSA Refurbished Tracker."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any

from nikon_tracker.database import TrackerDatabase
from nikon_tracker.models import AlertEvent, VariantSnapshot
from nikon_tracker.notifier import AlertDispatcher, evaluate_variant_alerts
from nikon_tracker.scraper import NikonUSAClient

logger = logging.getLogger(__name__)


class TrackerScheduler:
    """Manages periodic background polling of NikonUSA refurbished catalog and watchlist."""

    def __init__(
        self,
        db: TrackerDatabase | None = None,
        scraper: NikonUSAClient | None = None,
        dispatcher: AlertDispatcher | None = None,
    ) -> None:
        self.db = db or TrackerDatabase()
        self.scraper = scraper or NikonUSAClient(db=self.db)
        self.dispatcher = dispatcher or AlertDispatcher(db=self.db)

        self.is_running: bool = False
        self.is_paused: bool = False
        self.is_polling_now: bool = False
        self.last_poll_at: str | None = None
        self.last_poll_duration_ms: int | None = None
        self.last_error: str | None = None
        self.total_polls: int = 0
        self.last_triggered_alerts: list[AlertEvent] = []

        self._task: asyncio.Task[None] | None = None
        self._wake_event = asyncio.Event()
        self._poll_lock = asyncio.Lock()

    def get_status(self) -> dict[str, Any]:
        variants = self.db.get_all_variants()
        settings = self.db.get_settings()
        threshold = settings.global_discount_threshold_pct
        in_stock = [v for v in variants if v.is_on_stock]
        sub_threshold = [
            v
            for v in in_stock
            if v.discount_pct_vs_msrp is not None and v.discount_pct_vs_msrp >= threshold
        ]
        extra_sale = [v for v in in_stock if v.is_extra_refurb_sale]

        return {
            "is_running": self.is_running,
            "is_paused": self.is_paused,
            "is_polling_now": self.is_polling_now,
            "mute_desktop": self.dispatcher.mute_desktop,
            "poll_interval_seconds": settings.poll_interval_seconds,
            "global_discount_threshold_pct": threshold,
            "last_poll_at": self.last_poll_at,
            "last_poll_duration_ms": self.last_poll_duration_ms,
            "last_error": self.last_error,
            "total_polls": self.total_polls,
            "total_tracked_skus": len(variants),
            "in_stock_count": len(in_stock),
            "sub_50_msrp_count": len(sub_threshold),
            "extra_sale_count": len(extra_sale),
        }

    async def run_once(self) -> tuple[list[VariantSnapshot], list[AlertEvent]]:
        """Execute a single full poll of NikonUSA refurbished catalog + watchlist."""
        async with self._poll_lock:
            self.is_polling_now = True
            t0 = time.perf_counter()
            triggered_alerts: list[AlertEvent] = []
            updated_snapshots: list[VariantSnapshot] = []

            try:
                settings = self.db.get_settings()
                watchlist = self.db.get_watchlist()
                watchlist_by_sku = {w.sku.upper(): w for w in watchlist}

                # 1. Fetch main in-stock refurbished catalog
                catalog_snaps = await self.scraper.fetch_refurbished_catalog()
                catalog_skus = {s.sku.upper() for s in catalog_snaps}

                # 2. Fetch any out-of-stock / direct watchlisted product pages
                watchlist_snaps = await self.scraper.fetch_watchlist_items(
                    watchlist, catalog_skus
                )

                all_snaps = catalog_snaps + watchlist_snaps
                seen_skus = {s.sku for s in all_snaps}

                # 3. Upsert into SQLite and evaluate alert rules
                for snap in all_snaps:
                    prev, curr = self.db.upsert_variant(snap)
                    updated_snapshots.append(curr)
                    events = evaluate_variant_alerts(
                        previous=prev,
                        current=curr,
                        settings=settings,
                        watchlist_by_sku=watchlist_by_sku,
                        db=self.db,
                    )
                    for ev in events:
                        sent_ev = await self.dispatcher.dispatch(ev, settings)
                        triggered_alerts.append(sent_ev)

                # 4. Mark disappeared items as out of stock
                self.db.mark_missing_catalog_items_out_of_stock(seen_skus)

                self.last_poll_at = datetime.now(timezone.utc).isoformat()
                self.last_poll_duration_ms = int((time.perf_counter() - t0) * 1000)
                self.last_error = None
                self.total_polls += 1
                self.last_triggered_alerts = triggered_alerts
                return updated_snapshots, triggered_alerts

            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("Error during NikonUSA poll cycle: %s", exc)
                raise
            finally:
                self.is_polling_now = False

    def start(self) -> None:
        """Start the background polling loop task inside the running asyncio event loop."""
        if self._task is not None and not self._task.done():
            return
        self.is_running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the background polling loop."""
        self.is_running = False
        self._wake_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def pause(self) -> None:
        self.is_paused = True

    def resume(self) -> None:
        self.is_paused = False
        self._wake_event.set()

    def trigger_now(self) -> None:
        """Wake the background loop immediately to run a poll."""
        self._wake_event.set()

    async def _loop(self) -> None:
        consecutive_errors = 0
        while self.is_running:
            if not self.is_paused:
                try:
                    await self.run_once()
                    consecutive_errors = 0
                except Exception:
                    consecutive_errors += 1

            settings = self.db.get_settings()
            base_interval = max(15, settings.poll_interval_seconds)
            if consecutive_errors > 0:
                backoff = min(300, base_interval * (2 ** min(consecutive_errors, 4)))
                sleep_sec = backoff
            else:
                # Add polite +-10% jitter
                jitter = random.uniform(-0.1 * base_interval, 0.1 * base_interval)
                sleep_sec = max(10.0, base_interval + jitter)

            self._wake_event.clear()
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=sleep_sec)
            except asyncio.TimeoutError:
                pass
