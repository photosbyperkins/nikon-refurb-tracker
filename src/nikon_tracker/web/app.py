"""FastAPI web server and REST API for NikonUSA Refurbished Tracker."""

from __future__ import annotations

import asyncio
import csv
import io
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nikon_tracker.database import TrackerDatabase
from nikon_tracker.models import (
    CATALOG_CATEGORIES,
    POPULAR_NIKON_PRESETS,
    AlertEvent,
    TrackerSettings,
    VariantSnapshot,
    WatchlistEntry,
    classify_nikon_mount,
    load_refurbished_catalog,
    resolve_package_path,
)
from nikon_tracker.scheduler import TrackerScheduler
from nikon_tracker.scraper import NIKON_BASE_URL, parse_nikon_url

STATIC_DIR = resolve_package_path("web/static")


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
    notify_on_price_drop: bool = True
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
        version="1.0.0",
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

    @app.get("/api/events")
    async def sse_events(request: Request) -> StreamingResponse:
        """Server-Sent Events stream for real-time poll, price/stock diff, and status updates."""

        async def _event_generator() -> AsyncIterator[str]:
            q = tracker_scheduler.subscribe_sse()
            try:
                init_status = tracker_scheduler.get_status()
                init_payload = json.dumps({"event": "connected", "status": init_status, **init_status})
                yield f"data: {init_payload}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=15.0)
                        ev_name = msg.get("event", "message")
                        raw_data = msg.get("data") or {}
                        frame = {
                            "event": ev_name,
                            "status": tracker_scheduler.get_status(),
                            **(raw_data if isinstance(raw_data, dict) else {}),
                        }
                        yield f"data: {json.dumps(frame)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                tracker_scheduler.unsubscribe_sse(q)

        return StreamingResponse(
            _event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    def _query_filtered_deals(
        *,
        sub_50_only: bool = False,
        in_stock_only: bool = False,
        extra_sale_only: bool = False,
        atl_only: bool = False,
        category: str | None = None,
        mount: str | None = None,
        q: str | None = None,
        sort_by: str = "discount_desc",
    ) -> tuple[list[VariantSnapshot], list[VariantSnapshot], dict[str, dict[str, object]], float]:
        variants = tracker_db.get_all_variants()
        velocity_map = tracker_db.get_velocity_map(variants)
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
            if atl_only and not v.is_all_time_low:
                continue
            if category and category != "ALL" and v.category != category:
                continue
            if mount and mount != "ALL" and v.mount_system != mount:
                continue
            if q:
                needle = q.strip().lower()
                haystack = (
                    f"{v.name} {v.sku} {v.new_sku} {v.category} {v.mount_system} {v.promo_message}".lower()
                )
                if needle not in haystack:
                    continue
            filtered.append(v)

        def _velocity_sort_key(item: VariantSnapshot) -> tuple[int, float, float, int]:
            vel = velocity_map.get(item.sku.upper(), {})
            est_h = vel.get("estimated_hours_left")
            burn_h = float(vel.get("burn_rate_per_hour") or 0.0)
            urgency = (1000.0 / max(0.1, float(est_h))) if isinstance(est_h, (int, float)) else 0.0
            low_stock_score = (
                -item.available_quantity if item.is_on_stock and item.available_quantity > 0 else -999999
            )
            return (1 if item.is_on_stock else 0, urgency, burn_h, low_stock_score)

        if sort_by == "discount_desc":
            filtered.sort(
                key=lambda x: (
                    1 if x.is_on_stock else 0,
                    x.discount_pct_vs_msrp if x.discount_pct_vs_msrp is not None else -999.0,
                ),
                reverse=True,
            )
        elif sort_by == "savings_desc":
            filtered.sort(
                key=lambda x: (
                    1 if x.is_on_stock else 0,
                    x.savings_vs_msrp_dollars if x.savings_vs_msrp_dollars is not None else -999999.0,
                ),
                reverse=True,
            )
        elif sort_by == "velocity_desc":
            filtered.sort(key=_velocity_sort_key, reverse=True)
        elif sort_by == "price_asc":
            filtered.sort(key=lambda x: (0 if x.is_on_stock else 1, x.refurb_sale_cents))
        elif sort_by == "price_desc":
            filtered.sort(key=lambda x: (1 if x.is_on_stock else 0, x.refurb_sale_cents), reverse=True)
        elif sort_by == "stock_desc":
            filtered.sort(key=lambda x: (1 if x.is_on_stock else 0, x.available_quantity), reverse=True)
        elif sort_by == "name_asc":
            filtered.sort(key=lambda x: x.name.lower())

        return filtered, variants, velocity_map, threshold

    @app.get("/api/deals")
    async def get_deals(
        sub_50_only: bool = Query(default=False),
        in_stock_only: bool = Query(default=False),
        extra_sale_only: bool = Query(default=False),
        atl_only: bool = Query(default=False),
        category: str | None = Query(default=None),
        mount: str | None = Query(default=None),
        q: str | None = Query(default=None),
        search: str | None = Query(default=None),
        sort_by: str = Query(default="discount_desc"),
    ) -> dict[str, Any]:
        filtered, variants, velocity_map, threshold = _query_filtered_deals(
            sub_50_only=sub_50_only,
            in_stock_only=in_stock_only,
            extra_sale_only=extra_sale_only,
            atl_only=atl_only,
            category=category,
            mount=mount,
            q=q or search,
            sort_by=sort_by,
        )

        categories = sorted({v.category for v in variants if v.category})
        mounts = sorted({v.mount_system for v in variants if v.mount_system})
        enriched_items = []
        for v in filtered:
            d = v.model_dump()
            d["velocity"] = velocity_map.get(v.sku.upper())
            enriched_items.append(d)

        return {
            "count": len(filtered),
            "total_count": len(variants),
            "threshold_pct": threshold,
            "categories": categories,
            "mounts": mounts,
            "items": enriched_items,
        }

    @app.get("/api/deals/export.csv")
    async def export_deals_csv(
        sub_50_only: bool = Query(default=False),
        in_stock_only: bool = Query(default=False),
        extra_sale_only: bool = Query(default=False),
        atl_only: bool = Query(default=False),
        category: str | None = Query(default=None),
        mount: str | None = Query(default=None),
        q: str | None = Query(default=None),
        search: str | None = Query(default=None),
        sort_by: str = Query(default="discount_desc"),
    ) -> Response:
        """Export currently filtered refurbished deals to a downloadable CSV file."""
        filtered, _variants, velocity_map, _threshold = _query_filtered_deals(
            sub_50_only=sub_50_only,
            in_stock_only=in_stock_only,
            extra_sale_only=extra_sale_only,
            atl_only=atl_only,
            category=category,
            mount=mount,
            q=q or search,
            sort_by=sort_by,
        )

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "SKU",
                "New SKU",
                "Name",
                "Mount System",
                "Category",
                "In Stock",
                "Stock Qty",
                "Refurb Sale Price ($)",
                "Regular Refurb Price ($)",
                "Official New MSRP ($)",
                "% Off MSRP",
                "Savings vs MSRP ($)",
                "All-Time Low ($)",
                "Is All-Time Low",
                "24h Units Sold",
                "Burn Rate (/hr)",
                "Sellout Forecast",
                "Product URL",
            ]
        )
        for v in filtered:
            vel = velocity_map.get(v.sku.upper(), {})
            low_dollars = round(v.lowest_seen_cents / 100.0, 2) if v.lowest_seen_cents else ""
            writer.writerow(
                [
                    v.sku,
                    v.new_sku,
                    v.name,
                    v.mount_system,
                    v.category,
                    "Yes" if v.is_on_stock else "No",
                    v.available_quantity,
                    f"{v.effective_price_dollars:.2f}",
                    f"{v.refurb_reg_dollars:.2f}",
                    f"{v.msrp_dollars:.2f}" if v.msrp_dollars is not None else "",
                    f"{v.discount_pct_vs_msrp:.1f}" if v.discount_pct_vs_msrp is not None else "",
                    f"{v.savings_vs_msrp_dollars:.2f}" if v.savings_vs_msrp_dollars is not None else "",
                    f"{low_dollars:.2f}" if isinstance(low_dollars, float) else "",
                    "Yes" if v.is_all_time_low else "No",
                    vel.get("units_sold_24h", 0),
                    vel.get("burn_rate_per_hour", 0.0),
                    vel.get("sellout_forecast", ""),
                    v.product_url,
                ]
            )

        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="nikon_refurbished_deals.csv"'
            },
        )

    @app.post("/api/deals/{sku}/msrp")
    async def set_manual_msrp(sku: str, body: ManualMsrpRequest) -> dict[str, Any]:
        if body.msrp_dollars <= 0:
            raise HTTPException(status_code=400, detail="msrp_dollars must be > 0")
        updated = tracker_db.set_manual_msrp(sku.strip(), body.msrp_dollars)
        if not updated:
            raise HTTPException(status_code=404, detail=f"SKU {sku} not found")
        tracker_scheduler.broadcast_event("status_updated", tracker_scheduler.get_status())
        return updated.model_dump()

    @app.get("/api/history/{sku}")
    async def get_sku_history(sku: str) -> dict[str, Any]:
        clean_sku = sku.strip().upper()
        variant = tracker_db.get_variant(clean_sku)
        points = tracker_db.get_history(clean_sku)
        daily_points = tracker_db.get_daily_history(clean_sku)
        vel_map = tracker_db.get_velocity_map([variant] if variant else [])
        return {
            "sku": clean_sku,
            "variant": variant.model_dump() if variant else None,
            "velocity": vel_map.get(clean_sku),
            "daily_history": daily_points,
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

        full_catalog = load_refurbished_catalog()
        enriched_catalog = []
        for item in full_catalog:
            c_copy = dict(item)
            sku_key = str(c_copy.get("sku", "")).upper()
            slug_str = str(c_copy.get("slug", ""))
            label_str = str(c_copy.get("label", ""))
            c_copy["mount_system"] = classify_nikon_mount("", slug_str, label_str)
            live = variants_by_sku.get(sku_key)
            if live:
                c_copy["refurb_dollars"] = live.effective_price_dollars
                c_copy["msrp_dollars"] = live.msrp_dollars or c_copy.get("msrp_dollars")
                c_copy["discount_pct_vs_msrp"] = (
                    live.discount_pct_vs_msrp
                    if live.discount_pct_vs_msrp is not None
                    else c_copy.get("discount_pct_vs_msrp")
                )
                c_copy["is_on_stock"] = live.is_on_stock
                c_copy["qty"] = live.available_quantity
            enriched_catalog.append(c_copy)

        return {
            "items": enriched,
            "presets": enriched_catalog,
            "preset_categories": CATALOG_CATEGORIES,
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

        all_presets = load_refurbished_catalog() + list(POPULAR_NIKON_PRESETS)

        # Check if SKU matches one of the presets or already exists in DB
        if sku and (not slug or not product_key):
            existing_var = tracker_db.get_variant(sku)
            if existing_var:
                slug = slug or existing_var.slug
                product_key = product_key or existing_var.product_key
                label = label or existing_var.name
            else:
                preset = next(
                    (p for p in all_presets if str(p["sku"]).upper() == sku),
                    None,
                )
                if preset:
                    slug = slug or str(preset["slug"])
                    product_key = product_key or str(preset["product_key"]).upper()
                    label = label or str(preset["label"])

        if not sku or not slug or not product_key:
            raise HTTPException(
                status_code=400,
                detail="Please provide a valid NikonUSA product URL (e.g. https://www.nikonusa.com/p/z-f-refurbished/1761Q/overview) or select a catalog SKU.",
            )

        # Immediately probe NikonUSA for this product page if not yet in DB
        live_snap = tracker_db.get_variant(sku)
        if live_snap is None:
            try:
                snaps = await tracker_scheduler.scraper.fetch_product_by_url_or_slug(
                    slug, product_key
                )
                if snaps:
                    pairs = tracker_db.upsert_variants_batch(snaps)
                    for _, updated in pairs:
                        if updated.sku.upper() == sku:
                            live_snap = updated
                    if live_snap is None:
                        live_snap = pairs[0][1]
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
            notify_on_price_drop=req.notify_on_price_drop,
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

    @app.post("/api/seed-catalog")
    async def seed_full_catalog() -> dict[str, Any]:
        """Seed all 266 curated refurbished & out-of-stock items into the local Deals database in a single batch transaction."""
        catalog = load_refurbished_catalog()
        existing_skus = {v.sku.upper() for v in tracker_db.get_all_variants()}
        to_insert: list[VariantSnapshot] = []
        for item in catalog:
            sku = str(item.get("sku", "")).strip().upper()
            if not sku or sku in existing_skus:
                continue
            refurb_dollars = float(item.get("refurb_dollars") or 0.0)
            if refurb_dollars <= 0:
                continue
            msrp_dollars = item.get("msrp_dollars")
            msrp_cents = int(round(float(msrp_dollars) * 100)) if msrp_dollars else None
            refurb_cents = int(round(refurb_dollars * 100))
            slug = str(item.get("slug") or "")
            product_key = str(item.get("product_key") or sku)
            snap = VariantSnapshot(
                sku=sku,
                product_key=product_key,
                name=str(item.get("label") or sku),
                slug=slug,
                category=str(item.get("category") or "Refurbished Gear"),
                product_url=f"{NIKON_BASE_URL}/p/{slug}/{product_key}/overview",
                refurb_reg_cents=refurb_cents,
                refurb_sale_cents=refurb_cents,
                new_retail_cents=msrp_cents,
                true_msrp_cents=msrp_cents,
                msrp_source="new_catalog" if msrp_cents else "unknown",
                is_on_stock=False,
                available_quantity=0,
                add_to_cart_status="OUT_OF_STOCK",
            )
            to_insert.append(snap)
            existing_skus.add(sku)

        if to_insert:
            tracker_db.upsert_variants_batch(to_insert)

        status = tracker_scheduler.get_status()
        tracker_scheduler.broadcast_event("status_updated", status)
        return {
            "seeded_count": len(to_insert),
            "total_catalog_count": len(catalog),
            "status": status,
        }

    @app.post("/api/discover-oos")
    async def discover_oos_live() -> dict[str, Any]:
        """Live-probe NikonUSA's sitemap for all refurbished & out-of-stock pages."""
        snapshots = await tracker_scheduler.scraper.discover_all_refurbished_from_sitemap()
        if snapshots:
            tracker_db.upsert_variants_batch(snapshots)
        status = tracker_scheduler.get_status()
        tracker_scheduler.broadcast_event("status_updated", status)
        return {
            "discovered_count": len(snapshots),
            "status": status,
        }

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
        tracker_scheduler.broadcast_event("status_updated", tracker_scheduler.get_status())
        return saved.model_dump()

    @app.get("/api/backup")
    async def export_backup() -> Response:
        """Export portable JSON backup of settings, watchlist rules, and manual MSRPs."""
        data = tracker_db.export_backup()
        return Response(
            content=json.dumps(data, indent=2),
            media_type="application/json; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="nikon_tracker_backup.json"'
            },
        )

    @app.post("/api/backup")
    async def import_backup(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Restore settings, watchlist rules, and manual MSRPs from a JSON backup."""
        summary = tracker_db.import_backup(payload)
        status = tracker_scheduler.get_status()
        tracker_scheduler.broadcast_event("status_updated", status)
        return {"restored": summary, **summary, "status": status}

    @app.post("/api/check-now")
    async def check_now() -> dict[str, Any]:
        snapshots, alerts = await tracker_scheduler.run_once()
        return {
            "status": tracker_scheduler.get_status(),
            "updated_count": len(snapshots),
            "changed_skus": tracker_scheduler.last_changed_skus,
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
    async def toggle_mute_desktop(
        body: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        if isinstance(body, dict) and "muted" in body:
            next_muted = bool(body["muted"])
        else:
            next_muted = not tracker_scheduler.dispatcher.mute_desktop
        tracker_scheduler.dispatcher.mute_desktop = next_muted
        status = tracker_scheduler.get_status()
        tracker_scheduler.broadcast_event("status_updated", status)
        return status

    @app.post("/api/test-alert")
    async def send_test_alert(
        channel: str = Query(default="all"),
        settings_override: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        base_settings = tracker_db.get_settings()
        if isinstance(settings_override, dict) and settings_override:
            merged = {**base_settings.model_dump(), **settings_override}
            settings = TrackerSettings(**merged)
        else:
            settings = base_settings

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
        dispatched = await tracker_scheduler.dispatcher.dispatch_channel(
            sample, settings, channel=channel
        )
        return dispatched.model_dump()

    return app
