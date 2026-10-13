"""FastAPI web server and REST API for NikonUSA Refurbished Tracker."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nikon_tracker.database import TrackerDatabase
from nikon_tracker.models import (
    POPULAR_NIKON_PRESETS,
    AlertEvent,
    TrackerSettings,
    VariantSnapshot,
    WatchlistEntry,
)
from nikon_tracker.scheduler import TrackerScheduler
from nikon_tracker.scraper import parse_nikon_url

STATIC_DIR = Path(__file__).parent / "static"


class ManualMsrpRequest(BaseModel):
    msrp_dollars: float


class WatchlistCreateRequest(BaseModel):
    url_or_sku: str = ""
    sku: str = ""
    slug: str = ""
    product_key: str = ""
    label: str = ""
    max_price_dollars: float | None = None
    min_discount_pct_msrp: float | None = 50.0
    notify_on_restock: bool = True
    auto_open_browser: bool = False
    enabled: bool = True


def create_app(
    db: TrackerDatabase | None = None,
    scheduler: TrackerScheduler | None = None,
    start_background_poller: bool = True,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    tracker_db = db or TrackerDatabase()
    tracker_scheduler = scheduler or TrackerScheduler(db=tracker_db)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if start_background_poller:
            tracker_scheduler.start()
        try:
            yield
        finally:
            if start_background_poller:
                await tracker_scheduler.stop()

    app = FastAPI(
        title="NikonUSA Refurbished Tracker",
        version="0.1.0",
        description="Fast alert tracker for NikonUSA refurbished cameras & lenses (<50% MSRP deals & restocks).",
        lifespan=lifespan,
    )

    app.state.db = tracker_db
    app.state.scheduler = tracker_scheduler

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "index.html"))

    @app.get("/api/status")
    async def get_status() -> dict[str, Any]:
        return tracker_scheduler.get_status()

    @app.get("/api/deals")
    async def get_deals(
        sub_50_only: bool = Query(default=False),
        in_stock_only: bool = Query(default=False),
        extra_sale_only: bool = Query(default=False),
        category: str | None = Query(default=None),
        q: str | None = Query(default=None),
        sort_by: str = Query(default="discount_desc"),
    ) -> dict[str, Any]:
        variants = tracker_db.get_all_variants()
        settings = tracker_db.get_settings()
        threshold = settings.global_discount_threshold_pct

        filtered: list[VariantSnapshot] = []
        for v in variants:
            if in_stock_only and not v.is_on_stock:
                continue
            if sub_50_only:
                if v.discount_pct_vs_msrp is None or v.discount_pct_vs_msrp < threshold:
                    continue
            if extra_sale_only and not v.is_extra_refurb_sale:
                continue
            if category and category != "ALL" and v.category != category:
                continue
            if q:
                needle = q.strip().lower()
                haystack = f"{v.name} {v.sku} {v.new_sku} {v.category} {v.promo_message}".lower()
                if needle not in haystack:
                    continue
            filtered.append(v)

        if sort_by == "discount_desc":
            filtered.sort(
                key=lambda x: (
                    1 if x.is_on_stock else 0,
                    x.discount_pct_vs_msrp if x.discount_pct_vs_msrp is not None else -999.0,
                ),
                reverse=True,
            )
        elif sort_by == "price_asc":
            filtered.sort(key=lambda x: (0 if x.is_on_stock else 1, x.refurb_sale_cents))
        elif sort_by == "price_desc":
            filtered.sort(key=lambda x: (1 if x.is_on_stock else 0, x.refurb_sale_cents), reverse=True)
        elif sort_by == "stock_desc":
            filtered.sort(key=lambda x: (1 if x.is_on_stock else 0, x.available_quantity), reverse=True)
        elif sort_by == "name_asc":
            filtered.sort(key=lambda x: x.name.lower())

        categories = sorted({v.category for v in variants if v.category})
        return {
            "count": len(filtered),
            "threshold_pct": threshold,
            "categories": categories,
            "items": [v.model_dump() for v in filtered],
        }

    @app.post("/api/deals/{sku}/msrp")
    async def set_manual_msrp(sku: str, body: ManualMsrpRequest) -> dict[str, Any]:
        if body.msrp_dollars <= 0:
            raise HTTPException(status_code=400, detail="msrp_dollars must be > 0")
        updated = tracker_db.set_manual_msrp(sku.strip(), body.msrp_dollars)
        if not updated:
            raise HTTPException(status_code=404, detail=f"SKU {sku} not found")
        return updated.model_dump()

    @app.get("/api/history/{sku}")
    async def get_sku_history(sku: str) -> dict[str, Any]:
        variant = tracker_db.get_variant(sku.strip())
        points = tracker_db.get_history(sku.strip())
        return {
            "sku": sku.strip(),
            "variant": variant.model_dump() if variant else None,
            "history": points,
        }

    @app.get("/api/watchlist")
    async def get_watchlist() -> dict[str, Any]:
        entries = tracker_db.get_watchlist()
        variants_by_sku = {v.sku.upper(): v for v in tracker_db.get_all_variants()}
        enriched = []
        for entry in entries:
            d = entry.model_dump()
            snap = variants_by_sku.get(entry.sku.upper())
            d["live_variant"] = snap.model_dump() if snap else None
            enriched.append(d)
        return {
            "items": enriched,
            "presets": POPULAR_NIKON_PRESETS,
        }

    @app.post("/api/watchlist")
    async def upsert_watchlist(req: WatchlistCreateRequest) -> dict[str, Any]:
        sku = req.sku.strip().upper()
        slug = req.slug.strip()
        product_key = req.product_key.strip().upper()
        label = req.label.strip()

        if req.url_or_sku.strip():
            parsed = parse_nikon_url(req.url_or_sku.strip())
            if parsed:
                slug, product_key = parsed[0], parsed[1].upper()
                if not sku:
                    sku = product_key
            elif not sku:
                sku = req.url_or_sku.strip().upper()

        # Check if SKU matches one of the presets or already exists in DB
        if sku and (not slug or not product_key):
            existing_var = tracker_db.get_variant(sku)
            if existing_var:
                slug = slug or existing_var.slug
                product_key = product_key or existing_var.product_key
                label = label or existing_var.name
            else:
                preset = next(
                    (p for p in POPULAR_NIKON_PRESETS if str(p["sku"]).upper() == sku),
                    None,
                )
                if preset:
                    slug = slug or str(preset["slug"])
                    product_key = product_key or str(preset["product_key"]).upper()
                    label = label or str(preset["label"])

        if not sku or not slug or not product_key:
            raise HTTPException(
                status_code=400,
                detail="Please provide a valid NikonUSA product URL (e.g. https://www.nikonusa.com/p/z-f-refurbished/1761Q/overview) or select a preset SKU.",
            )

        # Immediately probe NikonUSA for this product page if not yet in DB
        live_snap = tracker_db.get_variant(sku)
        if live_snap is None:
            try:
                snaps = await tracker_scheduler.scraper.fetch_product_by_url_or_slug(
                    slug, product_key
                )
                for s in snaps:
                    _, updated = tracker_db.upsert_variant(s)
                    if updated.sku.upper() == sku:
                        live_snap = updated
                if live_snap is None and snaps:
                    live_snap = snaps[0]
                    sku = live_snap.sku.upper()
            except Exception:
                pass

        if not label:
            label = live_snap.name if live_snap else f"Nikon Refurbished {sku}"

        entry = WatchlistEntry(
            sku=sku,
            slug=slug,
            product_key=product_key,
            label=label,
            max_price_dollars=req.max_price_dollars,
            min_discount_pct_msrp=req.min_discount_pct_msrp,
            notify_on_restock=req.notify_on_restock,
            auto_open_browser=req.auto_open_browser,
            enabled=req.enabled,
        )
        saved = tracker_db.upsert_watchlist(entry)
        result = saved.model_dump()
        result["live_variant"] = live_snap.model_dump() if live_snap else None
        return result

    @app.delete("/api/watchlist/{sku}")
    async def delete_watchlist(sku: str) -> dict[str, Any]:
        deleted = tracker_db.delete_watchlist(sku)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Watchlist SKU {sku} not found")
        return {"deleted": True, "sku": sku.upper()}

    @app.get("/api/alerts")
    async def get_alerts(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
        alerts = tracker_db.get_recent_alerts(limit=limit)
        return {"items": [a.model_dump() for a in alerts]}

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return tracker_db.get_settings().model_dump()

    @app.put("/api/settings")
    async def update_settings(new_settings: TrackerSettings) -> dict[str, Any]:
        saved = tracker_db.save_settings(new_settings)
        return saved.model_dump()

    @app.post("/api/check-now")
    async def check_now() -> dict[str, Any]:
        snapshots, alerts = await tracker_scheduler.run_once()
        return {
            "status": tracker_scheduler.get_status(),
            "updated_count": len(snapshots),
            "triggered_alerts": [a.model_dump() for a in alerts],
        }

    @app.post("/api/pause")
    async def pause_polling() -> dict[str, Any]:
        tracker_scheduler.pause()
        return tracker_scheduler.get_status()

    @app.post("/api/resume")
    async def resume_polling() -> dict[str, Any]:
        tracker_scheduler.resume()
        return tracker_scheduler.get_status()

    @app.post("/api/mute-desktop")
    async def toggle_mute_desktop() -> dict[str, Any]:
        tracker_scheduler.dispatcher.mute_desktop = (
            not tracker_scheduler.dispatcher.mute_desktop
        )
        return tracker_scheduler.get_status()

    @app.post("/api/test-alert")
    async def send_test_alert() -> dict[str, Any]:
        settings = tracker_db.get_settings()
        sample = AlertEvent(
            event_type="TEST_ALERT",
            sku="1663B",
            name="Nikon Z 6II + NIKKOR Z 24-70mm f/4 S Kit (Refurbished)",
            title="🔥 TEST ALERT: 53% OFF MSRP — Nikon Z 6II Kit ($1,099.95)",
            message=(
                "Test notification from NikonUSA Refurbished Tracker! "
                "Refurb Sale: $1,099.95 vs Official New MSRP: $2,349.95 (46.8% of MSRP • Save $1,250.00)."
            ),
            product_url="https://www.nikonusa.com/p/z-6ii-refurbished/1659B/overview",
            effective_price_dollars=1099.95,
            msrp_dollars=2349.95,
            discount_pct_vs_msrp=53.2,
            available_quantity=5,
            auto_open_browser=False,
        )
        dispatched = await tracker_scheduler.dispatcher.dispatch(sample, settings)
        return dispatched.model_dump()

    return app
