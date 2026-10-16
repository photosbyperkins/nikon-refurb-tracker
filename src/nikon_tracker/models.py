"""Data models for NikonUSA Refurbished Tracker."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field


def resolve_package_path(relative_subpath: str) -> Path:
    """Resolve package resource path across normal Python and PyInstaller --onefile bundles."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / "nikon_tracker" / relative_subpath
        if candidate.exists():
            return candidate
    return Path(__file__).parent / relative_subpath


def strip_refurb_sku(sku: str) -> str:
    """Strip trailing B or Q refurbished suffix from a NikonUSA SKU/key."""
    if not sku:
        return ""
    return re.sub(r"[BQ]$", "", sku.strip().upper())


def strip_refurb_slug(slug: str) -> str:
    """Strip trailing -refurbished from a NikonUSA product slug."""
    if not slug:
        return ""
    return re.sub(r"-refurbished$", "", slug.strip())


def clean_promo_message(raw_msg: str | None) -> str:
    """Clean Frontastic RP_{...} promotional strings into human-readable text."""
    if not raw_msg:
        return ""
    if raw_msg.startswith("RP_"):
        matches = re.findall(r"\{([^}]+)\}", raw_msg)
        if matches:
            # Deduplicate while preserving order
            seen: list[str] = []
            for m in matches:
                if m not in seen and m.lower() != "refurbished sale":
                    seen.append(m)
            return " • ".join(seen) if seen else matches[0]
    return raw_msg.strip()


class VariantSnapshot(BaseModel):
    """Represents the live or stored state of a single NikonUSA refurbished SKU variant."""

    sku: str
    new_sku: str = ""
    product_key: str
    new_product_key: str = ""
    name: str
    variant_label: str = ""
    slug: str
    new_slug: str = ""
    category: str = "Refurbished"
    product_type_key: str = ""
    product_url: str
    image_url: str = ""

    # Prices in integer cents (USD)
    refurb_reg_cents: int = Field(description="Regular refurbished price in cents")
    refurb_sale_cents: int = Field(description="Current effective refurbished price in cents")
    new_retail_cents: int | None = Field(
        default=None, description="Current brand-new retail price on NikonUSA in cents"
    )
    true_msrp_cents: int | None = Field(
        default=None, description="Official brand-new MSRP in cents"
    )
    msrp_source: Literal["new_catalog", "refurb_base", "manual", "unknown"] = "unknown"

    promo_message: str = ""
    is_on_stock: bool = False
    available_quantity: int = 0
    add_to_cart_status: str = "OUT_OF_STOCK"

    lowest_seen_cents: int | None = None
    highest_seen_cents: int | None = None
    last_in_stock_at: str | None = None
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def model_post_init(self, __context: object) -> None:
        self.sku = self.sku.strip().upper()
        self.product_key = self.product_key.strip().upper()
        if not self.new_sku:
            self.new_sku = strip_refurb_sku(self.sku)
        if not self.new_product_key:
            self.new_product_key = strip_refurb_sku(self.product_key)
        if not self.new_slug:
            self.new_slug = strip_refurb_slug(self.slug)

    @computed_field
    @property
    def effective_price_dollars(self) -> float:
        return round(self.refurb_sale_cents / 100.0, 2)

    @computed_field
    @property
    def refurb_reg_dollars(self) -> float:
        return round(self.refurb_reg_cents / 100.0, 2)

    @computed_field
    @property
    def msrp_dollars(self) -> float | None:
        if self.true_msrp_cents and self.true_msrp_cents > 0:
            return round(self.true_msrp_cents / 100.0, 2)
        return None

    @computed_field
    @property
    def new_retail_dollars(self) -> float | None:
        if self.new_retail_cents and self.new_retail_cents > 0:
            return round(self.new_retail_cents / 100.0, 2)
        return None

    @computed_field
    @property
    def savings_vs_msrp_dollars(self) -> float | None:
        if self.true_msrp_cents and self.true_msrp_cents > 0:
            return round((self.true_msrp_cents - self.refurb_sale_cents) / 100.0, 2)
        return None

    @computed_field
    @property
    def pct_of_msrp(self) -> float | None:
        """Current refurbished price as a percentage of official New MSRP (e.g., 46.8 means 46.8% of MSRP)."""
        if self.true_msrp_cents and self.true_msrp_cents > 0:
            return round((self.refurb_sale_cents / self.true_msrp_cents) * 100.0, 1)
        return None

    @computed_field
    @property
    def discount_pct_vs_msrp(self) -> float | None:
        """Total discount percentage off official New MSRP (e.g., 53.2 means 53.2% off MSRP)."""
        if self.true_msrp_cents and self.true_msrp_cents > 0:
            return round((1.0 - (self.refurb_sale_cents / self.true_msrp_cents)) * 100.0, 1)
        return None

    @computed_field
    @property
    def discount_pct_vs_refurb_reg(self) -> float:
        """Extra promotional discount percentage off the regular refurbished price."""
        if self.refurb_reg_cents > 0 and self.refurb_sale_cents < self.refurb_reg_cents:
            return round((1.0 - (self.refurb_sale_cents / self.refurb_reg_cents)) * 100.0, 1)
        return 0.0

    @computed_field
    @property
    def is_sub_50_msrp(self) -> bool:
        """True when refurbished sale price is at or below 50% of the official New MSRP."""
        if self.true_msrp_cents and self.true_msrp_cents > 0:
            return self.refurb_sale_cents <= (self.true_msrp_cents * 0.50)
        return False

    @computed_field
    @property
    def is_extra_refurb_sale(self) -> bool:
        """True when an additional refurbished flash sale discount is active below regular refurb price."""
        return self.refurb_sale_cents < self.refurb_reg_cents

    @computed_field
    @property
    def is_all_time_low(self) -> bool:
        """True when current price is at the all-time lowest recorded price and represents a markdown."""
        if not self.lowest_seen_cents or self.lowest_seen_cents <= 0:
            return False
        if self.refurb_sale_cents > self.lowest_seen_cents:
            return False
        has_historical_drop = (
            self.highest_seen_cents is not None
            and self.highest_seen_cents > self.lowest_seen_cents
        )
        return bool(has_historical_drop or self.is_extra_refurb_sale or self.is_sub_50_msrp)

    @computed_field
    @property
    def mount_system(self) -> str:
        """Mount & sensor system classification (Z-Mount FX, Z-Mount DX, F-Mount, Optics & Accessories)."""
        return classify_nikon_mount(self.product_type_key, self.slug, self.name)


class WatchlistEntry(BaseModel):
    """User-configured product/SKU watchlist item with custom alert rules."""

    id: int | None = None
    sku: str = Field(description="Refurbished SKU (e.g. 1761Q, 1695B, 1659B)")
    slug: str = Field(description="Refurbished product slug (e.g. z-f-refurbished)")
    product_key: str = Field(description="Refurbished product key (e.g. 1761Q)")
    label: str = Field(description="Display name for the watchlisted item")
    max_price_dollars: float | None = Field(
        default=None, description="Alert when price is at or below this USD amount"
    )
    min_discount_pct_msrp: float | None = Field(
        default=50.0, description="Alert when % off MSRP is at or above this threshold"
    )
    notify_on_restock: bool = Field(
        default=True, description="Alert immediately when stock transitions from 0 to >0"
    )
    notify_on_price_drop: bool = Field(
        default=True,
        description="Alert whenever in-stock price drops below its previous recorded price",
    )
    auto_open_browser: bool = Field(
        default=False, description="Automatically open NikonUSA product page in browser on hit"
    )
    enabled: bool = True
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class AlertEvent(BaseModel):
    """Triggered alert event ready for multi-channel dispatch and logging."""

    id: int | None = None
    event_type: Literal[
        "SUB_50_MSRP",
        "FLASH_SALE_DROP",
        "WATCHLIST_PRICE",
        "WATCHLIST_PRICE_DROP",
        "WATCHLIST_RESTOCK",
        "TEST_ALERT",
    ]
    sku: str
    name: str
    title: str
    message: str
    product_url: str
    image_url: str = ""
    effective_price_dollars: float
    msrp_dollars: float | None = None
    discount_pct_vs_msrp: float | None = None
    available_quantity: int = 0
    previous_price_dollars: float | None = None
    auto_open_browser: bool = False
    channels_sent: list[str] = Field(default_factory=list)
    triggered_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class TrackerSettings(BaseModel):
    """Global tracker & notification settings persisted in SQLite."""

    poll_interval_seconds: int = Field(default=60, ge=15, le=3600)
    global_discount_threshold_pct: float = Field(
        default=50.0,
        ge=5.0,
        le=95.0,
        description="Trigger hot-deal alerts when refurb price is at least this % off New MSRP (50% = <= 50% of MSRP)",
    )
    alert_on_any_extra_refurb_sale: bool = Field(
        default=True,
        description="Alert when a new refurb flash sale drops an in-stock item's price by >= 5%",
    )
    alert_cooldown_minutes: int = Field(
        default=360,
        ge=1,
        description="Minimum minutes before re-alerting on the exact same SKU at the same price",
    )

    # Desktop notifications
    desktop_notifications_enabled: bool = True
    desktop_sound_enabled: bool = True
    auto_open_browser_on_sub50: bool = False

    # ntfy.sh mobile push
    ntfy_enabled: bool = False
    ntfy_server_url: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str = ""

    # Discord webhook
    discord_enabled: bool = False
    discord_webhook_url: str = ""

    # Telegram bot
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Pushover
    pushover_enabled: bool = False
    pushover_user_key: str = ""
    pushover_api_token: str = ""


# Built-in presets for high-demand Nikon refurbished cameras & lenses (including frequently out-of-stock items)
POPULAR_NIKON_PRESETS: list[dict[str, object]] = [
    {
        "sku": "1761Q",
        "product_key": "1761Q",
        "slug": "z-f-refurbished",
        "label": "Nikon Z f Mirrorless Body (Refurbished)",
        "msrp_dollars": 1999.95,
    },
    {
        "sku": "1695Q",
        "product_key": "1695Q",
        "slug": "z-8-refurbished",
        "label": "Nikon Z 8 Mirrorless Body (Refurbished)",
        "msrp_dollars": 3999.95,
    },
    {
        "sku": "1695B",
        "product_key": "1695B",
        "slug": "z-8-refurbished",
        "label": "Nikon Z 8 Mirrorless Body [B-SKU] (Refurbished)",
        "msrp_dollars": 3999.95,
    },
    {
        "sku": "1890Q",
        "product_key": "1890Q",
        "slug": "z6iii-refurbished",
        "label": "Nikon Z6III Mirrorless Body (Refurbished)",
        "msrp_dollars": 2499.95,
    },
    {
        "sku": "1659B",
        "product_key": "1659B",
        "slug": "z-6ii-refurbished",
        "label": "Nikon Z 6II Mirrorless Body (Refurbished)",
        "msrp_dollars": 1749.95,
    },
    {
        "sku": "1663B",
        "product_key": "1659B",
        "slug": "z-6ii-refurbished",
        "label": "Nikon Z 6II + NIKKOR Z 24-70mm f/4 S Kit (Refurbished)",
        "msrp_dollars": 2349.95,
    },
    {
        "sku": "1784Q",
        "product_key": "1784Q",
        "slug": "z50ii-refurbished",
        "label": "Nikon Z50II Mirrorless Body (Refurbished)",
        "msrp_dollars": 1009.95,
    },
    {
        "sku": "20089B",
        "product_key": "20089B",
        "slug": "nikkor-z-24-70mm-f28-s-refurbished",
        "label": "NIKKOR Z 24-70mm f/2.8 S (Refurbished)",
        "msrp_dollars": 2399.95,
    },
    {
        "sku": "20091B",
        "product_key": "20091B",
        "slug": "nikkor-z-70-200mm-f28-vr-s-refurbished",
        "label": "NIKKOR Z 70-200mm f/2.8 VR S (Refurbished)",
        "msrp_dollars": 2699.95,
    },
    {
        "sku": "20105B",
        "product_key": "20105B",
        "slug": "nikkor-z-24-120mm-f4-s-refurbished",
        "label": "NIKKOR Z 24-120mm f/4 S (Refurbished)",
        "msrp_dollars": 1149.95,
    },
    {
        "sku": "20117Q",
        "product_key": "20117Q",
        "slug": "nikkor-z-180-600mm-f56-63-vr-refurbished",
        "label": "NIKKOR Z 180-600mm f/5.6-6.3 VR (Refurbished)",
        "msrp_dollars": 1899.95,
    },
]

CATALOG_CATEGORIES: list[str] = [
    "Mirrorless Cameras (Z Series)",
    "NIKKOR Z Lenses & Teleconverters",
    "Cinema Cameras (Z Cinema)",
    "Adapters & Accessories (FTZ)",
    "DSLR Cameras",
    "F-Mount DSLR Lenses (AF-S / AF-P / PC)",
    "Flashes & Speedlights",
    "Compact & Superzoom Cameras (COOLPIX)",
    "Sport Optics (Binoculars, Rangefinders, Fieldscopes)",
    "Legacy AF / Manual F-Mount Lenses",
]

def classify_nikon_category(product_type_key: str, slug: str) -> str:
    """Map NikonUSA productTypeKey and slug into unified human-readable categories."""
    p_type = (product_type_key or "").strip().lower()
    s = (slug or "").strip().lower()
    if p_type == "pt-mirrorless-cameras":
        return "Mirrorless Cameras (Z Series)"
    if p_type in ("pt-cinemacam", "pt-cinema-cameras"):
        return "Cinema Cameras (Z Cinema)"
    if p_type in ("pt-lenses", "pt-lenses-nikon1"):
        if "nikkor-z" in s or "z-teleconverter" in s:
            return "NIKKOR Z Lenses & Teleconverters"
        if s.startswith(("af-s-", "af-p-", "pc-")):
            return "F-Mount DSLR Lenses (AF-S / AF-P / PC)"
        return "Legacy AF / Manual F-Mount Lenses"
    if p_type in ("pt-slr-cameras", "pt-dslr-cameras"):
        return "DSLR Cameras"
    if p_type == "pt-accessories":
        return "Adapters & Accessories (FTZ)"
    if p_type == "pt-flashes":
        return "Flashes & Speedlights"
    if p_type in ("pt-digital-cameras", "pt-point-shoot-cameras"):
        return "Compact & Superzoom Cameras (COOLPIX)"
    if p_type.startswith("pt-sport-optics") or p_type in (
        "pt-binoculars",
        "pt-rangefinders",
        "pt-fieldscopes",
    ):
        return "Sport Optics (Binoculars, Rangefinders, Fieldscopes)"
    return "Refurbished Gear"


def classify_nikon_mount(product_type_key: str, slug: str, name: str = "") -> str:
    """Classify a Nikon refurbished product into Z-Mount FX, Z-Mount DX, F-Mount, or Optics & Accessories."""
    p_type = (product_type_key or "").strip().lower()
    s = (slug or "").strip().lower()
    n = (name or "").strip().lower()

    # Z-Mount DX bodies & lenses
    is_z_dx_body = any(
        token in s or token in n
        for token in ("z-50", "z50", "z-30", "z30", "z-fc", "zfc")
    )
    is_z_dx_lens = ("nikkor-z-dx" in s) or ("nikkor z dx" in n)
    if is_z_dx_body or is_z_dx_lens:
        return "Z-Mount DX"

    # Z-Mount FX bodies, cinema, lenses, teleconverters, FTZ adapters
    if (
        p_type in ("pt-mirrorless-cameras", "pt-cinemacam", "pt-cinema-cameras")
        or "nikkor-z" in s
        or "nikkor z" in n
        or "z-teleconverter" in s
        or "ftz" in s
        or s.startswith(("z-5", "z5", "z-6", "z6", "z-7", "z7", "z-8", "z8", "z-9", "z9", "z-f", "zf", "zr"))
    ):
        return "Z-Mount FX"

    # F-Mount DSLRs & F-Mount NIKKOR lenses
    if (
        p_type in ("pt-slr-cameras", "pt-dslr-cameras")
        or s.startswith(("af-s-", "af-p-", "pc-", "af-nikkor", "af-d", "d3", "d5", "d6", "d7", "d8", "d500"))
        or "af-s " in n
        or "af-p " in n
        or "f-mount" in n
    ):
        return "F-Mount"

    return "Optics & Accessories"



_CACHED_CATALOG: list[dict[str, Any]] | None = None


def load_refurbished_catalog() -> list[dict[str, Any]]:
    """Load the full 266-item NikonUSA refurbished catalog (out-of-stock & in-stock)."""
    global _CACHED_CATALOG
    if _CACHED_CATALOG is not None:
        return _CACHED_CATALOG
    catalog_path = resolve_package_path("data/refurbished_catalog.json")
    if catalog_path.exists():
        try:
            _CACHED_CATALOG = json.loads(catalog_path.read_text(encoding="utf-8"))
            return _CACHED_CATALOG
        except Exception:
            pass
    return list(POPULAR_NIKON_PRESETS)

