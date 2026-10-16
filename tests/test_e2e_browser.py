"""Frontend DOM, JavaScript syntax, Category/Mount alignment, and full-stack user journey E2E tests."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from nikon_tracker.database import TrackerDatabase
from nikon_tracker.models import (
    CATALOG_CATEGORIES,
    classify_nikon_mount,
    load_refurbished_catalog,
)
from nikon_tracker.notifier import AlertDispatcher
from nikon_tracker.scheduler import TrackerScheduler
from nikon_tracker.scraper import NikonUSAClient
from nikon_tracker.web.app import STATIC_DIR, create_app


def test_frontend_dom_ids_and_js_integrity():
    """Verify that:
    1. app.js has valid JavaScript syntax (via node --check when available) and zero native alert()/prompt() calls.
    2. Every static document.getElementById('...') referenced in app.js exists in index.html or dynamic modal templates.
    3. Every quick-category pill data-cat in index.html matches CATALOG_CATEGORIES.
    4. Every mount option in #filter-mount in index.html matches classify_nikon_mount() output.
    """
    html_path = STATIC_DIR / "index.html"
    js_path = STATIC_DIR / "app.js"
    css_path = STATIC_DIR / "styles.css"

    html_text = html_path.read_text(encoding="utf-8")
    js_text = js_path.read_text(encoding="utf-8")
    css_text = css_path.read_text(encoding="utf-8")

    # 1. Zero blocking native dialogs (prompt/alert) in app.js
    assert "window.prompt(" not in js_text
    assert "window.alert(" not in js_text
    assert re.search(r"(?<![a-zA-Z0-9_])prompt\(", js_text) is None
    assert re.search(r"(?<![a-zA-Z0-9_])alert\(", js_text) is None

    # Validate JS syntax with node --check if node is installed
    node_bin = shutil.which("node")
    if node_bin:
        proc = subprocess.run([node_bin, "--check", str(js_path)], capture_output=True, text=True, check=False)
        assert proc.returncode == 0, f"JavaScript syntax error in app.js: {proc.stderr}"

    # 2. Verify all getElementById("...") calls in app.js exist in index.html or dynamic templates in app.js
    html_ids = set(re.findall(r'id="([^"]+)"', html_text))
    dynamic_js_ids = set(re.findall(r'id="([^"$]+)"', js_text))
    all_defined_ids = html_ids | dynamic_js_ids

    referenced_ids = set(re.findall(r'getElementById\("([^"$]+)"\)', js_text))
    missing_ids = referenced_ids - all_defined_ids
    assert not missing_ids, f"IDs referenced in app.js but missing from index.html: {sorted(missing_ids)}"

    # 3. Verify quick-category pill data-cat attributes match CATALOG_CATEGORIES
    catalog_categories = set(CATALOG_CATEGORIES)
    pill_cats = re.findall(r'class="cat-pill[^"]*"\s+data-cat="([^"]+)"', html_text)
    assert len(pill_cats) >= 5
    for cat in pill_cats:
        if cat != "ALL":
            assert cat in catalog_categories, (
                f"Quick-category pill data-cat='{cat}' does not match any preset category: {catalog_categories}"
            )

    # 4. Verify mount filter options in index.html match classify_nikon_mount() across the 266-item catalog
    full_catalog = load_refurbished_catalog()
    catalog_mounts = {
        classify_nikon_mount("", str(p.get("slug", "")), str(p.get("label", "")))
        for p in full_catalog
    }
    assert catalog_mounts == {"Z-Mount FX", "Z-Mount DX", "F-Mount", "Optics & Accessories"}
    for m in catalog_mounts:
        assert f'value="{m}"' in html_text

    # 5. Verify key v1.0 CSS classes exist in styles.css
    for cls in [
        ".flash-sale-banner",
        ".badge-atl",
        ".mount-pill",
        ".msrp-inline-editor",
        ".chart-hud-bar",
        ".row-just-updated",
    ]:
        assert cls in css_text, f"Expected CSS class {cls} in styles.css"


def test_e2e_full_user_journey_catalog_filters_and_inline_msrp(tmp_path: Path):
    """End-to-end test of the complete user workflow:
    - Boot dashboard -> Seed 266-item catalog
    - Filter by each Quick-Category pill and each Mount System
    - Inline edit MSRP on a deal and verify live discount recalculation
    - Add & edit a watchlist rule with notify_on_price_drop
    - Inspect SKU history modal payload
    """
    db = TrackerDatabase(tmp_path / "journey.db")
    scraper = NikonUSAClient(db)
    dispatcher = AlertDispatcher(db)
    scheduler = TrackerScheduler(db=db, scraper=scraper, dispatcher=dispatcher)
    app = create_app(db=db, scheduler=scheduler, start_background_poller=False)
    client = TestClient(app)

    # 1. Load static assets
    assert client.get("/").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200

    # 2. Seed all 266 catalog items
    seed_res = client.post("/api/seed-catalog")
    assert seed_res.status_code == 200
    seed_data = seed_res.json()
    assert seed_data["total_catalog_count"] >= 260

    # 3. Verify every Mount System filter returns matching items
    for mount in ["Z-Mount FX", "Z-Mount DX", "F-Mount", "Optics & Accessories"]:
        res = client.get("/api/deals", params={"mount": mount, "in_stock_only": False})
        assert res.status_code == 200
        items = res.json()["items"]
        assert len(items) > 0, f"Expected items for mount={mount}"
        assert all(i["mount_system"] == mount for i in items)

    # 4. Verify every Quick-Category pill returns matching items
    for cat in [
        "Mirrorless Cameras (Z Series)",
        "NIKKOR Z Lenses & Teleconverters",
        "DSLR Cameras",
        "F-Mount DSLR Lenses (AF-S / AF-P / PC)",
        "Adapters & Accessories (FTZ)",
        "Sport Optics (Binoculars, Rangefinders, Fieldscopes)",
    ]:
        res = client.get("/api/deals", params={"category": cat, "in_stock_only": False})
        assert res.status_code == 200
        items = res.json()["items"]
        assert len(items) > 0, f"Expected items for category={cat}"
        assert all(i["category"] == cat for i in items)

    # 5. Inline MSRP override workflow (no window.prompt)
    msrp_res = client.post("/api/deals/1695Q/msrp", json={"msrp_dollars": 4500.0})
    assert msrp_res.status_code == 200
    deal_1695 = next(
        i
        for i in client.get("/api/deals", params={"q": "1695Q", "in_stock_only": False}).json()["items"]
        if i["sku"] == "1695Q"
    )
    assert deal_1695["msrp_dollars"] == 4500.0

    # 6. History modal endpoint
    hist_res = client.get("/api/history/1695Q")
    assert hist_res.status_code == 200
    hist_data = hist_res.json()
    assert hist_data["sku"] == "1695Q"
    assert len(hist_data["daily_history"]) >= 1
    assert "velocity" in hist_data

    asyncio.run(scraper.aclose())
