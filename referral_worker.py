"""Referral workers: passwordless login → referral → CheckoutEmail(US/CA) + VoucherCodes(US/CA)."""

from __future__ import annotations

import os
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any

from checkout_emails import (
    MAX_LOGIN_ATTEMPTS,
    claim_pending_email,
    mark_bad_account,
    mark_logged_check,
    mark_skipped_weeks,
    release_claim,
)
from skip_weeks_worker import skip_result_ok
from hf_login import fetch_referral_for_email
from market import get_market
from promo import resolve_share_link_with_retries
from proxies import (
    assign_proxy,
    ensure_market_proxies,
    get_active_proxy,
    load_proxies_at_start,
)
from vouchers import (
    canonical_share_link,
    insert_voucher,
    promo_result_to_doc,
    share_link_key,
)

WORKER_COUNT = 10
IDLE_SLEEP_S = 3.0
# One login try per claim; retries need ≥15m gap (see checkout_emails.LOGIN_RETRY_GAP)
IMAP_MAX_ROUNDS = 45
IMAP_POLL_SECONDS = 2
MARKET_ORDER = ("US", "CA")


def _insert_referral_voucher(result: dict[str, Any], *, market: str) -> str:
    """Resolve share link pricing when possible, then insert into market vouchers."""
    mkt = get_market(market)
    share = result["referral_link"]
    promo: dict[str, Any] | None = None
    try:
        promo = resolve_share_link_with_retries(
            share, max_attempts=3, market=mkt["code"]
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[REFERRAL] resolve failed for {share}: {exc}", flush=True)

    if promo and promo.get("promo_code"):
        doc = promo_result_to_doc(promo)
        if doc:
            doc["source"] = "checkout_referral"
            doc["source_email"] = result.get("email")
            doc["customer_uuid"] = result.get("customer_uuid")
            doc["market"] = mkt["code"]
            doc["pending_pricing"] = False
            return insert_voucher(doc, market=mkt["code"])

    # Fallback: store share link so status workers can price-check it
    code = (
        (promo or {}).get("promo_code")
        or result.get("discount_voucher")
        or result.get("invite_link_code")
    )
    if not code:
        return "skip_no_code"

    now = datetime.now(timezone.utc)
    doc = {
        "promo_code": str(code).strip(),
        "share_link": canonical_share_link(share, market=mkt["code"]),
        "share_link_key": share_link_key(share),
        "market": mkt["code"],
        "active": None,
        "pending_pricing": True,
        "source": "checkout_referral",
        "source_email": result.get("email"),
        "customer_uuid": result.get("customer_uuid"),
        "invite_link_code": result.get("invite_link_code"),
        "discount_voucher": result.get("discount_voucher"),
        "created_at": now,
        "updated_at": now,
    }
    return insert_voucher(doc, market=mkt["code"])


def process_checkout_email(
    doc: dict[str, Any],
    *,
    worker_id: int,
    market: str,
) -> None:
    """One passwordless login try per claim (claim already bumped loginAttempts)."""
    mkt = get_market(market)
    email = (doc.get("email") or "").strip().lower()
    attempt = int(doc.get("loginAttempts") or 1)
    tag = f"[W{worker_id}:{mkt['code']}]"
    print(
        f"{tag} Processing {email} "
        f"(attempt {attempt}/{MAX_LOGIN_ATTEMPTS}, "
        f"lastAttemptEST={doc.get('lastAttemptEST')})",
        flush=True,
    )

    proxy = assign_proxy(market=mkt["code"]) or get_active_proxy()
    try:
        result = fetch_referral_for_email(
            email,
            proxy=proxy,
            market=mkt["code"],
            max_rounds=IMAP_MAX_ROUNDS,
            poll_seconds=IMAP_POLL_SECONDS,
        )
        skipped = result.get("skipped_weeks") or {}
        paused = skipped.get("paused_weeks") or []
        if paused:
            print(
                f"{tag} Paused {len(paused)} delivery week(s) for {email}",
                flush=True,
            )
        print(
            f"{tag} Referral {email} → {result['referral_link']}",
            flush=True,
        )
        mark_logged_check(
            email,
            referral_link=result["referral_link"],
            customer_uuid=result.get("customer_uuid"),
            first_name=result.get("first_name"),
            invite_link_code=result.get("invite_link_code"),
            discount_voucher=result.get("discount_voucher"),
            market=mkt["code"],
        )
        if skip_result_ok(skipped):
            mark_skipped_weeks(
                email,
                market=mkt["code"],
                kept_week=skipped.get("kept_week"),
                paused_weeks=skipped.get("paused_weeks"),
                subscription_id=skipped.get("subscription_id"),
            )
        status = _insert_referral_voucher(result, market=mkt["code"])
        col = mkt["collections"]["vouchers"]
        print(f"{tag} {col} insert={status} for {email}", flush=True)
    except Exception as exc:  # noqa: BLE001
        err_msg = str(exc)
        print(
            f"{tag} attempt {attempt}/{MAX_LOGIN_ATTEMPTS} failed "
            f"for {email}: {err_msg}",
            flush=True,
        )
        if attempt >= MAX_LOGIN_ATTEMPTS:
            mark_bad_account(email, error=err_msg, market=mkt["code"])
            print(
                f"{tag} badAccount after {MAX_LOGIN_ATTEMPTS} failures "
                f"(no further retries): {email}",
                flush=True,
            )
        else:
            release_claim(email, error=err_msg, market=mkt["code"])
            print(
                f"{tag} released — retry after ≥15m "
                f"(attempt {attempt}/{MAX_LOGIN_ATTEMPTS}): {email}",
                flush=True,
            )


def _worker_loop(
    worker_id: int,
    stop_event: threading.Event,
    *,
    market: str,
) -> None:
    mkt = get_market(market)
    tag = f"[W{worker_id}:{mkt['code']}]"
    while not stop_event.is_set():
        doc = None
        try:
            doc = claim_pending_email(market=mkt["code"])
            if not doc:
                stop_event.wait(IDLE_SLEEP_S)
                continue
            process_checkout_email(doc, worker_id=worker_id, market=mkt["code"])
        except Exception as exc:  # noqa: BLE001
            email = (doc or {}).get("email") or "?"
            print(f"{tag} Failed {email}: {exc}", flush=True)
            traceback.print_exc()
            if doc and doc.get("email"):
                # Unexpected error outside retry loop — release so another
                # worker can retry later (still not loggedCheck).
                release_claim(doc["email"], error=str(exc), market=mkt["code"])
            stop_event.wait(2.0)


def _split_worker_counts(total: int) -> dict[str, int]:
    """Split total workers across US/CA (odd extras go to US)."""
    total = max(0, int(total))
    us_env = os.environ.get("REFERRAL_WORKERS_US")
    ca_env = os.environ.get("REFERRAL_WORKERS_CA")
    if us_env is not None or ca_env is not None:
        us_n = int(us_env) if us_env is not None else max(0, total // 2 + total % 2)
        ca_n = int(ca_env) if ca_env is not None else max(0, total - us_n)
        return {"US": max(0, us_n), "CA": max(0, ca_n)}
    us_n = total // 2 + total % 2
    ca_n = total // 2
    return {"US": us_n, "CA": ca_n}


def start_referral_workers(
    *,
    worker_count: int = WORKER_COUNT,
    stop_event: threading.Event | None = None,
    markets: tuple[str, ...] = MARKET_ORDER,
) -> tuple[threading.Event, list[threading.Thread]]:
    """Start US + CA referral workers. Returns (stop_event, threads)."""
    counts = _split_worker_counts(worker_count)
    for mkt_code in markets:
        if counts.get(mkt_code, 0) <= 0:
            continue
        ensure_market_proxies(mkt_code)
        # Keep global pool warm with first market as fallback
        if mkt_code == markets[0]:
            load_proxies_at_start(market=mkt_code)

    evt = stop_event or threading.Event()
    threads: list[threading.Thread] = []
    wid = 0
    for mkt_code in markets:
        n = int(counts.get(mkt_code, 0) or 0)
        for _ in range(n):
            wid += 1
            t = threading.Thread(
                target=_worker_loop,
                args=(wid, evt),
                kwargs={"market": mkt_code},
                name=f"referral-worker-{mkt_code}-{wid}",
                daemon=True,
            )
            t.start()
            threads.append(t)

    summary = ", ".join(f"{m}={counts.get(m, 0)}" for m in markets)
    print(f"[REFERRAL] Started {len(threads)} workers ({summary})", flush=True)
    return evt, threads


if __name__ == "__main__":
    stop, threads = start_referral_workers()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop.set()
        for t in threads:
            t.join(timeout=2)
