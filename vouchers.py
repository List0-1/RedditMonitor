"""HelloFresh.VoucherCodes MongoDB persistence.

DB: HelloFresh
Collection: VoucherCodes

Stores resolved share-link promo details. Dedupes by share_link and promo_code.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

from proxies import MONGO_URI

VOUCHERS_DB = "HelloFresh"
VOUCHERS_COL = "VoucherCodes"
BEST_COL = "BestVoucherCode"
BEST_DOC_ID = "current"

_client: MongoClient | None = None


def get_client() -> MongoClient:
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=15000)
    return _client


def canonical_share_link(url: str) -> str:
    """Normalize share / landing URLs for dedupe.

    - /gw/share/CODE  -> https://www.hellofresh.com/gw/share/CODE
    - ?c=PROMO        -> https://www.hellofresh.com/pages/meal-kit-delivery?c=PROMO
    """
    from urllib.parse import parse_qs

    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    path = parsed.path.rstrip("/")
    qs = parse_qs(parsed.query)

    if "/gw/share/" in path.lower():
        code = path.rsplit("/", 1)[-1] if path else ""
        if not code:
            return raw
        return urlunparse(
            ("https", "www.hellofresh.com", f"/gw/share/{code}", "", "", "")
        )

    promo = (qs.get("c") or [None])[0]
    if promo:
        return urlunparse(
            (
                "https",
                "www.hellofresh.com",
                "/pages/meal-kit-delivery",
                "",
                f"c={promo}",
                "",
            )
        )

    code = path.rsplit("/", 1)[-1] if path else ""
    if not code:
        return raw
    return urlunparse(("https", "www.hellofresh.com", f"/gw/share/{code}", "", "", ""))


def share_link_key(url: str) -> str:
    """Case-insensitive key for dedupe / skip checks."""
    return canonical_share_link(url).lower()


def vouchers_collection() -> Collection:
    col = get_client()[VOUCHERS_DB][VOUCHERS_COL]
    # Indexes are create-if-missing; never drop the collection or its data.
    col.create_index([("promo_code", ASCENDING)], unique=True, sparse=True)
    col.create_index([("share_link", ASCENDING)], unique=True, sparse=True)
    col.create_index([("share_link_key", ASCENDING)], unique=True, sparse=True)
    col.create_index([("reddit_comment_id", ASCENDING)], sparse=True)
    return col


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def is_bad_voucher(doc: dict[str, Any] | None) -> bool:
    """True for inactive or no usable free-box offer.

    Bad when:
      - active is False, OR
      - max_free_meals is missing/0, OR
      - servings_at_max is missing/0

    Bad docs stay in VoucherCodes and are skipped on each scan run.
    """
    if not doc:
        return True
    if doc.get("active") is False:
        return True
    meals = _num(doc.get("max_free_meals"), 0.0)
    servings = _num(doc.get("servings_at_max"), 0.0)
    return meals <= 0 or servings <= 0


def load_known() -> dict[str, set[str]]:
    """Preload known share links / promo codes from VoucherCodes.

    Bad vouchers (inactive / zero shipping) stay in Mongo and are listed in
    bad_codes / bad_links so each scan run can skip them without deleting.
    """
    share_links: set[str] = set()
    promo_codes: set[str] = set()
    comment_ids: set[str] = set()
    bad_codes: set[str] = set()
    bad_links: set[str] = set()

    for doc in vouchers_collection().find(
        {},
        {
            "share_link": 1,
            "share_link_key": 1,
            "promo_code": 1,
            "reddit_comment_id": 1,
            "active": 1,
            "max_free_meals": 1,
            "servings_at_max": 1,
        },
    ):
        link = doc.get("share_link")
        key = doc.get("share_link_key") or (share_link_key(link) if link else "")
        if key:
            share_links.add(key)
        if link:
            share_links.add(share_link_key(link))
        code = str(doc.get("promo_code") or "").strip()
        if code:
            promo_codes.add(code)
        cid = doc.get("reddit_comment_id")
        if cid:
            comment_ids.add(str(cid))
        if is_bad_voucher(doc):
            if code:
                bad_codes.add(code)
            if key:
                bad_links.add(key)

    return {
        "share_links": share_links,
        "promo_codes": promo_codes,
        "comment_ids": comment_ids,
        "bad_codes": bad_codes,
        "bad_links": bad_links,
    }


def exists(share_link: str | None = None, promo_code: str | None = None) -> bool:
    """True if share_link and/or promo_code already stored (never mutates DB)."""
    col = vouchers_collection()
    clauses: list[dict[str, Any]] = []
    if share_link:
        canon = canonical_share_link(share_link)
        key = share_link_key(share_link)
        clauses.append({"share_link": canon})
        clauses.append({"share_link_key": key})
        # Legacy docs without share_link_key
        clauses.append({"share_link": {"$regex": f"^{canon}$", "$options": "i"}})
    if promo_code:
        clauses.append({"promo_code": str(promo_code).strip()})
    if not clauses:
        return False
    return col.find_one({"$or": clauses}, {"_id": 1}) is not None


def share_link_exists(share_link: str) -> bool:
    return exists(share_link=share_link)


def inactive_voucher_doc(
    result: dict[str, Any],
    *,
    comment: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build a VoucherCodes doc for a confirmed not-working code (active:false)."""
    code = result.get("promo_code")
    share = result.get("share_url")
    if not code or not share:
        return None

    now = datetime.now(timezone.utc)
    metrics = None
    try:
        from promo import pricing_metrics_from_result

        metrics = pricing_metrics_from_result(result)
    except Exception:  # noqa: BLE001
        metrics = None

    doc: dict[str, Any] = {
        "promo_code": str(code).strip(),
        "share_link": canonical_share_link(share),
        "share_link_key": share_link_key(share),
        "offer": None,
        "active": False,
        "discount_type": None,
        "discount_value": None,
        "channel": None,
        "boxes": {},
        "max_free_meals": (metrics or {}).get("max_free_meals", 0) or 0,
        "servings_at_max": (metrics or {}).get("servings_at_max", 0) or 0,
        "shipping_at_max": (metrics or {}).get("shipping_at_max", 0) or 0,
        "shipping_fee": (metrics or {}).get("shipping_fee", 0) or 0,
        "shipping_discount": (metrics or {}).get("shipping_discount", 0) or 0,
        "free_configs": [],
        "zip_code": result.get("zip_code"),
        "dead_reason": str(result.get("error") or "code_invalid")[:300],
        "updated_at": now,
    }

    if comment:
        doc["reddit_comment_id"] = comment.get("id")
        doc["reddit_author"] = comment.get("author")
        permalink = comment.get("permalink") or ""
        if permalink.startswith("/"):
            permalink = f"https://www.reddit.com{permalink}"
        doc["reddit_permalink"] = permalink or None
        doc["reddit_created_utc"] = comment.get("created_utc")

    return doc


def promo_result_to_doc(
    result: dict[str, Any],
    *,
    comment: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build a VoucherCodes document from resolve_share_link() output.

    Requires API pricing metrics (max_free_meals, servings_at_max, shipping_at_max).
    Returns None when those are missing — caller must not save incomplete data.
    """
    from promo import format_offer_line, pricing_metrics_from_result

    code = result.get("promo_code")
    share = result.get("share_url")
    if not code or not share:
        return None

    metrics = pricing_metrics_from_result(result)
    if not metrics:
        return None

    voucher = result.get("voucher") or {}
    pricing = result.get("box_pricing") or {}

    now = datetime.now(timezone.utc)
    doc: dict[str, Any] = {
        "promo_code": str(code).strip(),
        "share_link": canonical_share_link(share),
        "share_link_key": share_link_key(share),
        "offer": format_offer_line(voucher) if voucher else None,
        "active": voucher.get("is_active"),
        "discount_type": voucher.get("discount_type"),
        "discount_value": voucher.get("discount_value"),
        "channel": voucher.get("channel"),
        "boxes": voucher.get("box_discounts") or {},
        "max_free_meals": metrics["max_free_meals"],
        "servings_at_max": metrics["servings_at_max"],
        "shipping_at_max": metrics["shipping_at_max"],
        "shipping_fee": metrics["shipping_fee"],
        "shipping_discount": metrics["shipping_discount"],
        "free_configs": [
            {
                "meals": r.get("meals"),
                "people": r.get("people"),
                "sku": r.get("sku"),
            }
            for r in (pricing.get("free_configs") or [])
        ],
        "updated_at": now,
    }

    if comment:
        doc["reddit_comment_id"] = comment.get("id")
        doc["reddit_author"] = comment.get("author")
        permalink = comment.get("permalink") or ""
        if permalink.startswith("/"):
            permalink = f"https://www.reddit.com{permalink}"
        doc["reddit_permalink"] = permalink or None
        doc["reddit_created_utc"] = comment.get("created_utc")

    return doc


def insert_voucher(doc: dict[str, Any]) -> str:
    """Insert ONLY if share_link and promo_code are both new.

    Never updates, replaces, or deletes existing VoucherCodes documents.
    """
    col = vouchers_collection()
    share = canonical_share_link(doc.get("share_link") or "")
    code = str(doc.get("promo_code") or "").strip()
    if not share or not code:
        return "skip_invalid"

    # Hard skip — do not touch existing rows
    if exists(share_link=share) or exists(promo_code=code):
        return "skip_exists"

    now = datetime.now(timezone.utc)
    payload = dict(doc)
    payload["share_link"] = share
    payload["share_link_key"] = share_link_key(share)
    payload["promo_code"] = code
    payload.setdefault("created_at", now)
    payload["updated_at"] = now

    try:
        col.insert_one(payload)
        return "inserted"
    except DuplicateKeyError:
        return "skip_exists"


def list_vouchers() -> list[dict[str, Any]]:
    return list(vouchers_collection().find({}))


def update_voucher(promo_code: str, fields: dict[str, Any]) -> None:
    """Partial update only — never writes nulls over existing values, never changes share_link."""
    clean = {
        k: v
        for k, v in fields.items()
        if v is not None and k not in {"share_link", "share_link_key", "_id", "created_at"}
    }
    if not clean:
        return
    clean["updated_at"] = datetime.now(timezone.utc)
    vouchers_collection().update_one(
        {"promo_code": promo_code},
        {"$set": clean},
    )


def delete_voucher(promo_code: str) -> bool:
    result = vouchers_collection().delete_one({"promo_code": promo_code})
    return result.deleted_count > 0


def comparable_snapshot(doc: dict[str, Any]) -> dict[str, Any]:
    """Fields used to detect meaningful updates during status refresh."""
    return {
        "offer": doc.get("offer"),
        "active": doc.get("active"),
        "discount_type": doc.get("discount_type"),
        "discount_value": doc.get("discount_value"),
        "channel": doc.get("channel"),
        "boxes": doc.get("boxes") or {},
        "max_free_meals": doc.get("max_free_meals"),
        "servings_at_max": doc.get("servings_at_max"),
        "shipping_at_max": doc.get("shipping_at_max"),
        "shipping_fee": doc.get("shipping_fee"),
        "shipping_discount": doc.get("shipping_discount"),
        "free_configs": doc.get("free_configs") or [],
    }


def best_voucher_collection() -> Collection:
    return get_client()[VOUCHERS_DB][BEST_COL]


def voucher_rank_key(doc: dict[str, Any]) -> tuple[float, float, float]:
    """Sort key: lowest shipping, then highest meals, then highest servings.

    Priority: shipping_at_max > max_free_meals > servings_at_max
    """
    shipping = _num(doc.get("shipping_at_max"), default=9999.0)
    meals = _num(doc.get("max_free_meals"), default=-1.0)
    servings = _num(doc.get("servings_at_max"), default=-1.0)
    return (shipping, -meals, -servings)


def select_best_active_voucher(
    docs: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Pick best active voucher: min shipping, then max meals, then max servings."""
    pool = docs if docs is not None else list_vouchers()
    active = [d for d in pool if d.get("active") is True]
    if not active:
        return None
    return min(active, key=voucher_rank_key)


def update_best_voucher_code(
    docs: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Select best active voucher and upsert HelloFresh.BestVoucherCode."""
    best = select_best_active_voucher(docs)
    col = best_voucher_collection()
    now = datetime.now(timezone.utc)

    if best is None:
        col.delete_one({"_id": BEST_DOC_ID})
        print("BestVoucherCode: none (no active vouchers)", flush=True)
        return None

    payload = {k: v for k, v in best.items() if k != "_id"}
    payload["selected_at"] = now
    payload["updated_at"] = now
    col.replace_one({"_id": BEST_DOC_ID}, {"_id": BEST_DOC_ID, **payload}, upsert=True)

    print(
        f"BestVoucherCode → {payload.get('promo_code')} | "
        f"shipping_at_max={payload.get('shipping_at_max')} "
        f"max_free_meals={payload.get('max_free_meals')} "
        f"servings_at_max={payload.get('servings_at_max')}",
        flush=True,
    )
    return payload
