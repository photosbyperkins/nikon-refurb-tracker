"""SQLite persistence layer for NikonUSA Refurbished Tracker."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nikon_tracker.models import AlertEvent, TrackerSettings, VariantSnapshot, WatchlistEntry


def get_default_db_path() -> Path:
    """Return the default SQLite database path (portable ./data/tracker.db or env override)."""
    env_path = os.environ.get("NIKON_TRACKER_DB")
    if env_path:
        p = Path(env_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    base_dir = Path.cwd() / "data"
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / "tracker.db"


class TrackerDatabase:
    """Thread-safe SQLite database manager for NikonUSA Refurbished Tracker."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else get_default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS products_variants (
                    sku TEXT PRIMARY KEY,
                    new_sku TEXT NOT NULL,
                    product_key TEXT NOT NULL,
                    new_product_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    variant_label TEXT NOT NULL DEFAULT '',
                    slug TEXT NOT NULL,
                    new_slug TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'Refurbished',
                    product_type_key TEXT NOT NULL DEFAULT '',
                    product_url TEXT NOT NULL,
                    image_url TEXT NOT NULL DEFAULT '',
                    refurb_reg_cents INTEGER NOT NULL,
                    refurb_sale_cents INTEGER NOT NULL,
                    new_retail_cents INTEGER,
                    true_msrp_cents INTEGER,
                    msrp_source TEXT NOT NULL DEFAULT 'unknown',
                    msrp_updated_at TEXT,
                    promo_message TEXT NOT NULL DEFAULT '',
                    is_on_stock INTEGER NOT NULL DEFAULT 0,
                    available_quantity INTEGER NOT NULL DEFAULT 0,
                    add_to_cart_status TEXT NOT NULL DEFAULT 'OUT_OF_STOCK',
                    lowest_seen_cents INTEGER,
                    highest_seen_cents INTEGER,
                    last_in_stock_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS price_stock_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sku TEXT NOT NULL,
                    refurb_sale_cents INTEGER NOT NULL,
                    refurb_reg_cents INTEGER NOT NULL,
                    true_msrp_cents INTEGER,
                    is_on_stock INTEGER NOT NULL,
                    available_quantity INTEGER NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_history_sku_time
                    ON price_stock_history(sku, recorded_at);

                CREATE TABLE IF NOT EXISTS watchlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sku TEXT NOT NULL UNIQUE,
                    slug TEXT NOT NULL,
                    product_key TEXT NOT NULL,
                    label TEXT NOT NULL,
                    max_price_dollars REAL,
                    min_discount_pct_msrp REAL DEFAULT 50.0,
                    notify_on_restock INTEGER NOT NULL DEFAULT 1,
                    auto_open_browser INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS alerts_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    sku TEXT NOT NULL,
                    name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    product_url TEXT NOT NULL,
                    image_url TEXT NOT NULL DEFAULT '',
                    effective_price_dollars REAL NOT NULL,
                    msrp_dollars REAL,
                    discount_pct_vs_msrp REAL,
                    available_quantity INTEGER NOT NULL DEFAULT 0,
                    previous_price_dollars REAL,
                    channels_sent TEXT NOT NULL DEFAULT '[]',
                    triggered_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_alerts_sku_time
                    ON alerts_log(sku, event_type, triggered_at);

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            # Seed default watchlist entry if table is completely empty
            cur = conn.execute("SELECT COUNT(*) AS cnt FROM watchlist")
            if cur.fetchone()["cnt"] == 0:
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """
                    INSERT OR IGNORE INTO watchlist
                    (sku, slug, product_key, label, max_price_dollars, min_discount_pct_msrp, notify_on_restock, auto_open_browser, enabled, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "1761Q",
                        "z-f-refurbished",
                        "1761Q",
                        "Nikon Z f Mirrorless Body (Refurbished)",
                        1400.0,
                        30.0,
                        1,
                        0,
                        1,
                        now,
                    ),
                )

    def get_variant(self, sku: str) -> VariantSnapshot | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM products_variants WHERE sku = ?", (sku,)
            ).fetchone()
            return self._row_to_variant(row) if row else None

    def get_all_variants(self) -> list[VariantSnapshot]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM products_variants ORDER BY is_on_stock DESC, refurb_sale_cents ASC"
            ).fetchall()
            return [self._row_to_variant(r) for r in rows]

    def get_cached_msrp(
        self, sku: str, max_age_hours: int = 24
    ) -> tuple[int | None, int | None, str] | None:
        """Return (true_msrp_cents, new_retail_cents, msrp_source) if cached and fresh."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT true_msrp_cents, new_retail_cents, msrp_source, msrp_updated_at FROM products_variants WHERE sku = ?",
                (sku,),
            ).fetchone()
            if not row:
                return None
            source = row["msrp_source"]
            if source == "manual" and row["true_msrp_cents"]:
                return (row["true_msrp_cents"], row["new_retail_cents"], "manual")
            if row["msrp_updated_at"]:
                try:
                    updated = datetime.fromisoformat(row["msrp_updated_at"])
                    if datetime.now(timezone.utc) - updated < timedelta(hours=max_age_hours):
                        return (row["true_msrp_cents"], row["new_retail_cents"], source)
                except ValueError:
                    pass
            return None

    def set_manual_msrp(self, sku: str, msrp_dollars: float) -> VariantSnapshot | None:
        """Allow user to manually set or override the New MSRP for a SKU."""
        msrp_cents = int(round(msrp_dollars * 100))
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE products_variants
                SET true_msrp_cents = ?, msrp_source = 'manual', msrp_updated_at = ?, updated_at = ?
                WHERE sku = ?
                """,
                (msrp_cents, now, now, sku),
            )
        return self.get_variant(sku)

    def upsert_variant(
        self, snapshot: VariantSnapshot
    ) -> tuple[VariantSnapshot | None, VariantSnapshot]:
        """Insert or update a variant snapshot and record price/stock history if changed.

        Returns (previous_snapshot, updated_snapshot).
        """
        previous = self.get_variant(snapshot.sku)
        now = datetime.now(timezone.utc).isoformat()
        snapshot.updated_at = now

        # Preserve manual MSRP override or existing catalog MSRP if new snapshot didn't resolve one
        msrp_updated_at: str | None = now
        if previous:
            if previous.msrp_source == "manual" and previous.true_msrp_cents:
                snapshot.true_msrp_cents = previous.true_msrp_cents
                snapshot.msrp_source = "manual"
            elif not snapshot.true_msrp_cents and previous.true_msrp_cents:
                snapshot.true_msrp_cents = previous.true_msrp_cents
                snapshot.new_retail_cents = snapshot.new_retail_cents or previous.new_retail_cents
                snapshot.msrp_source = previous.msrp_source

            lows = [
                c
                for c in [previous.lowest_seen_cents, snapshot.refurb_sale_cents]
                if c and c > 0
            ]
            highs = [
                c
                for c in [previous.highest_seen_cents, snapshot.refurb_sale_cents]
                if c and c > 0
            ]
            snapshot.lowest_seen_cents = min(lows) if lows else snapshot.refurb_sale_cents
            snapshot.highest_seen_cents = max(highs) if highs else snapshot.refurb_sale_cents
            if snapshot.is_on_stock:
                snapshot.last_in_stock_at = now
            else:
                snapshot.last_in_stock_at = previous.last_in_stock_at
        else:
            snapshot.lowest_seen_cents = snapshot.refurb_sale_cents
            snapshot.highest_seen_cents = snapshot.refurb_sale_cents
            snapshot.last_in_stock_at = now if snapshot.is_on_stock else None

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO products_variants (
                    sku, new_sku, product_key, new_product_key, name, variant_label,
                    slug, new_slug, category, product_type_key, product_url, image_url,
                    refurb_reg_cents, refurb_sale_cents, new_retail_cents, true_msrp_cents,
                    msrp_source, msrp_updated_at, promo_message, is_on_stock,
                    available_quantity, add_to_cart_status, lowest_seen_cents,
                    highest_seen_cents, last_in_stock_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(sku) DO UPDATE SET
                    new_sku = excluded.new_sku,
                    product_key = excluded.product_key,
                    new_product_key = excluded.new_product_key,
                    name = excluded.name,
                    variant_label = excluded.variant_label,
                    slug = excluded.slug,
                    new_slug = excluded.new_slug,
                    category = excluded.category,
                    product_type_key = excluded.product_type_key,
                    product_url = excluded.product_url,
                    image_url = CASE WHEN excluded.image_url != '' THEN excluded.image_url ELSE products_variants.image_url END,
                    refurb_reg_cents = excluded.refurb_reg_cents,
                    refurb_sale_cents = excluded.refurb_sale_cents,
                    new_retail_cents = COALESCE(excluded.new_retail_cents, products_variants.new_retail_cents),
                    true_msrp_cents = COALESCE(excluded.true_msrp_cents, products_variants.true_msrp_cents),
                    msrp_source = excluded.msrp_source,
                    msrp_updated_at = COALESCE(excluded.msrp_updated_at, products_variants.msrp_updated_at),
                    promo_message = excluded.promo_message,
                    is_on_stock = excluded.is_on_stock,
                    available_quantity = excluded.available_quantity,
                    add_to_cart_status = excluded.add_to_cart_status,
                    lowest_seen_cents = excluded.lowest_seen_cents,
                    highest_seen_cents = excluded.highest_seen_cents,
                    last_in_stock_at = excluded.last_in_stock_at,
                    updated_at = excluded.updated_at
                """,
                (
                    snapshot.sku,
                    snapshot.new_sku,
                    snapshot.product_key,
                    snapshot.new_product_key,
                    snapshot.name,
                    snapshot.variant_label,
                    snapshot.slug,
                    snapshot.new_slug,
                    snapshot.category,
                    snapshot.product_type_key,
                    snapshot.product_url,
                    snapshot.image_url,
                    snapshot.refurb_reg_cents,
                    snapshot.refurb_sale_cents,
                    snapshot.new_retail_cents,
                    snapshot.true_msrp_cents,
                    snapshot.msrp_source,
                    msrp_updated_at,
                    snapshot.promo_message,
                    1 if snapshot.is_on_stock else 0,
                    snapshot.available_quantity,
                    snapshot.add_to_cart_status,
                    snapshot.lowest_seen_cents,
                    snapshot.highest_seen_cents,
                    snapshot.last_in_stock_at,
                    snapshot.updated_at,
                ),
            )

            # Record history point if new SKU or if price/stock changed
            should_record = (
                previous is None
                or previous.refurb_sale_cents != snapshot.refurb_sale_cents
                or previous.refurb_reg_cents != snapshot.refurb_reg_cents
                or previous.is_on_stock != snapshot.is_on_stock
                or previous.available_quantity != snapshot.available_quantity
            )
            if should_record:
                conn.execute(
                    """
                    INSERT INTO price_stock_history
                    (sku, refurb_sale_cents, refurb_reg_cents, true_msrp_cents, is_on_stock, available_quantity, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.sku,
                        snapshot.refurb_sale_cents,
                        snapshot.refurb_reg_cents,
                        snapshot.true_msrp_cents,
                        1 if snapshot.is_on_stock else 0,
                        snapshot.available_quantity,
                        now,
                    ),
                )

        return previous, snapshot

    def mark_missing_catalog_items_out_of_stock(self, seen_skus: set[str]) -> None:
        """Mark previously in-stock SKUs that disappeared from the catalog as out of stock."""
        if not seen_skus:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT sku, refurb_sale_cents, refurb_reg_cents, true_msrp_cents FROM products_variants WHERE is_on_stock = 1"
            ).fetchall()
            for r in rows:
                sku = r["sku"]
                if sku not in seen_skus:
                    conn.execute(
                        """
                        UPDATE products_variants
                        SET is_on_stock = 0, available_quantity = 0, add_to_cart_status = 'OUT_OF_STOCK', updated_at = ?
                        WHERE sku = ?
                        """,
                        (now, sku),
                    )
                    conn.execute(
                        """
                        INSERT INTO price_stock_history
                        (sku, refurb_sale_cents, refurb_reg_cents, true_msrp_cents, is_on_stock, available_quantity, recorded_at)
                        VALUES (?, ?, ?, ?, 0, 0, ?)
                        """,
                        (
                            sku,
                            r["refurb_sale_cents"],
                            r["refurb_reg_cents"],
                            r["true_msrp_cents"],
                            now,
                        ),
                    )

    def get_history(self, sku: str, limit: int = 200) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT refurb_sale_cents, refurb_reg_cents, true_msrp_cents, is_on_stock, available_quantity, recorded_at
                FROM price_stock_history
                WHERE sku = ?
                ORDER BY recorded_at DESC
                LIMIT ?
                """,
                (sku, limit),
            ).fetchall()
            points = [
                {
                    "effective_price_dollars": round(r["refurb_sale_cents"] / 100.0, 2),
                    "refurb_reg_dollars": round(r["refurb_reg_cents"] / 100.0, 2),
                    "msrp_dollars": round(r["true_msrp_cents"] / 100.0, 2)
                    if r["true_msrp_cents"]
                    else None,
                    "is_on_stock": bool(r["is_on_stock"]),
                    "available_quantity": r["available_quantity"],
                    "recorded_at": r["recorded_at"],
                }
                for r in reversed(rows)
            ]
            return points

    # --- Watchlist CRUD ---
    def get_watchlist(self) -> list[WatchlistEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM watchlist ORDER BY created_at DESC"
            ).fetchall()
            return [
                WatchlistEntry(
                    id=r["id"],
                    sku=r["sku"],
                    slug=r["slug"],
                    product_key=r["product_key"],
                    label=r["label"],
                    max_price_dollars=r["max_price_dollars"],
                    min_discount_pct_msrp=r["min_discount_pct_msrp"],
                    notify_on_restock=bool(r["notify_on_restock"]),
                    auto_open_browser=bool(r["auto_open_browser"]),
                    enabled=bool(r["enabled"]),
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    def upsert_watchlist(self, entry: WatchlistEntry) -> WatchlistEntry:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO watchlist
                (sku, slug, product_key, label, max_price_dollars, min_discount_pct_msrp, notify_on_restock, auto_open_browser, enabled, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sku) DO UPDATE SET
                    slug = excluded.slug,
                    product_key = excluded.product_key,
                    label = excluded.label,
                    max_price_dollars = excluded.max_price_dollars,
                    min_discount_pct_msrp = excluded.min_discount_pct_msrp,
                    notify_on_restock = excluded.notify_on_restock,
                    auto_open_browser = excluded.auto_open_browser,
                    enabled = excluded.enabled
                """,
                (
                    entry.sku.strip().upper(),
                    entry.slug.strip(),
                    entry.product_key.strip().upper(),
                    entry.label.strip(),
                    entry.max_price_dollars,
                    entry.min_discount_pct_msrp,
                    1 if entry.notify_on_restock else 0,
                    1 if entry.auto_open_browser else 0,
                    1 if entry.enabled else 0,
                    entry.created_at or now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM watchlist WHERE sku = ?", (entry.sku.strip().upper(),)
            ).fetchone()
            return WatchlistEntry(
                id=row["id"],
                sku=row["sku"],
                slug=row["slug"],
                product_key=row["product_key"],
                label=row["label"],
                max_price_dollars=row["max_price_dollars"],
                min_discount_pct_msrp=row["min_discount_pct_msrp"],
                notify_on_restock=bool(row["notify_on_restock"]),
                auto_open_browser=bool(row["auto_open_browser"]),
                enabled=bool(row["enabled"]),
                created_at=row["created_at"],
            )

    def delete_watchlist(self, sku: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM watchlist WHERE sku = ?", (sku.strip().upper(),))
            return cur.rowcount > 0

    # --- Alerts Log & Cooldown ---
    def has_recent_alert(
        self, sku: str, event_type: str, effective_price_dollars: float, cooldown_minutes: int
    ) -> bool:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM alerts_log
                WHERE sku = ? AND event_type = ? AND ABS(effective_price_dollars - ?) < 0.05 AND triggered_at >= ?
                LIMIT 1
                """,
                (sku, event_type, effective_price_dollars, cutoff),
            ).fetchone()
            return row is not None

    def log_alert(self, alert: AlertEvent) -> AlertEvent:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO alerts_log (
                    event_type, sku, name, title, message, product_url, image_url,
                    effective_price_dollars, msrp_dollars, discount_pct_vs_msrp,
                    available_quantity, previous_price_dollars, channels_sent, triggered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert.event_type,
                    alert.sku,
                    alert.name,
                    alert.title,
                    alert.message,
                    alert.product_url,
                    alert.image_url,
                    alert.effective_price_dollars,
                    alert.msrp_dollars,
                    alert.discount_pct_vs_msrp,
                    alert.available_quantity,
                    alert.previous_price_dollars,
                    json.dumps(alert.channels_sent),
                    alert.triggered_at,
                ),
            )
            alert.id = cur.lastrowid
            return alert

    def get_recent_alerts(self, limit: int = 50) -> list[AlertEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM alerts_log ORDER BY triggered_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [
                AlertEvent(
                    id=r["id"],
                    event_type=r["event_type"],
                    sku=r["sku"],
                    name=r["name"],
                    title=r["title"],
                    message=r["message"],
                    product_url=r["product_url"],
                    image_url=r["image_url"] or "",
                    effective_price_dollars=r["effective_price_dollars"],
                    msrp_dollars=r["msrp_dollars"],
                    discount_pct_vs_msrp=r["discount_pct_vs_msrp"],
                    available_quantity=r["available_quantity"],
                    previous_price_dollars=r["previous_price_dollars"],
                    channels_sent=json.loads(r["channels_sent"] or "[]"),
                    triggered_at=r["triggered_at"],
                )
                for r in rows
            ]

    # --- Settings ---
    def get_settings(self) -> TrackerSettings:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = 'tracker_settings'"
            ).fetchone()
            if not row:
                default_settings = TrackerSettings()
                self.save_settings(default_settings)
                return default_settings
            try:
                data = json.loads(row["value"])
                return TrackerSettings(**data)
            except Exception:
                return TrackerSettings()

    def save_settings(self, settings: TrackerSettings) -> TrackerSettings:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO settings (key, value) VALUES ('tracker_settings', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (settings.model_dump_json(),),
            )
        return settings

    @staticmethod
    def _row_to_variant(row: sqlite3.Row) -> VariantSnapshot:
        return VariantSnapshot(
            sku=row["sku"],
            new_sku=row["new_sku"],
            product_key=row["product_key"],
            new_product_key=row["new_product_key"],
            name=row["name"],
            variant_label=row["variant_label"] or "",
            slug=row["slug"],
            new_slug=row["new_slug"],
            category=row["category"] or "Refurbished",
            product_type_key=row["product_type_key"] or "",
            product_url=row["product_url"],
            image_url=row["image_url"] or "",
            refurb_reg_cents=row["refurb_reg_cents"],
            refurb_sale_cents=row["refurb_sale_cents"],
            new_retail_cents=row["new_retail_cents"],
            true_msrp_cents=row["true_msrp_cents"],
            msrp_source=row["msrp_source"] or "unknown",
            promo_message=row["promo_message"] or "",
            is_on_stock=bool(row["is_on_stock"]),
            available_quantity=row["available_quantity"] or 0,
            add_to_cart_status=row["add_to_cart_status"] or "OUT_OF_STOCK",
            lowest_seen_cents=row["lowest_seen_cents"],
            highest_seen_cents=row["highest_seen_cents"],
            last_in_stock_at=row["last_in_stock_at"],
            updated_at=row["updated_at"],
        )
