"""NikonUSA Next.js JSON scraper and True New-MSRP Resolver."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from nikon_tracker.database import TrackerDatabase
from nikon_tracker.models import (
    VariantSnapshot,
    WatchlistEntry,
    clean_promo_message,
    strip_refurb_sku,
    strip_refurb_slug,
)

logger = logging.getLogger(__name__)

NIKON_BASE_URL = "https://www.nikonusa.com"
REFURB_CATALOG_PATH = "/refurbished-cameras"
REFURB_DATASOURCE_ID = "b0ba7574-a75f-49f3-98a2-bf26e32de0a6"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

PRODUCT_TYPE_LABELS: dict[str, str] = {
    "pt-mirrorless-cameras": "Mirrorless Cameras",
    "pt-dslr-cameras": "DSLR Cameras",
    "pt-lenses": "NIKKOR Lenses",
    "pt-lenses-nikon1": "NIKKOR Lenses",
    "pt-cinema-cameras": "Cinema Cameras",
    "pt-point-shoot-cameras": "Compact Cameras",
    "pt-binoculars": "Sport Optics",
    "pt-rangefinders": "Sport Optics",
    "pt-fieldscopes": "Sport Optics",
    "pt-flashes": "Flashes & Speedlights",
    "pt-accessories": "Accessories",
}


def parse_nikon_url(url_or_path: str) -> tuple[str, str] | None:
    """Extract (slug, product_key) from a NikonUSA product URL or path.

    Example: 'https://www.nikonusa.com/p/z-f-refurbished/1761Q/overview'
    -> ('z-f-refurbished', '1761Q')
    """
    text = url_or_path.strip()
    if text.startswith("http://") or text.startswith("https://"):
        parsed = urlparse(text)
        path = parsed.path
    else:
        path = text
    m = re.search(r"/p/([^/]+)/([^/?#]+)", path)
    if m:
        return m.group(1), m.group(2)
    return None


def extract_next_data_from_html(html_text: str) -> dict[str, Any]:
    """Extract and parse the <script id="__NEXT_DATA__"> JSON object from HTML."""
    m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html_text,
        re.DOTALL | re.IGNORECASE,
    )
    if not m:
        raise ValueError("Could not locate __NEXT_DATA__ script tag in NikonUSA HTML response.")
    return json.loads(m.group(1))


class NikonUSAClient:
    """Async client for scraping NikonUSA refurbished listings and resolving true New MSRPs."""

    def __init__(
        self,
        db: TrackerDatabase | None = None,
        client: httpx.AsyncClient | None = None,
        max_concurrency: int = 6,
    ) -> None:
        self.db = db
        self._external_client = client
        self.build_id: str | None = None
        self._semaphore = asyncio.Semaphore(max_concurrency)
        # In-memory cache of new product variant MSRPs: {new_sku: (msrp_cents, retail_cents)}
        self._new_sku_msrp_cache: dict[str, tuple[int, int]] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._external_client is not None:
            return self._external_client
        return httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            timeout=httpx.Timeout(15.0, connect=10.0),
            follow_redirects=True,
        )

    async def ensure_build_id(self, client: httpx.AsyncClient, force_refresh: bool = False) -> str:
        """Fetch NikonUSA's current Next.js buildId from HTML if not cached or if invalidated."""
        if self.build_id and not force_refresh:
            return self.build_id

        url = f"{NIKON_BASE_URL}{REFURB_CATALOG_PATH}?limit=99"
        resp = await client.get(url)
        resp.raise_for_status()
        next_data = extract_next_data_from_html(resp.text)
        build_id = next_data.get("buildId")
        if not build_id:
            raise ValueError("NikonUSA __NEXT_DATA__ did not contain a buildId.")
        self.build_id = str(build_id)
        # Opportunistically harvest any new-product MSRPs embedded in __master preloadedValue
        self._harvest_master_msrps(next_data.get("props", {}).get("pageProps", {}))
        return self.build_id

    async def fetch_next_json(
        self, client: httpx.AsyncClient, route_path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Fetch a Next.js /_next/data/{buildId}/en-US/{route_path}.json endpoint with auto-recovery."""
        build_id = await self.ensure_build_id(client)
        clean_route = route_path.lstrip("/")
        if not clean_route.endswith(".json"):
            clean_route = f"{clean_route}.json"

        url = f"{NIKON_BASE_URL}/_next/data/{build_id}/en-US/{clean_route}"
        resp = await client.get(url, params=params)

        if resp.status_code == 404:
            # Could be an expired buildId OR a non-existent product route.
            # Try refreshing buildId once if it looks like a catalog route or if buildId changed.
            new_build_id = await self.ensure_build_id(client, force_refresh=True)
            if new_build_id != build_id:
                retry_url = f"{NIKON_BASE_URL}/_next/data/{new_build_id}/en-US/{clean_route}"
                resp = await client.get(retry_url, params=params)

        resp.raise_for_status()
        data = resp.json()
        page_props = data.get("pageProps", data.get("props", {}).get("pageProps", {}))
        self._harvest_master_msrps(page_props)
        return page_props

    def _harvest_master_msrps(self, page_props: dict[str, Any]) -> None:
        """Harvest any brand-new product prices present in __master dataSourceConfigurations."""
        try:
            dsc_list = (
                page_props.get("data", {})
                .get("pageFolder", {})
                .get("dataSourceConfigurations", [])
            )
            for dsc in dsc_list:
                if dsc.get("dataSourceId") == "__master" and isinstance(
                    dsc.get("preloadedValue"), dict
                ):
                    pv = dsc["preloadedValue"]
                    for item in pv.get("items", []):
                        self._extract_new_product_variants_to_cache(item)
        except Exception:
            pass

    def _extract_new_product_variants_to_cache(self, product: dict[str, Any]) -> None:
        """Store brand-new variant MSRPs and current retail prices into in-memory cache."""
        for var in product.get("variants", []):
            attrs = var.get("attributes", {})
            if attrs.get("isRefurbished"):
                continue
            sku = str(var.get("sku") or "").strip()
            price_obj = var.get("price")
            if not sku or not isinstance(price_obj, dict):
                continue
            retail_cents = price_obj.get("centAmount")
            if not isinstance(retail_cents, int) or retail_cents <= 0:
                continue
            base_cents = (
                price_obj.get("custom", {})
                .get("fields", {})
                .get("basePrice", {})
                .get("centAmount")
            )
            msrp_cents = (
                base_cents
                if isinstance(base_cents, int) and base_cents >= retail_cents
                else retail_cents
            )
            self._new_sku_msrp_cache[sku] = (msrp_cents, retail_cents)

    async def fetch_refurbished_catalog(self) -> list[VariantSnapshot]:
        """Fetch all refurbished products & variants from NikonUSA and resolve their true New MSRPs."""
        own_client = self._external_client is None
        client = await self._get_client()
        try:
            all_products: list[dict[str, Any]] = []
            offset = 0
            limit = 99

            while True:
                try:
                    page_props = await self.fetch_next_json(
                        client,
                        "refurbished-cameras.json",
                        params={"limit": str(limit), "offset": str(offset)},
                    )
                except Exception:
                    # Fallback directly to HTML __NEXT_DATA__ extraction
                    resp = await client.get(
                        f"{NIKON_BASE_URL}{REFURB_CATALOG_PATH}",
                        params={"limit": str(limit), "offset": str(offset)},
                    )
                    resp.raise_for_status()
                    next_data = extract_next_data_from_html(resp.text)
                    self.build_id = str(next_data.get("buildId") or self.build_id or "")
                    page_props = next_data.get("props", {}).get("pageProps", {})

                refurb_ds = self._find_refurb_datasource(page_props)
                items = refurb_ds.get("items", []) if refurb_ds else []
                total = refurb_ds.get("total", len(items)) if refurb_ds else len(items)

                all_products.extend(items)
                offset += len(items)
                if not items or offset >= total:
                    break

            snapshots: list[VariantSnapshot] = []
            seen_skus: set[str] = set()
            for prod in all_products:
                for snap in self._parse_product_variants(prod):
                    if snap.sku not in seen_skus:
                        seen_skus.add(snap.sku)
                        snapshots.append(snap)

            # Resolve true brand-new MSRPs for all snapshots
            await self._resolve_msrps_batch(client, snapshots)
            return snapshots
        finally:
            if own_client:
                await client.aclose()

    async def fetch_watchlist_items(
        self, watchlist: list[WatchlistEntry], catalog_skus: set[str]
    ) -> list[VariantSnapshot]:
        """Fetch individual product pages for watchlisted items not already returned in the in-stock catalog."""
        missing_entries = [
            w for w in watchlist if w.enabled and w.sku.upper() not in catalog_skus
        ]
        if not missing_entries:
            return []

        own_client = self._external_client is None
        client = await self._get_client()
        try:
            # Group by (slug, product_key) to avoid duplicate product page fetches
            by_page: dict[tuple[str, str], list[WatchlistEntry]] = {}
            for w in missing_entries:
                by_page.setdefault((w.slug, w.product_key), []).append(w)

            snapshots: list[VariantSnapshot] = []
            for (slug, product_key), entries in by_page.items():
                prod_snaps = await self._fetch_single_product_page(client, slug, product_key)
                if prod_snaps:
                    snapshots.extend(prod_snaps)

            if snapshots:
                await self._resolve_msrps_batch(client, snapshots)
            return snapshots
        finally:
            if own_client:
                await client.aclose()

    async def fetch_product_by_url_or_slug(
        self, slug: str, product_key: str
    ) -> list[VariantSnapshot]:
        """Public helper to fetch and resolve all variants for a single product page."""
        own_client = self._external_client is None
        client = await self._get_client()
        try:
            snaps = await self._fetch_single_product_page(client, slug, product_key)
            if snaps:
                await self._resolve_msrps_batch(client, snaps)
            return snaps
        finally:
            if own_client:
                await client.aclose()

    async def _fetch_single_product_page(
        self, client: httpx.AsyncClient, slug: str, product_key: str
    ) -> list[VariantSnapshot]:
        """Fetch /_next/data/{buildId}/en-US/p/{slug}/{product_key}/overview.json (or HTML fallback)."""
        route = f"p/{slug}/{product_key}/overview.json"
        page_props: dict[str, Any] | None = None
        async with self._semaphore:
            try:
                page_props = await self.fetch_next_json(client, route)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404 and product_key.endswith(("B", "Q")):
                    # Try alternate refurb suffix (B <-> Q) in case Nikon switched the SKU suffix
                    alt_suffix = "Q" if product_key.endswith("B") else "B"
                    alt_key = product_key[:-1] + alt_suffix
                    try:
                        page_props = await self.fetch_next_json(
                            client, f"p/{slug}/{alt_key}/overview.json"
                        )
                    except Exception:
                        return []
                else:
                    return []
            except Exception:
                # Fallback to HTML page
                try:
                    resp = await client.get(f"{NIKON_BASE_URL}/p/{slug}/{product_key}/overview")
                    if resp.status_code == 200:
                        next_data = extract_next_data_from_html(resp.text)
                        page_props = next_data.get("props", {}).get("pageProps", {})
                except Exception:
                    return []

        if not page_props:
            return []

        ds = page_props.get("data", {}).get("data", {}).get("dataSources", {})
        for val in ds.values():
            if isinstance(val, dict) and isinstance(val.get("product"), dict):
                return self._parse_product_variants(val["product"])
        return []

    async def _resolve_msrps_batch(
        self, client: httpx.AsyncClient, snapshots: list[VariantSnapshot]
    ) -> None:
        """Populate true_msrp_cents and new_retail_cents on every VariantSnapshot."""
        to_lookup_by_page: dict[tuple[str, str], list[VariantSnapshot]] = {}

        for snap in snapshots:
            # 1. Check SQLite cached MSRP (manual override or fresh new_catalog entry)
            if self.db is not None:
                cached = self.db.get_cached_msrp(snap.sku)
                if cached is not None:
                    msrp_cents, retail_cents, source = cached
                    snap.true_msrp_cents = msrp_cents
                    snap.new_retail_cents = retail_cents or snap.new_retail_cents
                    snap.msrp_source = source  # type: ignore[assignment]
                    continue

            # 2. Check in-memory new SKU cache
            if snap.new_sku in self._new_sku_msrp_cache:
                msrp_cents, retail_cents = self._new_sku_msrp_cache[snap.new_sku]
                snap.true_msrp_cents = msrp_cents
                snap.new_retail_cents = retail_cents
                snap.msrp_source = "new_catalog"
                continue

            # 3. Queue brand-new product page lookup
            if snap.new_slug and snap.new_product_key:
                to_lookup_by_page.setdefault(
                    (snap.new_slug, snap.new_product_key), []
                ).append(snap)

        if not to_lookup_by_page:
            return

        async def _lookup_new_page(
            new_slug: str, new_key: str, group: list[VariantSnapshot]
        ) -> None:
            async with self._semaphore:
                route = f"p/{new_slug}/{new_key}/overview.json"
                try:
                    page_props = await self.fetch_next_json(client, route)
                    ds = page_props.get("data", {}).get("data", {}).get("dataSources", {})
                    for val in ds.values():
                        if isinstance(val, dict) and isinstance(val.get("product"), dict):
                            prod = val["product"]
                            self._extract_new_product_variants_to_cache(prod)
                            # If single-variant product and SKU mapping differed slightly, map primary variant
                            variants = prod.get("variants", [])
                            if len(variants) == 1 and len(group) == 1:
                                v0 = variants[0]
                                p0 = v0.get("price") or {}
                                r0 = p0.get("centAmount")
                                b0 = (
                                    p0.get("custom", {})
                                    .get("fields", {})
                                    .get("basePrice", {})
                                    .get("centAmount")
                                )
                                if isinstance(r0, int) and r0 > 0:
                                    m0 = b0 if isinstance(b0, int) and b0 >= r0 else r0
                                    self._new_sku_msrp_cache[group[0].new_sku] = (m0, r0)
                except Exception as exc:
                    logger.debug("Could not resolve new product page %s/%s: %s", new_slug, new_key, exc)

            for snap in group:
                if snap.new_sku in self._new_sku_msrp_cache:
                    msrp_cents, retail_cents = self._new_sku_msrp_cache[snap.new_sku]
                    snap.true_msrp_cents = msrp_cents
                    snap.new_retail_cents = retail_cents
                    snap.msrp_source = "new_catalog"

        await asyncio.gather(
            *(
                _lookup_new_page(nslug, nkey, grp)
                for (nslug, nkey), grp in to_lookup_by_page.items()
            )
        )

    @staticmethod
    def _find_refurb_datasource(page_props: dict[str, Any]) -> dict[str, Any] | None:
        ds = page_props.get("data", {}).get("data", {}).get("dataSources", {})
        if REFURB_DATASOURCE_ID in ds and isinstance(ds[REFURB_DATASOURCE_ID], dict):
            return ds[REFURB_DATASOURCE_ID]
        for val in ds.values():
            if isinstance(val, dict) and val.get("slug") == "/refurbished-cameras":
                return val
        for val in ds.values():
            if isinstance(val, dict) and isinstance(val.get("items"), list):
                items = val["items"]
                if items and any(
                    v.get("attributes", {}).get("isRefurbished")
                    for v in items[0].get("variants", [])
                ):
                    return val
        return None

    @staticmethod
    def _parse_product_variants(prod: dict[str, Any]) -> list[VariantSnapshot]:
        """Convert a NikonUSA Frontastic/Commercetools product dict into VariantSnapshot objects."""
        product_name = str(prod.get("name") or "Unknown Nikon Product").strip()
        product_key = str(prod.get("key") or "").strip()
        slug = str(prod.get("slug") or "").strip()
        product_type_key = str(prod.get("productTypeKey") or "").strip()
        category = PRODUCT_TYPE_LABELS.get(product_type_key, "Refurbished Gear")
        raw_url = str(prod.get("_url") or f"/p/{slug}/{product_key}").strip()
        if not raw_url.endswith("/overview"):
            raw_url = f"{raw_url.rstrip('/')}/overview"
        product_url = (
            raw_url if raw_url.startswith("http") else f"{NIKON_BASE_URL}{raw_url}"
        )

        variants = prod.get("variants") or []
        multi_variant = len(variants) > 1
        results: list[VariantSnapshot] = []

        for var in variants:
            sku = str(var.get("sku") or "").strip()
            price_obj = var.get("price")
            if not sku or not isinstance(price_obj, dict):
                continue

            reg_cents = price_obj.get("centAmount")
            if not isinstance(reg_cents, int) or reg_cents <= 0:
                continue

            disc_obj = var.get("discountedPrice")
            sale_cents = (
                disc_obj.get("centAmount")
                if isinstance(disc_obj, dict) and isinstance(disc_obj.get("centAmount"), int)
                else reg_cents
            )

            custom_fields = price_obj.get("custom", {}).get("fields", {})
            base_cents = custom_fields.get("basePrice", {}).get("centAmount")
            has_refurb_base = isinstance(base_cents, int) and base_cents > reg_cents

            raw_promo = (
                (disc_obj.get("discountedPriceMsg") if isinstance(disc_obj, dict) else None)
                or custom_fields.get("marketingCopy")
                or ""
            )
            promo_message = clean_promo_message(raw_promo)

            attrs = var.get("attributes") or {}
            variant_label = ""
            if multi_variant:
                kit_items = attrs.get("kitItems")
                color = attrs.get("color")
                if isinstance(kit_items, list) and kit_items:
                    variant_label = f"Kit ({', '.join(str(k) for k in kit_items)})"
                elif attrs.get("bodyOnly") is True:
                    variant_label = "Body Only"
                elif isinstance(color, dict) and color.get("label"):
                    variant_label = str(color["label"])
                else:
                    variant_label = f"Option {sku}"

            display_name = (
                f"{product_name} — {variant_label}" if variant_label else product_name
            )
            image_url = str(
                var.get("representativeImage")
                or (var.get("images")[0] if var.get("images") else "")
                or ""
            )

            snap = VariantSnapshot(
                sku=sku,
                new_sku=strip_refurb_sku(sku),
                product_key=product_key or sku,
                new_product_key=strip_refurb_sku(product_key or sku),
                name=display_name,
                variant_label=variant_label,
                slug=slug,
                new_slug=strip_refurb_slug(slug),
                category=category,
                product_type_key=product_type_key,
                product_url=product_url,
                image_url=image_url,
                refurb_reg_cents=reg_cents,
                refurb_sale_cents=sale_cents,
                new_retail_cents=base_cents if has_refurb_base else None,
                true_msrp_cents=base_cents if has_refurb_base else None,
                msrp_source="refurb_base" if has_refurb_base else "unknown",
                promo_message=promo_message,
                is_on_stock=bool(var.get("isOnStock")),
                available_quantity=int(var.get("availableQuantity") or 0),
                add_to_cart_status=str(var.get("addToCartStatus") or "OUT_OF_STOCK"),
            )
            results.append(snap)

        return results
