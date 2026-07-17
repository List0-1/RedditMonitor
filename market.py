"""Market configs for US / CA HelloFresh pipelines."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

# Classic box SKUs from US plans funnel
US_PRODUCT_IDS = [
    f"US-CBU-{meals}-{people}-0"
    for meals in (2, 3, 4, 5, 6)
    for people in (2, 3, 4, 6)
]

# From fullharcadhf.har prospect/batch productIds (+ extras seen in page)
CA_PRODUCT_IDS = [
    "CA-CBU-1-6-0",
    "CA-CBU-2-2-0",
    "CA-CBU-2-4-0",
    "CA-CBU-2-6-0",
    "CA-CBU-3-2-0",
    "CA-CBU-3-4-0",
    "CA-CBU-3-6-0",
    "CA-CBU-4-2-0",
    "CA-CBU-4-4-0",
    "CA-CBU-4-6-0",
    "CA-CBU-5-2-0",
    "CA-CBU-5-4-0",
    "CA-CBU-5-6-0",
    "CA-CBU-6-2-0",
    "CA-CBU-6-4-0",
]

MARKETS: dict[str, dict[str, Any]] = {
    "US": {
        "code": "US",
        "host": "www.hellofresh.com",
        "origin": "https://www.hellofresh.com",
        "country": "US",
        "locale": "en-US",
        "sku_prefix": "US-CBU",
        "product_ids": US_PRODUCT_IDS,
        "proxy_collection": "Resi_Lightning",
        "fallback_postal": "10001",
        "accept_language": "en-US,en;q=0.9",
        "collections": {
            "vouchers": "VoucherCodes",
            "bad": "BadVoucherCodes",
            "best": "BestVoucherCode",
            "checkout": "CheckoutEmail",
        },
    },
    "CA": {
        "code": "CA",
        "host": "www.hellofresh.ca",
        "origin": "https://www.hellofresh.ca",
        "country": "CA",
        "locale": "en-CA",
        "sku_prefix": "CA-CBU",
        "product_ids": CA_PRODUCT_IDS,
        "proxy_collection": "Resi_LightningCA",
        "fallback_postal": "H3X3S1",  # from fullharcadhf.har
        "accept_language": "en-CA,en;q=0.9",
        "collections": {
            "vouchers": "VoucherCodesCA",
            "bad": "BadVoucherCodesCA",
            "best": "BestVoucherCodeCA",
            "checkout": "CheckoutEmailCA",
        },
    },
}

DEFAULT_MARKET = "US"


def get_market(market: str | None = None) -> dict[str, Any]:
    key = (market or DEFAULT_MARKET).upper()
    if key not in MARKETS:
        raise ValueError(f"Unknown market {market!r}; expected US or CA")
    return MARKETS[key]


def detect_market_from_url(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    if host.endswith(".ca") or "hellofresh.ca" in host:
        return "CA"
    return "US"


def product_ids_for_market(market: str | None = None) -> list[str]:
    return list(get_market(market)["product_ids"])
