"""Reddit scan flow:

1) Discover HelloFresh share-codes thread(s) (pinned/stickied)
2) Collect ALL share links + bare promo codes from those threads
3) Resolve/scan them in parallel (default 10 workers)
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from promo import (
    format_promo_result,
    has_required_api_pricing,
    resolve_share_link_with_retries,
)
from reddit_fetch import fetch_comments, find_share_code_threads
from vouchers import (
    canonical_share_link,
    exists,
    insert_voucher,
    is_bad_voucher,
    load_known,
    promo_result_to_doc,
    share_link_exists,
    share_link_key,
)

THREAD_URL = (
    "https://www.reddit.com/r/hellofresh/comments/1uv8bo8/"
    "share_weekly_trial_offer_and_free_box_codes_here/"
)
PROMO_WORKERS = 10


def _landing_url_for_code(code: str) -> str:
    return f"https://www.hellofresh.com/pages/meal-kit-delivery?c={quote(code)}"


def collect_from_threads(
    thread_urls: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Visit threads; return (comments, inventory items).

    Each inventory item: {kind: share|code, value, comment, resolve_url}
    """
    from monitor import walk_comments

    all_comments: list[dict[str, Any]] = []
    inventory: list[dict[str, Any]] = []
    seen_links: set[str] = set()
    seen_codes: set[str] = set()

    for url in thread_urls:
        print(f"\n📡 Collecting from thread: {url}", flush=True)
        comments = walk_comments(fetch_comments(url))
        all_comments.extend(comments)
        for comment in comments:
            for link in comment.get("share_links") or []:
                key = share_link_key(link)
                if key in seen_links:
                    continue
                seen_links.add(key)
                inventory.append(
                    {
                        "kind": "share",
                        "value": link,
                        "resolve_url": link,
                        "comment": comment,
                    }
                )
            for code in comment.get("promo_codes") or []:
                code_u = str(code).strip().upper()
                if not code_u or code_u in seen_codes:
                    continue
                seen_codes.add(code_u)
                inventory.append(
                    {
                        "kind": "code",
                        "value": code_u,
                        "resolve_url": _landing_url_for_code(code_u),
                        "comment": comment,
                    }
                )

    print(
        f"\n📋 Inventory | threads={len(thread_urls)} comments={len(all_comments)} "
        f"share_links={sum(1 for i in inventory if i['kind']=='share')} "
        f"bare_codes={sum(1 for i in inventory if i['kind']=='code')} "
        f"total={len(inventory)}",
        flush=True,
    )
    for i, item in enumerate(inventory, 1):
        print(f"  {i:>3}. [{item['kind']}] {item['value']}", flush=True)
    return all_comments, inventory


def _resolve_one(
    index: int,
    total: int,
    item: dict[str, Any],
    *,
    country: str,
    locale: str,
    known_lock: threading.Lock,
    known: dict[str, set[str]],
) -> dict[str, Any]:
    """Worker: resolve one share link / bare code and insert if new."""
    comment = item["comment"]
    resolve_url = item["resolve_url"]
    kind = item["kind"]
    value = item["value"]
    author = comment.get("author", "?")
    out = {"resolved": 0, "inserted": 0, "skipped_exists": 0, "failed": 0}

    # Skip if already in Mongo
    if kind == "share":
        key = share_link_key(value)
        with known_lock:
            already = key in known["share_links"]
        if already or share_link_exists(value):
            print(f"[{index}/{total}] skip existing share | {canonical_share_link(value)}", flush=True)
            with known_lock:
                known["share_links"].add(key)
            out["skipped_exists"] += 1
            return out
    else:
        with known_lock:
            already = value in known["promo_codes"]
        if already or exists(promo_code=value):
            print(f"[{index}/{total}] skip existing code | {value}", flush=True)
            with known_lock:
                known["promo_codes"].add(value)
            out["skipped_exists"] += 1
            return out

    print(f"[{index}/{total}] {author} | [{kind}] {value}", flush=True)
    promo = resolve_share_link_with_retries(
        resolve_url,
        country=country,
        locale=locale,
        max_attempts=3,
    )
    # Ensure bare-code path still records the known code / a stable URL
    if kind == "code" and not promo.get("promo_code"):
        promo["promo_code"] = value
    if kind == "code" and not promo.get("share_url"):
        promo["share_url"] = resolve_url
    if kind == "share":
        promo.setdefault("share_url", value)

    out["resolved"] += 1
    print(format_promo_result(promo), flush=True)

    code = promo.get("promo_code")
    with known_lock:
        code_known = bool(code and code in known["promo_codes"])
    if code_known:
        print("  → skip save (promo_code already in Mongo)", flush=True)
        out["skipped_exists"] += 1
        with known_lock:
            if kind == "share":
                known["share_links"].add(share_link_key(value))
            if code:
                known["promo_codes"].add(code)
        return out

    if kind == "share" and share_link_exists(value):
        print("  → skip save (share_link already in Mongo)", flush=True)
        out["skipped_exists"] += 1
        return out

    # Incomplete API pricing after retries — do NOT save (unknown good/bad)
    if promo.get("incomplete") or not has_required_api_pricing(promo):
        print(
            "  → skip save (incomplete max_free_meals/servings_at_max/shipping_at_max)",
            flush=True,
        )
        out["failed"] += 1
        return out

    doc = promo_result_to_doc(promo, comment=comment)
    if not doc:
        print("  → skip save (no promo doc / incomplete pricing)", flush=True)
        out["failed"] += 1
        return out

    # Complete API data but bad offer — still save so later runs skip it
    if is_bad_voucher(doc):
        action = insert_voucher(doc)
        if action == "inserted":
            print("  → saved bad code (skip later runs)", flush=True)
            out["inserted"] += 1
        else:
            print(f"  → skip save ({action}) bad/existing", flush=True)
            out["skipped_exists"] += 1
        with known_lock:
            known["promo_codes"].add(doc["promo_code"])
            known.setdefault("bad_codes", set()).add(doc["promo_code"])
            if doc.get("share_link_key"):
                known["share_links"].add(doc["share_link_key"])
                known.setdefault("bad_links", set()).add(doc["share_link_key"])
        return out

    action = insert_voucher(doc)
    if action == "inserted":
        print("  → saved to HelloFresh.VoucherCodes", flush=True)
        out["inserted"] += 1
        with known_lock:
            known["share_links"].add(doc.get("share_link_key") or share_link_key(doc["share_link"]))
            known["promo_codes"].add(doc["promo_code"])
            if doc.get("reddit_comment_id"):
                known["comment_ids"].add(str(doc["reddit_comment_id"]))
    else:
        print(f"  → skip save ({action}) — existing doc left untouched", flush=True)
        out["skipped_exists"] += 1
        with known_lock:
            known["promo_codes"].add(doc["promo_code"])
    return out


def parse_all_links_from_reddit(
    thread_url: str | None = None,
    *,
    country: str = "US",
    locale: str = "en-US",
    workers: int = PROMO_WORKERS,
    subreddit: str = "hellofresh",
) -> dict[str, int]:
    """Phase 1 collect from share-codes threads → Phase 2 scan with N workers."""
    from proxies import begin_proxy_cycle, cycle_ips_used

    begin_proxy_cycle("reddit-scan")

    # --- Preload Mongo (bad codes stay in DB and are skipped this run) ---
    known = load_known()
    print(
        f"Mongo preload | share_links={len(known['share_links'])} "
        f"promo_codes={len(known['promo_codes'])} "
        f"bad_skip={len(known.get('bad_codes') or set())}",
        flush=True,
    )

    # --- Phase 1: discover thread(s) + collect all links/codes ---
    if thread_url:
        thread_urls = [thread_url]
        print(f"[REDDIT] Using provided thread: {thread_url}", flush=True)
    else:
        try:
            threads = find_share_code_threads(subreddit=subreddit)
            thread_urls = [t["url"] for t in threads]
        except Exception as exc:  # noqa: BLE001
            print(f"[REDDIT] Thread discovery failed ({exc}) — fallback", flush=True)
            thread_urls = [THREAD_URL]

    _comments, inventory = collect_from_threads(thread_urls)

    # Skip anything already in Mongo (including bad codes left there on purpose)
    to_resolve: list[dict[str, Any]] = []
    skipped = 0
    skipped_bad = 0
    bad_codes = known.get("bad_codes") or set()
    bad_links = known.get("bad_links") or set()
    for item in inventory:
        if item["kind"] == "share":
            key = share_link_key(item["value"])
            if key in known["share_links"] or share_link_exists(item["value"]):
                skipped += 1
                if key in bad_links:
                    skipped_bad += 1
                known["share_links"].add(key)
                continue
        else:
            code = item["value"]
            if code in known["promo_codes"] or exists(promo_code=code):
                skipped += 1
                if code in bad_codes:
                    skipped_bad += 1
                known["promo_codes"].add(code)
                continue
        to_resolve.append(item)

    print(
        f"\n🔎 To scan: {len(to_resolve)} | already known/skipped: {skipped} "
        f"(bad={skipped_bad}) | workers={workers}",
        flush=True,
    )

    stats = {
        "resolved": 0,
        "inserted": 0,
        "skipped_exists": skipped,
        "failed": 0,
        "skipped_comments": 0,
        "skipped_links": skipped,
        "skipped_bad": skipped_bad,
    }
    if not to_resolve:
        print("Nothing new to resolve.")
        return stats

    # --- Phase 2: parallel promo scan ---
    known_lock = threading.Lock()
    total = len(to_resolve)
    print(f"\n🚀 Scanning {total} item(s) with {workers} workers…", flush=True)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [
            pool.submit(
                _resolve_one,
                i,
                total,
                item,
                country=country,
                locale=locale,
                known_lock=known_lock,
                known=known,
            )
            for i, item in enumerate(to_resolve, 1)
        ]
        for fut in as_completed(futures):
            try:
                delta = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"  worker error: {exc}", flush=True)
                stats["failed"] += 1
                continue
            for k in ("resolved", "inserted", "skipped_exists", "failed"):
                stats[k] += int(delta.get(k) or 0)

    print(
        f"\n{datetime.now(timezone.utc).isoformat()} | "
        f"resolved={stats['resolved']} inserted={stats['inserted']} "
        f"skipped_exists={stats['skipped_exists']} failed={stats['failed']} "
        f"unique_ips={len(cycle_ips_used())}"
    )
    return stats
