"""Integration tests for the FastAPI web dashboard endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from nikon_tracker.database import TrackerDatabase
from nikon_tracker.models import VariantSnapshot
from nikon_tracker.scheduler import TrackerScheduler
from nikon_tracker.web.app import create_app


def test_api_endpoints(tmp_path: Path) -> None:
    db = TrackerDatabase(tmp_path / "api_test.db")
    scheduler = TrackerScheduler(db=db)

    # Seed two variants: one <= 50% of MSRP, one > 50% of MSRP
    db.upsert_variant(
        VariantSnapshot(
            sku="1663B",
            product_key="1659B",
            name="Z 6II — Kit (20072)",
            slug="z-6ii-refurbished",
            category="Mirrorless Cameras",
            product_url="https://www.nikonusa.com/p/z-6ii-refurbished/1659B/overview",
            refurb_reg_cents=109995,
            refurb_sale_cents=109995,
            true_msrp_cents=234995,
            msrp_source="new_catalog",
            is_on_stock=True,
            available_quantity=396,
        )
    )
    db.upsert_variant(
        VariantSnapshot(
            sku="1659B",
            product_key="1659B",
            name="Z 6II — Body Only",
            slug="z-6ii-refurbished",
            category="Mirrorless Cameras",
            product_url="https://www.nikonusa.com/p/z-6ii-refurbished/1659B/overview",
            refurb_reg_cents=103995,
            refurb_sale_cents=93595,
            true_msrp_cents=174995,
            msrp_source="new_catalog",
            is_on_stock=True,
            available_quantity=8,
        )
    )

    app = create_app(db=db, scheduler=scheduler, start_background_poller=False)
    client = TestClient(app)

    # 1. Status endpoint
    status_res = client.get("/api/status")
    assert status_res.status_code == 200
    status_data = status_res.json()
    assert status_data["total_tracked_skus"] == 2
    assert status_data["in_stock_count"] == 2
    assert status_data["sub_50_msrp_count"] == 1

    # 2. Deals endpoint (all vs sub_50_only vs savings_desc)
    all_deals = client.get("/api/deals").json()
    assert all_deals["count"] == 2
    assert all_deals["total_count"] == 2

    sub50_deals = client.get("/api/deals?sub_50_only=true").json()
    assert sub50_deals["count"] == 1
    assert sub50_deals["items"][0]["sku"] == "1663B"
    assert sub50_deals["total_count"] == 2

    savings_deals = client.get("/api/deals?sort_by=savings_desc").json()
    assert savings_deals["items"][0]["sku"] == "1663B"  # Saves $1,250 vs $814

    index_res = client.get("/")
    assert index_res.status_code == 200
    assert "live-deals-table" in index_res.text

    # 3. Manual MSRP override endpoint (case-insensitive SKU)
    override_res = client.post("/api/deals/1659b/msrp", json={"msrp_dollars": 1999.95})
    assert override_res.status_code == 200
    override_data = override_res.json()
    assert override_data["msrp_dollars"] == 1999.95
    assert override_data["msrp_source"] == "manual"
    assert override_data["is_sub_50_msrp"] is True  # $935.95 / $1999.95 = 46.8% of MSRP

    # 4. Simulate stock drop on 1659B (8 -> 3 units) and verify daily rollup & sellout forecast
    db.upsert_variant(
        VariantSnapshot(
            sku="1659B",
            product_key="1659B",
            name="Z 6II — Body Only",
            slug="z-6ii-refurbished",
            category="Mirrorless Cameras",
            product_url="https://www.nikonusa.com/p/z-6ii-refurbished/1659B/overview",
            refurb_reg_cents=103995,
            refurb_sale_cents=93595,
            true_msrp_cents=199995,
            msrp_source="manual",
            is_on_stock=True,
            available_quantity=3,
        )
    )

    hist_res = client.get("/api/history/1659B")
    assert hist_res.status_code == 200
    hist_json = hist_res.json()
    assert len(hist_json["history"]) == 2
    assert len(hist_json["daily_history"]) == 1
    day_entry = hist_json["daily_history"][0]
    assert day_entry["open_quantity"] == 8
    assert day_entry["close_quantity"] == 3
    assert day_entry["units_sold_est"] == 5
    assert day_entry["net_quantity_change"] == -5

    vel = hist_json["velocity"]
    assert vel["units_sold_24h"] == 5
    assert vel["sellout_today_likely"] is True
    assert vel["sellout_risk"] == "critical"

    vel_deals = client.get("/api/deals?sort_by=velocity_desc").json()
    assert vel_deals["items"][0]["sku"] == "1659B"

    # 5. Settings update endpoint
    set_res = client.put(
        "/api/settings",
        json={
            "poll_interval_seconds": 30,
            "global_discount_threshold_pct": 45.0,
            "desktop_notifications_enabled": False,
        },
    )
    assert set_res.status_code == 200
    assert set_res.json()["poll_interval_seconds"] == 30

    # 6. Full 266-item out-of-stock & refurbished catalog presets + seeding
    wl_res = client.get("/api/watchlist")
    assert wl_res.status_code == 200
    wl_data = wl_res.json()
    assert len(wl_data["presets"]) >= 250
    assert "Mirrorless Cameras (Z Series)" in wl_data["preset_categories"]

    seed_res = client.post("/api/seed-catalog")
    assert seed_res.status_code == 200
    seed_data = seed_res.json()
    assert seed_data["total_catalog_count"] >= 250
    assert seed_data["seeded_count"] >= 240

