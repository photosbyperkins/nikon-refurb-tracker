"""Unit tests for NikonUSA Next.js JSON scraper and True New-MSRP Resolver."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from nikon_tracker.database import TrackerDatabase
from nikon_tracker.models import clean_promo_message, strip_refurb_sku, strip_refurb_slug
from nikon_tracker.scraper import (
    NikonUSAClient,
    extract_next_data_from_html,
    parse_nikon_url,
)


def test_strip_helpers() -> None:
    assert strip_refurb_sku("1659B") == "1659"
    assert strip_refurb_sku("1784Q") == "1784"
    assert strip_refurb_sku("20097") == "20097"
    assert strip_refurb_slug("z-6ii-refurbished") == "z-6ii"
    assert strip_refurb_slug("nikkor-z-24-70mm-f28-s") == "nikkor-z-24-70mm-f28-s"


def test_clean_promo_message() -> None:
    raw = "RP_{10% Off Select Refurbished Products} {10% Off Select Refurbished Products} {Refurbished Sale} {10% Off Select Refurbished Products}"
    assert clean_promo_message(raw) == "10% Off Select Refurbished Products"
    assert clean_promo_message("Save $450 Compared to Buying New") == "Save $450 Compared to Buying New"
    assert clean_promo_message(None) == ""


def test_parse_nikon_url() -> None:
    assert parse_nikon_url("https://www.nikonusa.com/p/z-f-refurbished/1761Q/overview") == (
        "z-f-refurbished",
        "1761Q",
    )
    assert parse_nikon_url("/p/af-s-dx-nikkor-10-24mm-f35-45g-ed-refurbished/2181B") == (
        "af-s-dx-nikkor-10-24mm-f35-45g-ed-refurbished",
        "2181B",
    )
    assert parse_nikon_url("invalid-string") is None


@pytest.mark.asyncio
async def test_scraper_catalog_and_true_msrp_resolver(tmp_path: Path) -> None:
    """Verify catalog scraping, buildId recovery, and True New-MSRP resolution."""
    db = TrackerDatabase(tmp_path / "test.db")

    sample_catalog_payload = {
        "pageProps": {
            "data": {
                "data": {
                    "dataSources": {
                        "b0ba7574-a75f-49f3-98a2-bf26e32de0a6": {
                            "total": 2,
                            "count": 2,
                            "slug": "/refurbished-cameras",
                            "items": [
                                {
                                    "name": "AF-S DX NIKKOR 10-24mm F3.5-4.5G ED",
                                    "key": "2181B",
                                    "slug": "af-s-dx-nikkor-10-24mm-f35-45g-ed-refurbished",
                                    "productTypeKey": "pt-lenses",
                                    "_url": "/p/af-s-dx-nikkor-10-24mm-f35-45g-ed-refurbished/2181B",
                                    "variants": [
                                        {
                                            "id": "1",
                                            "sku": "2181B",
                                            "representativeImage": "https://example.com/2181B.png",
                                            "attributes": {"isRefurbished": True},
                                            "price": {"fractionDigits": 2, "centAmount": 47995, "currencyCode": "USD"},
                                            "discountedPrice": {
                                                "fractionDigits": 2,
                                                "centAmount": 43195,
                                                "currencyCode": "USD",
                                                "discountedPriceMsg": "RP_{10% Off Select Refurbished Products} {Refurbished Sale}",
                                            },
                                            "isOnStock": True,
                                            "availableQuantity": 13,
                                            "addToCartStatus": "ADD_TO_CART",
                                        }
                                    ],
                                },
                                {
                                    "name": "Z 6II",
                                    "key": "1659B",
                                    "slug": "z-6ii-refurbished",
                                    "productTypeKey": "pt-mirrorless-cameras",
                                    "_url": "/p/z-6ii-refurbished/1659B",
                                    "variants": [
                                        {
                                            "id": "1",
                                            "sku": "1659B",
                                            "attributes": {"isRefurbished": True, "bodyOnly": True},
                                            "price": {"fractionDigits": 2, "centAmount": 103995, "currencyCode": "USD"},
                                            "discountedPrice": {
                                                "fractionDigits": 2,
                                                "centAmount": 93595,
                                                "currencyCode": "USD",
                                                "discountedPriceMsg": "RP_{10% Off Select Refurbished Products}",
                                            },
                                            "isOnStock": True,
                                            "availableQuantity": 8,
                                            "addToCartStatus": "ADD_TO_CART",
                                        },
                                        {
                                            "id": "4",
                                            "sku": "1663B",
                                            "attributes": {"isRefurbished": True, "kitItems": ["20072"]},
                                            "price": {
                                                "fractionDigits": 2,
                                                "centAmount": 109995,
                                                "currencyCode": "USD",
                                                "custom": {
                                                    "fields": {
                                                        "basePrice": {"centAmount": 189995},
                                                        "marketingCopy": "Save $800 Compared to Buying New",
                                                    }
                                                },
                                            },
                                            "discountedPrice": {"fractionDigits": 2, "centAmount": 109995},
                                            "isOnStock": True,
                                            "availableQuantity": 396,
                                            "addToCartStatus": "ADD_TO_CART",
                                        },
                                    ],
                                },
                            ],
                        }
                    }
                }
            }
        }
    }

    new_2181_payload = {
        "pageProps": {
            "data": {
                "data": {
                    "dataSources": {
                        "__master": {
                            "product": {
                                "name": "AF-S DX NIKKOR 10-24mm F3.5-4.5G ED",
                                "key": "2181",
                                "variants": [
                                    {
                                        "sku": "2181",
                                        "attributes": {"isRefurbished": False},
                                        "price": {"centAmount": 89995, "currencyCode": "USD"},
                                    }
                                ],
                            }
                        }
                    }
                }
            }
        }
    }

    new_1659_payload = {
        "pageProps": {
            "data": {
                "data": {
                    "dataSources": {
                        "__master": {
                            "product": {
                                "name": "Z 6II",
                                "key": "1659",
                                "variants": [
                                    {
                                        "sku": "1659",
                                        "attributes": {"isRefurbished": False},
                                        "price": {
                                            "centAmount": 129995,
                                            "custom": {"fields": {"basePrice": {"centAmount": 174995}}},
                                        },
                                    },
                                    {
                                        "sku": "1663",
                                        "attributes": {"isRefurbished": False},
                                        "price": {
                                            "centAmount": 189995,
                                            "custom": {"fields": {"basePrice": {"centAmount": 234995}}},
                                        },
                                    },
                                ],
                            }
                        }
                    }
                }
            }
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/refurbished-cameras":
            html = (
                '<html><body><script id="__NEXT_DATA__" type="application/json">'
                + json.dumps({"buildId": "test-build-123", "props": sample_catalog_payload})
                + "</script></body></html>"
            )
            return httpx.Response(200, text=html)
        if path == "/_next/data/test-build-123/en-US/refurbished-cameras.json":
            return httpx.Response(200, json=sample_catalog_payload)
        if path == "/_next/data/test-build-123/en-US/p/af-s-dx-nikkor-10-24mm-f35-45g-ed/2181/overview.json":
            return httpx.Response(200, json=new_2181_payload)
        if path == "/_next/data/test-build-123/en-US/p/z-6ii/1659/overview.json":
            return httpx.Response(200, json=new_1659_payload)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as mock_http:
        scraper = NikonUSAClient(db=db, client=mock_http)
        snaps = await scraper.fetch_refurbished_catalog()

    assert len(snaps) == 3
    by_sku = {s.sku: s for s in snaps}

    # Check 2181B: $431.95 refurb sale vs $899.95 resolved New MSRP -> 48.0% of MSRP (52.0% off -> sub-50%!)
    s_2181 = by_sku["2181B"]
    assert s_2181.effective_price_dollars == 431.95
    assert s_2181.refurb_reg_dollars == 479.95
    assert s_2181.msrp_dollars == 899.95
    assert s_2181.msrp_source == "new_catalog"
    assert s_2181.is_sub_50_msrp is True
    assert s_2181.discount_pct_vs_msrp == 52.0

    # Check 1663B: $1,099.95 vs $2,349.95 true New MSRP (53.2% off -> sub-50%!)
    s_1663 = by_sku["1663B"]
    assert s_1663.effective_price_dollars == 1099.95
    assert s_1663.msrp_dollars == 2349.95
    assert s_1663.new_retail_dollars == 1899.95
    assert s_1663.is_sub_50_msrp is True
    assert s_1663.discount_pct_vs_msrp == 53.2

    # Check 1659B: $935.95 vs $1,749.95 true New MSRP (46.5% off -> not sub-50%)
    s_1659 = by_sku["1659B"]
    assert s_1659.effective_price_dollars == 935.95
    assert s_1659.msrp_dollars == 1749.95
    assert s_1659.is_sub_50_msrp is False
    assert s_1659.discount_pct_vs_msrp == 46.5


def test_extract_next_data_error() -> None:
    with pytest.raises(ValueError):
        extract_next_data_from_html("<html><body>No script here</body></html>")
