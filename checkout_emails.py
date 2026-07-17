"""HelloFresh CheckoutEmail MongoDB helpers (US + CA)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import ASCENDING, ReturnDocument
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

from market import get_market
from vouchers import get_client

HF_DB = "HelloFresh"

# williamturner19978+{tag}@gmail.com
EMAIL_RE = re.compile(
    r"^williamturner19978\+[A-Za-z0-9._%+-]+@gmail\.com$",
    re.IGNORECASE,
)

CLAIM_TTL = timedelta(minutes=10)
SKIP_CLAIM_TTL = timedelta(minutes=15)


def is_allowed_email(email: str) -> bool:
    return bool(EMAIL_RE.match((email or "").strip()))


def checkout_collection(market: str = "US") -> Collection:
    mkt = get_market(market)
    col_name = mkt["collections"]["checkout"]
    col = get_client()[HF_DB][col_name]
    col.create_index([("email", ASCENDING)], unique=True)
    col.create_index([("loggedCheck", ASCENDING)])
    col.create_index([("processing", ASCENDING)])
    col.create_index([("SkippedWeeks", ASCENDING)])
    return col


def email_exists(email: str, *, market: str = "US") -> bool:
    target = (email or "").strip().lower()
    if not target:
        return False
    return (
        checkout_collection(market).find_one({"email": target}, {"_id": 1}) is not None
    )


def save_checkout_email(email: str, *, market: str = "US") -> dict[str, Any]:
    """Insert CheckoutEmail only if the email is new for that market.

    US → HelloFresh.CheckoutEmail
    CA → HelloFresh.CheckoutEmailCA
    """
    mkt = get_market(market)
    target = (email or "").strip().lower()
    now = datetime.now(timezone.utc)
    col = checkout_collection(mkt["code"])

    existing = col.find_one({"email": target})
    if existing:
        return {
            "email": existing.get("email"),
            "loggedCheck": bool(existing.get("loggedCheck")),
            "_id": str(existing.get("_id")) if existing.get("_id") else None,
            "inserted": False,
            "exists": True,
            "market": mkt["code"],
            "collection": mkt["collections"]["checkout"],
        }

    doc = {
        "email": target,
        "market": mkt["code"],
        "loggedCheck": False,
        "processing": False,
        "SkippedWeeks": False,
        "created_at": now,
        "updated_at": now,
    }
    try:
        result = col.insert_one(doc)
        return {
            "email": target,
            "loggedCheck": False,
            "_id": str(result.inserted_id),
            "inserted": True,
            "exists": False,
            "market": mkt["code"],
            "collection": mkt["collections"]["checkout"],
        }
    except DuplicateKeyError:
        # Race: another writer inserted the same email
        existing = col.find_one({"email": target}) or {"email": target}
        return {
            "email": existing.get("email"),
            "loggedCheck": bool(existing.get("loggedCheck")),
            "_id": str(existing.get("_id")) if existing.get("_id") else None,
            "inserted": False,
            "exists": True,
            "market": mkt["code"],
            "collection": mkt["collections"]["checkout"],
        }


def claim_pending_email(*, market: str = "US") -> dict[str, Any] | None:
    """Atomically claim one CheckoutEmail that still needs loggedCheck."""
    now = datetime.now(timezone.utc)
    stale = now - CLAIM_TTL
    col = checkout_collection(market)
    return col.find_one_and_update(
        {
            "loggedCheck": {"$ne": True},
            "$or": [
                {"processing": {"$ne": True}},
                {"processing_at": {"$lt": stale}},
                {"processing_at": {"$exists": False}},
            ],
        },
        {
            "$set": {
                "processing": True,
                "processing_at": now,
                "updated_at": now,
            }
        },
        sort=[("created_at", ASCENDING)],
        return_document=ReturnDocument.AFTER,
    )


def release_claim(email: str, *, error: str | None = None, market: str = "US") -> None:
    now = datetime.now(timezone.utc)
    fields: dict[str, Any] = {
        "processing": False,
        "updated_at": now,
    }
    if error:
        fields["last_error"] = str(error)[:500]
    checkout_collection(market).update_one(
        {"email": (email or "").strip().lower()},
        {"$set": fields},
    )


def mark_logged_check(
    email: str,
    *,
    referral_link: str,
    customer_uuid: str | None = None,
    first_name: str | None = None,
    invite_link_code: str | None = None,
    discount_voucher: str | None = None,
    market: str = "US",
) -> None:
    now = datetime.now(timezone.utc)
    checkout_collection(market).update_one(
        {"email": (email or "").strip().lower()},
        {
            "$set": {
                "loggedCheck": True,
                "badAccount": False,
                "processing": False,
                "referral_link": referral_link,
                "customer_uuid": customer_uuid,
                "first_name": first_name,
                "invite_link_code": invite_link_code,
                "discount_voucher": discount_voucher,
                "logged_at": now,
                "updated_at": now,
                "last_error": None,
            }
        },
    )


def mark_bad_account(
    email: str, *, error: str | None = None, market: str = "US"
) -> None:
    """Mark account done (loggedCheck) so workers never retry — no HF login link / bad acc."""
    now = datetime.now(timezone.utc)
    checkout_collection(market).update_one(
        {"email": (email or "").strip().lower()},
        {
            "$set": {
                "loggedCheck": True,
                "badAccount": True,
                "processing": False,
                "referral_link": None,
                "logged_at": now,
                "updated_at": now,
                "last_error": (str(error)[:500] if error else "bad_account"),
            }
        },
    )

def claim_pending_skip_email(*, market: str = "US") -> dict[str, Any] | None:
    """Claim one email needing skip after referral worker (loggedCheck=True).

    Requires loggedCheck=True, SkippedWeeks != True, badAccount != True,
    skipFailed != True (permanent skip-backup failure after max attempts).
    """
    now = datetime.now(timezone.utc)
    stale = now - SKIP_CLAIM_TTL
    col = checkout_collection(market)
    return col.find_one_and_update(
        {
            "loggedCheck": True,
            "SkippedWeeks": {"$ne": True},
            "badAccount": {"$ne": True},
            "skipFailed": {"$ne": True},
            "$or": [
                {"skip_processing": {"$ne": True}},
                {"skip_processing_at": {"$lt": stale}},
                {"skip_processing_at": {"$exists": False}},
            ],
        },
        {
            "$set": {
                "skip_processing": True,
                "skip_processing_at": now,
                "updated_at": now,
            }
        },
        sort=[("created_at", ASCENDING)],
        return_document=ReturnDocument.AFTER,
    )


def release_skip_claim(
    email: str, *, error: str | None = None, market: str = "US"
) -> None:
    now = datetime.now(timezone.utc)
    fields: dict[str, Any] = {
        "skip_processing": False,
        "updated_at": now,
    }
    if error:
        fields["last_skip_error"] = str(error)[:500]
    checkout_collection(market).update_one(
        {"email": (email or "").strip().lower()},
        {"$set": fields},
    )


def mark_skipped_weeks(
    email: str,
    *,
    market: str = "US",
    kept_week: str | int | None = None,
    paused_weeks: list[str] | None = None,
    subscription_id: int | None = None,
) -> None:
    """Set SkippedWeeks=True, skip_processing=False, store skip metadata + skipped_at."""
    now = datetime.now(timezone.utc)
    checkout_collection(market).update_one(
        {"email": (email or "").strip().lower()},
        {
            "$set": {
                "SkippedWeeks": True,
                "skipFailed": False,
                "skip_processing": False,
                "skipped_at": now,
                "updated_at": now,
                "last_skip_error": None,
                "skip_kept_week": kept_week,
                "skip_paused_weeks": paused_weeks or [],
                "skip_subscription_id": subscription_id,
            }
        },
    )


def mark_skip_failed(
    email: str, *, error: str | None = None, market: str = "US"
) -> None:
    """Permanent skip-backup failure — never claim again for skip weeks."""
    now = datetime.now(timezone.utc)
    checkout_collection(market).update_one(
        {"email": (email or "").strip().lower()},
        {
            "$set": {
                "skipFailed": True,
                "SkippedWeeks": False,
                "skip_processing": False,
                "skip_failed_at": now,
                "updated_at": now,
                "last_skip_error": (
                    str(error)[:500] if error else "skip_backup_failed"
                ),
            }
        },
    )
