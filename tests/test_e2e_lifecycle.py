"""End-to-End (E2E) multi-poll lifecycle, resilience, velocity, and backup/restore tests."""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from nikon_tracker.database import TrackerDatabase
from nikon_tracker.models import VariantSnapshot, WatchlistEntry, classify_nikon_mount
from nikon_tracker.notifier import AlertDispatcher
from nikon_tracker.scheduler import TrackerScheduler
from nikon_tracker.scraper import NIKON_BASE_URL, REFURB_CATALOG_PATH, NikonUSAClient
from nikon_tracker.web.app import create_app

CATALOG_URL = f"{NIKON_BASE_URL}{REFURB_CATALOG_PATH}"


def _make_variant(
    sku: str = "1669B",
    name: str = "Nikon Z 8 Refurbished",
    slug: str = "z-8-refurbished",
    product_key: str = "1669Q",
    product_type_key: str = "mirrorless-cameras",
    refurb_sale_cents: int = 199995,
    refurb_reg_cents: int = 349995,
    true_msrp_cents: int | None = 399995,
    is_on_stock: bool = True,
    available_quantity: int = 10,
) -> VariantSnapshot:
    return VariantSnapshot(
        sku=sku,
        product_key=product_key,
        name=name,
        slug=slug,
        product_url=f"https://www.nikonusa.com/p/{slug}/{product_key}/overview",
        image_url="https://cdn-4.nikon-cdn.com/z8.png",
        product_type_key=product_type_key,
        is_on_stock=is_on_stock,
        available_quantity=available_quantity,
        refurb_sale_cents=refurb_sale_cents,
        refurb_reg_cents=refurb_reg_cents,
        new_retail_cents=true_msrp_cents,
        true_msrp_cents=true_msrp_cents,
        msrp_source="new_catalog" if true_msrp_cents else "unknown",
        promo_message="Extra Refurb Flash Sale",
    )


@pytest.mark.asyncio
async def test_e2e_multi_poll_lifecycle_and_sse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Simulate a realistic multi-poll lifecycle:
    Poll 1: Cold start with 3 sub-50% deals -> consolidated single desktop toast + audible debounce,
            ATL detected, flash sale detected.
    Poll 2: Intraday stock burn (10 -> 3 units) + Watchlist OOS restock (0 -> 2 units) -> WATCHLIST_RESTOCK alert.
    Poll 3: Watchlist price drop ($1,999.95 -> $1,799.95, still above $1,500 target) -> WATCHLIST_PRICE_DROP alert.
    """
    db = TrackerDatabase(tmp_path / "e2e_lifecycle.db")
    scraper = NikonUSAClient(db)
    dispatcher = AlertDispatcher(db)

    desktop_toasts: list[str] = []

    async def fake_send_desktop(alert, settings, play_sound=True):
        desktop_toasts.append(alert.title)
        return True

    monkeypatch.setattr(dispatcher, "_send_desktop", fake_send_desktop)

    scheduler = TrackerScheduler(db=db, scraper=scraper, dispatcher=dispatcher)
    queue = scheduler.subscribe_sse()

    # Add a watched lens (NIKKOR Z 24-70mm f/2.8 S Refurbished, 20089B)
    db.upsert_watchlist(
        WatchlistEntry(
            sku="20089B",
            slug="nikkor-z-24-70mm-f2-8-s-refurbished",
            product_key="20089Q",
            label="NIKKOR Z 24-70mm f/2.8 S Refurbished",
            max_price_dollars=1500.0,
            min_discount_pct_msrp=45.0,
            notify_on_restock=True,
            notify_on_price_drop=True,
        )
    )

    # --- Poll 1: Cold Start with 3 sub-50% deals ---
    v_z8 = _make_variant(
        sku="1669B",
        name="Nikon Z 8 Refurbished",
        refurb_sale_cents=199995,  # 50% off $3,999.95 MSRP
        refurb_reg_cents=349995,
        true_msrp_cents=399995,
        is_on_stock=True,
        available_quantity=10,
    )
    v_z6ii = _make_variant(
        sku="1659B",
        name="Nikon Z 6II Refurbished",
        slug="z-6ii-refurbished",
        product_key="1659Q",
        refurb_sale_cents=89995,  # >50% off $1,999.95 MSRP
        refurb_reg_cents=159995,
        true_msrp_cents=199995,
        is_on_stock=True,
        available_quantity=5,
    )
    v_zf = _make_variant(
        sku="1761B",
        name="Nikon Z f Refurbished",
        slug="z-f-refurbished",
        product_key="1761Q",
        refurb_sale_cents=94995,  # >50% off $1,999.95 MSRP
        refurb_reg_cents=159995,
        true_msrp_cents=199995,
        is_on_stock=True,
        available_quantity=4,
    )
    v_lens_oos = _make_variant(
        sku="20089B",
        name="NIKKOR Z 24-70mm f/2.8 S Refurbished",
        slug="nikkor-z-24-70mm-f2-8-s-refurbished",
        product_key="20089Q",
        product_type_key="mirrorless-lenses",
        refurb_sale_cents=199995,
        refurb_reg_cents=199995,
        true_msrp_cents=239995,
        is_on_stock=False,
        available_quantity=0,
    )

    poll_stage = {"step": 1}

    async def fake_fetch_catalog():
        if poll_stage["step"] == 1:
            return [v_z8, v_z6ii, v_zf]
        if poll_stage["step"] == 2:
            # Z8 burns down from 10 -> 3 units
            return [v_z8.model_copy(update={"available_quantity": 3}), v_z6ii, v_zf]
        # Step 3: Z8 price rises above ATL ($2,799.95)
        return [v_z8.model_copy(update={"available_quantity": 3, "refurb_sale_cents": 279995}), v_z6ii, v_zf]

    async def fake_fetch_watchlist(entries, catalog_skus):
        if poll_stage["step"] == 1:
            return [v_lens_oos]
        if poll_stage["step"] == 2:
            # Restocks at $1,999.95 (2 units)
            return [v_lens_oos.model_copy(update={"is_on_stock": True, "available_quantity": 2})]
        # Step 3: Drops price from $1,999.95 -> $1,799.95 (still above $1,500 target, so WATCHLIST_PRICE_DROP fires!)
        return [
            v_lens_oos.model_copy(
                update={
                    "is_on_stock": True,
                    "available_quantity": 2,
                    "refurb_sale_cents": 179995,
                }
            )
        ]

    monkeypatch.setattr(scraper, "fetch_refurbished_catalog", fake_fetch_catalog)
    monkeypatch.setattr(scraper, "fetch_watchlist_items", fake_fetch_watchlist)

    snaps1, alerts1 = await scheduler.run_once()
    assert len(snaps1) == 4
    assert len(alerts1) == 3
    # All 3 cold-start alerts (>2) must be consolidated into a SINGLE desktop summary toast!
    assert len(desktop_toasts) == 1
    assert "3 Nikon Refurbished Deals Found" in desktop_toasts[0]

    # Check SSE events emitted for Poll 1
    events_poll1 = []
    while not queue.empty():
        events_poll1.append(queue.get_nowait())
    event_names = [e["event"] for e in events_poll1]
    assert "poll_started" in event_names
    assert "poll_completed" in event_names

    status1 = scheduler.get_status()
    assert status1["flash_sale_active"] is True
    assert status1["max_extra_sale_pct"] >= 40.0
    assert status1["atl_count"] >= 3

    # --- Poll 2: Stock burn on Z8 + OOS Restock on 20089B ---
    poll_stage["step"] = 2
    snaps2, alerts2 = await scheduler.run_once()
    assert any(a.event_type == "WATCHLIST_RESTOCK" and a.sku == "20089B" for a in alerts2)
    assert "1669B" in scheduler.last_changed_skus
    assert "20089B" in scheduler.last_changed_skus

    # Check velocity calculation on Z8 (burned 7 units from 10 -> 3)
    vel_map = db.get_velocity_map()
    assert vel_map["1669B"]["units_sold_24h"] == 7
    assert vel_map["1669B"]["burn_rate_per_hour"] > 0

    # --- Poll 3: Watchlist Price Drop on 20089B + Z8 price rises above ATL ---
    poll_stage["step"] = 3
    snaps3, alerts3 = await scheduler.run_once()
    assert any(a.event_type == "WATCHLIST_PRICE_DROP" and a.sku == "20089B" for a in alerts3)

    z8_after = db.get_variant("1669B")
    assert z8_after is not None
    assert z8_after.lowest_seen_cents == 199995
    assert z8_after.refurb_sale_cents == 279995
    assert z8_after.is_all_time_low is False

    lens_after = db.get_variant("20089B")
    assert lens_after is not None
    assert lens_after.is_all_time_low is True
    assert lens_after.mount_system == "Z-Mount FX"

    scheduler.unsubscribe_sse(queue)
    await scraper.aclose()


@pytest.mark.asyncio
async def test_e2e_build_id_404_rotation_recovery(tmp_path: Path):
    """Verify that when NikonUSA deploys a new Next.js buildId and returns 404 on /_next/data/old-build/...,
    NikonUSAClient automatically refreshes the buildId and completes product probing without failing.
    """
    db = TrackerDatabase(tmp_path / "build_rotation.db")
    client = NikonUSAClient(db)
    client.build_id = "stale-build-id"

    stale_next_url = "https://www.nikonusa.com/_next/data/stale-build-id/en-US/p/z-8-refurbished/1669Q/overview.json"
    fresh_next_url = "https://www.nikonusa.com/_next/data/fresh-build-999/en-US/p/z-8-refurbished/1669Q/overview.json"
    new_msrp_url = "https://www.nikonusa.com/_next/data/fresh-build-999/en-US/p/z-8/1669/overview.json"

    catalog_html_with_fresh_build = """
    <!DOCTYPE html><html><head>
    <script id="__NEXT_DATA__" type="application/json">
    {"buildId": "fresh-build-999", "props": {"pageProps": {"data": {}}}}
    </script></head><body></body></html>
    """

    next_data_payload = {
        "pageProps": {
            "data": {
                "data": {
                    "dataSources": {
                        "prod1": {
                            "product": {
                                "key": "1669Q",
                                "name": "Nikon Z 8 Refurbished",
                                "slug": "z-8-refurbished",
                                "productType": {"key": "mirrorless-cameras"},
                                "variants": [
                                    {
                                        "sku": "1669B",
                                        "attributes": {"isRefurbished": True},
                                        "availability": {
                                            "isOnStock": True,
                                            "availableQuantity": 4,
                                            "addToCartStatus": "AVAILABLE",
                                        },
                                        "price": {
                                            "centAmount": 279995,
                                            "custom": {"fields": {"basePrice": {"centAmount": 329995}}},
                                        },
                                        "images": [{"url": "https://cdn-4.nikon-cdn.com/z8.png"}],
                                    }
                                ],
                            }
                        }
                    }
                }
            }
        }
    }

    with respx.mock(assert_all_called=False) as mock_http:
        mock_http.get(stale_next_url).mock(return_value=httpx.Response(404))
        mock_http.get(CATALOG_URL).mock(return_value=httpx.Response(200, text=catalog_html_with_fresh_build))
        mock_http.get(fresh_next_url).mock(return_value=httpx.Response(200, json=next_data_payload))
        mock_http.get(new_msrp_url).mock(return_value=httpx.Response(404))

        snaps = await client.fetch_product_by_url_or_slug("z-8-refurbished", "1669Q")
        assert len(snaps) == 1
        snap = snaps[0]
        assert snap.sku == "1669B"
        assert snap.refurb_sale_cents == 279995
        assert snap.available_quantity == 4
        assert client.build_id == "fresh-build-999"

    await client.aclose()


def test_e2e_mount_classification_and_velocity_bounded_query(tmp_path: Path):
    """Verify mount classification across Z-Mount FX, Z-Mount DX, F-Mount, and Optics & Accessories,
    and verify bounded 8-day velocity query performance/correctness.
    """
    assert classify_nikon_mount("mirrorless-cameras", "z-8-refurbished", "Nikon Z 8 Refurbished") == "Z-Mount FX"
    assert classify_nikon_mount("mirrorless-cameras", "z-50-refurbished", "Nikon Z 50 Refurbished") == "Z-Mount DX"
    assert (
        classify_nikon_mount("mirrorless-lenses", "nikkor-z-dx-24mm-f1-7-refurbished", "NIKKOR Z DX 24mm f/1.7")
        == "Z-Mount DX"
    )
    assert classify_nikon_mount("dslr-cameras", "d850-refurbished", "Nikon D850 Refurbished") == "F-Mount"
    assert (
        classify_nikon_mount("dslr-lenses", "af-s-nikkor-500mm-f5-6e-pf-ed-vr", "AF-S NIKKOR 500mm f/5.6E PF ED VR")
        == "F-Mount"
    )
    assert classify_nikon_mount("sport-optics", "monarch-m7-8x42", "MONARCH M7 8x42") == "Optics & Accessories"

    db = TrackerDatabase(tmp_path / "velocity_bound.db")
    v = _make_variant(sku="1669B", available_quantity=10)
    db.upsert_variants_batch([v])

    # Insert an ancient history point 30 days ago (outside 8-day window) and recent points
    old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    recent_ts_1 = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    recent_ts_2 = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with db._connect() as conn:
        conn.execute("DELETE FROM price_stock_history WHERE sku = '1669B'")
        conn.execute(
            """
            INSERT INTO price_stock_history
            (sku, recorded_at, refurb_sale_cents, refurb_reg_cents, true_msrp_cents, is_on_stock, available_quantity)
            VALUES (?, ?, ?, ?, ?, ?, ?), (?, ?, ?, ?, ?, ?, ?), (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "1669B",
                old_ts,
                199995,
                349995,
                399995,
                1,
                99,  # 30 days ago: should be ignored by 8-day bound!
                "1669B",
                recent_ts_1,
                199995,
                349995,
                399995,
                1,
                10,
                "1669B",
                recent_ts_2,
                199995,
                349995,
                399995,
                1,
                4,
            ),
        )
    db._invalidate_cache()
    vmap = db.get_velocity_map()
    assert vmap["1669B"]["units_sold_24h"] == 6
    assert vmap["1669B"]["units_sold_7d"] == 6


def test_e2e_backup_restore_csv_and_channel_test(tmp_path: Path):
    """Test full round-trip JSON backup & restore across two separate databases,
    CSV export filtering, desktop mute toggle, and per-channel test alert with draft settings.
    """
    db1 = TrackerDatabase(tmp_path / "db1.db")
    scraper1 = NikonUSAClient(db1)
    dispatcher1 = AlertDispatcher(db1)
    scheduler1 = TrackerScheduler(db=db1, scraper=scraper1, dispatcher=dispatcher1)
    app1 = create_app(db=db1, scheduler=scheduler1, start_background_poller=False)
    client1 = TestClient(app1)

    # Seed variants and configure custom state in db1
    v_fx = _make_variant(sku="1669B", name="Nikon Z 8 Refurbished", refurb_sale_cents=199995, true_msrp_cents=399995)
    v_dx = _make_variant(
        sku="1632B",
        name="Nikon Z 50 Refurbished",
        slug="z-50-refurbished",
        product_key="1632Q",
        refurb_sale_cents=49995,
        refurb_reg_cents=75995,
        true_msrp_cents=85995,
    )
    db1.upsert_variants_batch([v_fx, v_dx])
    db1.set_manual_msrp("1669B", 4299.95)
    db1.upsert_watchlist(
        WatchlistEntry(
            sku="1669B",
            slug="z-8-refurbished",
            product_key="1669Q",
            label="Nikon Z 8 Refurbished",
            max_price_dollars=2100.0,
            min_discount_pct_msrp=48.0,
            notify_on_restock=True,
            notify_on_price_drop=True,
        )
    )
    current_settings = db1.get_settings().model_dump()
    current_settings.update({"poll_interval_seconds": 90, "ntfy_topic": "nikon-v1-test"})
    client1.put("/api/settings", json=current_settings)

    # Test Mount & ATL filters on /api/deals
    res_dx = client1.get("/api/deals?mount=Z-Mount+DX&atl_only=true")
    assert res_dx.status_code == 200
    dx_items = res_dx.json()["items"]
    assert len(dx_items) == 1
    assert dx_items[0]["sku"] == "1632B"
    assert dx_items[0]["mount_system"] == "Z-Mount DX"
    assert dx_items[0]["is_all_time_low"] is True

    # Test CSV Export
    csv_res = client1.get("/api/deals/export.csv?mount=Z-Mount+DX")
    assert csv_res.status_code == 200
    assert "text/csv" in csv_res.headers["content-type"]
    rows = list(csv.DictReader(io.StringIO(csv_res.text)))
    assert len(rows) == 1
    assert rows[0]["SKU"] == "1632B"
    assert rows[0]["Mount System"] == "Z-Mount DX"

    # Test Desktop Mute Toggle
    mute_res = client1.post("/api/mute-desktop", json={"muted": True})
    assert mute_res.status_code == 200
    assert mute_res.json()["mute_desktop"] is True
    assert mute_res.json()["desktop_sound_muted"] is True

    # Test Per-Channel Test Alert with unsaved draft settings override
    with respx.mock(assert_all_called=False) as mock_http:
        mock_http.post("https://ntfy.sh/draft-topic-123").mock(return_value=httpx.Response(200))
        ch_res = client1.post(
            "/api/test-alert?channel=ntfy",
            json={"ntfy_server_url": "https://ntfy.sh", "ntfy_topic": "draft-topic-123"},
        )
        assert ch_res.status_code == 200
        assert ch_res.json()["channels_sent"] == ["ntfy"]

    # Export JSON Backup from db1 (includes default 1761Q + our 1669B)
    backup_res = client1.get("/api/backup")
    assert backup_res.status_code == 200
    backup_payload = backup_res.json()
    assert backup_payload["version"] == "1.0.0"
    assert len(backup_payload["watchlist"]) == 2
    assert backup_payload["manual_msrps"]["1669B"] == 4299.95

    # Restore JSON Backup into a brand-new empty database db2 BEFORE any SKU is polled
    db2 = TrackerDatabase(tmp_path / "db2.db")
    scraper2 = NikonUSAClient(db2)
    dispatcher2 = AlertDispatcher(db2)
    scheduler2 = TrackerScheduler(db=db2, scraper=scraper2, dispatcher=dispatcher2)
    app2 = create_app(db=db2, scheduler=scheduler2, start_background_poller=False)
    client2 = TestClient(app2)

    restore_res = client2.post("/api/backup", json=backup_payload)
    assert restore_res.status_code == 200
    summary = restore_res.json()
    assert summary["watchlist_restored"] == 2
    assert summary["manual_msrps_restored"] == 1
    assert db2.get_settings().ntfy_topic == "nikon-v1-test"
    assert db2.get_manual_msrps()["1669B"] == 4299.95

    # Now when 1669B is polled for the first time, it automatically inherits the restored manual MSRP!
    db2.upsert_variants_batch([v_fx])
    v_fx_restored = db2.get_variant("1669B")
    assert v_fx_restored is not None
    assert v_fx_restored.msrp_dollars == 4299.95
    assert v_fx_restored.msrp_source == "manual"

    wl2 = {w.sku: w for w in db2.get_watchlist()}
    assert "1669B" in wl2
    assert wl2["1669B"].notify_on_price_drop is True

    asyncio.run(scraper1.aclose())
    asyncio.run(scraper2.aclose())
