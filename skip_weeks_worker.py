"""Skip-weeks backup workers: login → pause later delivery weeks → SkippedWeeks=true."""

from __future__ import annotations

import os
import threading
import time
import traceback
from typing import Any

from checkout_emails import (
    claim_pending_skip_email,
    mark_skip_failed,
    mark_skipped_weeks,
    release_skip_claim,
)
from hf_login import fetch_referral_for_email
from market import get_market
from proxies import assign_proxy, ensure_market_proxies, get_active_proxy

WORKER_COUNT = 10
SCAN_INTERVAL_S = 1800
MAX_LOGIN_ATTEMPTS = 3
IMAP_MAX_ROUNDS = 15
IMAP_POLL_SECONDS = 2
MARKET_ORDER = ("US", "CA")


def skip_result_ok(skipped: dict[str, Any] | None) -> bool:
    """True when skip succeeded or only the first week exists (no failures)."""
    if not skipped:
        return False
    if skipped.get("failed_weeks"):
        return False
    paused = skipped.get("paused_weeks") or []
    kept = skipped.get("kept_week")
    if paused:
        return True
    if kept is not None:
        return True
    return False


def process_skip_email(
    doc: dict[str, Any],
    *,
    worker_id: int,
    market: str,
) -> None:
    mkt = get_market(market)
    email = (doc.get("email") or "").strip().lower()
    tag = f"[SKIPW{worker_id}:{mkt['code']}]"
    print(f"{tag} Processing skip backup for {email}", flush=True)

    last_error: Exception | None = None
    for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
        proxy = assign_proxy(market=mkt["code"]) or get_active_proxy()
        print(
            f"{tag} login attempt {attempt}/{MAX_LOGIN_ATTEMPTS} for {email}",
            flush=True,
        )
        try:
            result = fetch_referral_for_email(
                email,
                proxy=proxy,
                market=mkt["code"],
                max_rounds=IMAP_MAX_ROUNDS,
                poll_seconds=IMAP_POLL_SECONDS,
            )
            skipped = result.get("skipped_weeks") or {}
            if skip_result_ok(skipped):
                mark_skipped_weeks(
                    email,
                    market=mkt["code"],
                    kept_week=skipped.get("kept_week"),
                    paused_weeks=skipped.get("paused_weeks"),
                    subscription_id=skipped.get("subscription_id"),
                )
                paused = skipped.get("paused_weeks") or []
                print(
                    f"{tag} SkippedWeeks=true for {email} "
                    f"(kept={skipped.get('kept_week')}, paused={len(paused)})",
                    flush=True,
                )
                return
            # Login worked but pause incomplete — count as a failed attempt
            err = "skip_weeks_failed_or_incomplete"
            if skipped.get("failed_weeks"):
                err = str(skipped.get("failed_weeks"))[:500]
            last_error = RuntimeError(err)
            print(
                f"{tag} attempt {attempt}/{MAX_LOGIN_ATTEMPTS} incomplete "
                f"for {email}: {err}",
                flush=True,
            )
            if attempt < MAX_LOGIN_ATTEMPTS:
                time.sleep(2.0)
                continue
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            print(
                f"{tag} attempt {attempt}/{MAX_LOGIN_ATTEMPTS} failed for {email}: {exc}",
                flush=True,
            )
            if attempt < MAX_LOGIN_ATTEMPTS:
                time.sleep(2.0)

    err_msg = str(last_error) if last_error else "skip_backup_failed"
    mark_skip_failed(email, error=err_msg, market=mkt["code"])
    print(
        f"{tag} skipFailed=true after {MAX_LOGIN_ATTEMPTS} failures "
        f"(no further skip retries): {email} ({err_msg})",
        flush=True,
    )


def _worker_loop(
    worker_id: int,
    stop_event: threading.Event,
    *,
    market: str,
) -> None:
    mkt = get_market(market)
    tag = f"[SKIPW{worker_id}:{mkt['code']}]"
    while not stop_event.is_set():
        doc = None
        try:
            doc = claim_pending_skip_email(market=mkt["code"])
            if not doc:
                stop_event.wait(SCAN_INTERVAL_S)
                continue
            process_skip_email(doc, worker_id=worker_id, market=mkt["code"])
        except Exception as exc:  # noqa: BLE001
            email = (doc or {}).get("email") or "?"
            print(f"{tag} Failed {email}: {exc}", flush=True)
            traceback.print_exc()
            if doc and doc.get("email"):
                release_skip_claim(
                    doc["email"], error=str(exc), market=mkt["code"]
                )
            stop_event.wait(2.0)


def _split_worker_counts(total: int) -> dict[str, int]:
    """Split total workers across US/CA (odd extras go to US)."""
    total = max(0, int(total))
    us_env = os.environ.get("SKIP_WEEKS_WORKERS_US")
    ca_env = os.environ.get("SKIP_WEEKS_WORKERS_CA")
    if us_env is not None or ca_env is not None:
        us_n = int(us_env) if us_env is not None else max(0, total // 2 + total % 2)
        ca_n = int(ca_env) if ca_env is not None else max(0, total - us_n)
        return {"US": max(0, us_n), "CA": max(0, ca_n)}
    us_n = total // 2 + total % 2
    ca_n = total // 2
    return {"US": us_n, "CA": ca_n}


def start_skip_weeks_workers(
    *,
    worker_count: int = WORKER_COUNT,
    stop_event: threading.Event | None = None,
    markets: tuple[str, ...] = MARKET_ORDER,
) -> tuple[threading.Event, list[threading.Thread]]:
    """Start US + CA skip-weeks backup workers. Returns (stop_event, threads)."""
    counts = _split_worker_counts(worker_count)
    for mkt_code in markets:
        if counts.get(mkt_code, 0) <= 0:
            continue
        n = ensure_market_proxies(mkt_code)
        print(
            f"[SKIPW] Proxies ready for {mkt_code} "
            f"({get_market(mkt_code)['proxy_collection']}, n={n})",
            flush=True,
        )

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
                name=f"skip-weeks-worker-{mkt_code}-{wid}",
                daemon=True,
            )
            t.start()
            threads.append(t)

    summary = ", ".join(f"{m}={counts.get(m, 0)}" for m in markets)
    print(
        f"[SKIPW] Started {len(threads)} backup workers ({summary}), "
        f"scan every {SCAN_INTERVAL_S // 60}m when idle",
        flush=True,
    )
    return evt, threads


if __name__ == "__main__":
    stop, threads = start_skip_weeks_workers()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop.set()
        for t in threads:
            t.join(timeout=2)
