"""Background polling scheduler for NikonUSA Refurbished Tracker."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timedelta, timezone
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
        self.next_poll_at: str | None = None
        self.last_poll_duration_ms: int | None = None
        self.last_error: str | None = None
        self.total_polls: int = 0
        self.last_triggered_alerts: list[AlertEvent] = []
        self.last_changed_skus: list[str] = []

        self._task: asyncio.Task[None] | None = None
        self._wake_event = asyncio.Event()
        self._poll_lock = asyncio.Lock()
        self._sse_subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe_sse(self) -> asyncio.Queue[dict[str, Any]]:
        """Subscribe a client queue to real-time scheduler SSE events."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=32)
        self._sse_subscribers.add(q)
        return q

    def unsubscribe_sse(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove a client SSE queue."""
        self._sse_subscribers.discard(q)

    def broadcast_event(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """Non-blocking broadcast of a real-time event to all connected SSE clients."""
        if not self._sse_subscribers:
            return
        msg = {
            "event": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": payload or {},
        }
        for q in list(self._sse_subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

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
        atl_items = [v for v in in_stock if v.is_all_time_low]

        max_extra_sale_pct = max(
            (v.discount_pct_vs_refurb_reg for v in extra_sale), default=0.0
        )
        max_discount_pct = max(
            (v.discount_pct_vs_msrp for v in in_stock if v.discount_pct_vs_msrp is not None),
            default=0.0,
        )

        return {
            "is_running": self.is_running,
            "is_paused": self.is_paused,
            "is_polling_now": self.is_polling_now,
            "mute_desktop": self.dispatcher.mute_desktop,
            "desktop_sound_muted": self.dispatcher.mute_desktop,
            "poll_interval_seconds": settings.poll_interval_seconds,
            "global_discount_threshold_pct": threshold,
            "last_poll_at": self.last_poll_at,
            "next_poll_at": self.next_poll_at,
            "last_poll_duration_ms": self.last_poll_duration_ms,
            "last_error": self.last_error,
            "total_polls": self.total_polls,
            "total_tracked_skus": len(variants),
            "in_stock_count": len(in_stock),
            "sub_50_msrp_count": len(sub_threshold),
            "extra_sale_count": len(extra_sale),
            "atl_count": len(atl_items),
            "max_extra_sale_pct": round(max_extra_sale_pct, 1),
            "max_discount_pct": round(max_discount_pct, 1),
            "flash_sale_active": len(extra_sale) >= 1 and max_extra_sale_pct >= 5.0,
            "last_changed_skus": self.last_changed_skus,
        }

    async def run_once(self) -> tuple[list[VariantSnapshot], list[AlertEvent]]:
        """Execute a single full poll of NikonUSA refurbished catalog + watchlist."""
        async with self._poll_lock:
            self.is_polling_now = True
            self.broadcast_event("poll_started", {"is_polling_now": True})
            t0 = time.perf_counter()
            triggered_alerts: list[AlertEvent] = []
            updated_snapshots: list[VariantSnapshot] = []
            changed_skus: list[str] = []

            try:
                settings = self.db.get_settings()
                watchlist = self.db.get_watchlist()
                watchlist_by_sku = {w.sku.upper(): w for w in watchlist}

                # 1. Fetch main in-stock refurbished catalog
                catalog_snaps = await self.scraper.fetch_refurbished_catalog()
                catalog_skus = {s.sku.upper() for s in catalog_snaps}

                # 2. Fetch any out-of-stock / direct watchlisted product pages concurrently
                watchlist_snaps = await self.scraper.fetch_watchlist_items(
                    watchlist, catalog_skus
                )

                all_snaps = catalog_snaps + watchlist_snaps
                seen_skus = {s.sku for s in all_snaps}

                # 3. Batch upsert into SQLite inside a single transaction and evaluate alert rules
                upserted_pairs = self.db.upsert_variants_batch(all_snaps)
                pending_alerts: list[AlertEvent] = []
                for prev, curr in upserted_pairs:
                    updated_snapshots.append(curr)
                    if prev is not None and (
                        prev.refurb_sale_cents != curr.refurb_sale_cents
                        or prev.is_on_stock != curr.is_on_stock
                        or prev.available_quantity != curr.available_quantity
                    ):
                        changed_skus.append(curr.sku)

                    events = evaluate_variant_alerts(
                        previous=prev,
                        current=curr,
                        settings=settings,
                        watchlist_by_sku=watchlist_by_sku,
                        db=self.db,
                    )
                    for idx, ev in enumerate(events):
                        if idx > 0:
                            ev.auto_open_browser = False
                        pending_alerts.append(ev)

                if pending_alerts:
                    triggered_alerts = await self.dispatcher.dispatch_batch(
                        pending_alerts, settings
                    )

                # 4. Mark disappeared items as out of stock
                self.db.mark_missing_catalog_items_out_of_stock(seen_skus)

                self.last_poll_at = datetime.now(timezone.utc).isoformat()
                self.last_poll_duration_ms = int((time.perf_counter() - t0) * 1000)
                self.last_error = None
                self.total_polls += 1
                self.last_triggered_alerts = triggered_alerts
                self.last_changed_skus = changed_skus
                return updated_snapshots, triggered_alerts

            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("Error during NikonUSA poll cycle: %s", exc)
                raise
            finally:
                self.is_polling_now = False
                done_status = self.get_status()
                self.broadcast_event(
                    "poll_completed",
                    {
                        "status": done_status,
                        "changed_skus": self.last_changed_skus,
                        "triggered_alerts": [a.model_dump() for a in self.last_triggered_alerts],
                        **done_status,
                    },
                )

    def start(self) -> None:
        """Start the background polling loop task inside the running asyncio event loop."""
        if self._task is not None and not self._task.done():
            return
        self.is_running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the background polling loop and close persistent HTTP connections."""
        self.is_running = False
        self._wake_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.scraper.aclose()

    def pause(self) -> None:
        self.is_paused = True
        self.next_poll_at = None
        self.broadcast_event("status_updated", self.get_status())

    def resume(self) -> None:
        self.is_paused = False
        self._wake_event.set()
        self.broadcast_event("status_updated", self.get_status())

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
                sleep_sec = float(backoff)
            else:
                # Add polite +-10% jitter
                jitter = random.uniform(-0.1 * base_interval, 0.1 * base_interval)
                sleep_sec = max(10.0, base_interval + jitter)

            if not self.is_paused:
                self.next_poll_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=sleep_sec)
                ).isoformat()
            else:
                self.next_poll_at = None

            self._wake_event.clear()
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=sleep_sec)
            except asyncio.TimeoutError:
                pass
