"""Unit tests for alert rule evaluation and SQLite cooldown deduplication."""

from __future__ import annotations

from pathlib import Path

from nikon_tracker.database import TrackerDatabase
from nikon_tracker.models import TrackerSettings, VariantSnapshot, WatchlistEntry
from nikon_tracker.notifier import evaluate_variant_alerts


def _make_snapshot(
    sku: str = "2181B",
    name: str = "AF-S DX NIKKOR 10-24mm",
    refurb_reg_cents: int = 47995,
    refurb_sale_cents: int = 43195,
    true_msrp_cents: int = 89995,
    is_on_stock: bool = True,
    available_quantity: int = 10,
) -> VariantSnapshot:
    return VariantSnapshot(
        sku=sku,
        product_key=sku,
        name=name,
        slug="test-lens-refurbished",
        product_url="https://www.nikonusa.com/p/test-lens-refurbished/2181B/overview",
        refurb_reg_cents=refurb_reg_cents,
        refurb_sale_cents=refurb_sale_cents,
        true_msrp_cents=true_msrp_cents,
        msrp_source="new_catalog",
        is_on_stock=is_on_stock,
        available_quantity=available_quantity,
    )


def test_sub_50_msrp_alert_and_cooldown(tmp_path: Path) -> None:
    db = TrackerDatabase(tmp_path / "alerts.db")
    settings = TrackerSettings(global_discount_threshold_pct=50.0, alert_cooldown_minutes=360)

    # $431.95 on $899.95 MSRP is 52.0% off (48.0% of MSRP -> triggers SUB_50_MSRP)
    curr = _make_snapshot(refurb_sale_cents=43195, true_msrp_cents=89995)
    events = evaluate_variant_alerts(
        previous=None,
        current=curr,
        settings=settings,
        watchlist_by_sku={},
        db=db,
    )
    assert len(events) == 1
    assert events[0].event_type == "SUB_50_MSRP"
    assert events[0].discount_pct_vs_msrp == 52.0

    # Log the alert and verify cooldown suppresses duplicate alert at the same price
    db.log_alert(events[0])
    events_repeat = evaluate_variant_alerts(
        previous=curr,
        current=curr,
        settings=settings,
        watchlist_by_sku={},
        db=db,
    )
    assert len(events_repeat) == 0

    # If the price drops further (e.g. to $399.95), cooldown should NOT suppress the new price
    curr_lower = _make_snapshot(refurb_sale_cents=39995, true_msrp_cents=89995)
    events_lower = evaluate_variant_alerts(
        previous=curr,
        current=curr_lower,
        settings=settings,
        watchlist_by_sku={},
        db=db,
    )
    assert len(events_lower) == 1
    assert events_lower[0].effective_price_dollars == 399.95


def test_watchlist_restock_and_target_price() -> None:
    settings = TrackerSettings(global_discount_threshold_pct=55.0)
    watch = WatchlistEntry(
        sku="1761Q",
        slug="z-f-refurbished",
        product_key="1761Q",
        label="Nikon Z f",
        max_price_dollars=1600.00,
        min_discount_pct_msrp=40.0,
        notify_on_restock=True,
        auto_open_browser=True,
    )
    watchlist_map = {"1761Q": watch}

    prev_oos = _make_snapshot(
        sku="1761Q",
        name="Z f",
        refurb_reg_cents=175995,
        refurb_sale_cents=158395,
        true_msrp_cents=199995,
        is_on_stock=False,
        available_quantity=0,
    )
    curr_instock = _make_snapshot(
        sku="1761Q",
        name="Z f",
        refurb_reg_cents=175995,
        refurb_sale_cents=158395,
        true_msrp_cents=199995,
        is_on_stock=True,
        available_quantity=4,
    )

    events = evaluate_variant_alerts(
        previous=prev_oos,
        current=curr_instock,
        settings=settings,
        watchlist_by_sku=watchlist_map,
        db=None,
    )
    event_types = {e.event_type for e in events}
    assert "WATCHLIST_RESTOCK" in event_types
    assert "WATCHLIST_PRICE" in event_types
    assert "SUB_50_MSRP" not in event_types
    assert all(e.auto_open_browser for e in events)


def test_unknown_msrp_not_cached_and_no_triple_alert(tmp_path: Path) -> None:
    db = TrackerDatabase(tmp_path / "msrp_cache.db")
    snap_unknown = VariantSnapshot(
        sku="1695Q",
        product_key="1695Q",
        name="Z 8 — Body Only",
        slug="z-8-refurbished",
        product_url="https://www.nikonusa.com/p/z-8-refurbished/1695Q/overview",
        refurb_reg_cents=273235,
        refurb_sale_cents=199995,
        true_msrp_cents=None,
        msrp_source="unknown",
        is_on_stock=True,
        available_quantity=2,
    )
    db.upsert_variant(snap_unknown)
    # Unknown MSRP must NOT return a cache hit
    assert db.get_cached_msrp("1695Q") is None

    # Once MSRP is resolved, it should be cached
    snap_resolved = snap_unknown.model_copy(
        update={"true_msrp_cents": 429995, "msrp_source": "new_catalog"}
    )
    db.upsert_variant(snap_resolved)
    cached = db.get_cached_msrp("1695q")  # case-insensitive
    assert cached is not None
    assert cached[0] == 429995

    # Watchlisted item at >50% off MSRP should fire WATCHLIST_PRICE but not duplicate SUB_50_MSRP
    settings = TrackerSettings(global_discount_threshold_pct=50.0)
    watch = WatchlistEntry(
        sku="1695Q",
        slug="z-8-refurbished",
        product_key="1695Q",
        label="Nikon Z 8",
        min_discount_pct_msrp=50.0,
        notify_on_restock=True,
    )
    events = evaluate_variant_alerts(
        previous=None,
        current=snap_resolved,
        settings=settings,
        watchlist_by_sku={"1695Q": watch},
        db=db,
    )
    assert [e.event_type for e in events] == ["WATCHLIST_PRICE"]

