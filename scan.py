"""Reddit scan flow:

1) Discover HelloFresh share-codes thread(s) (pinned/stickied)
2) Collect ALL share links + bare promo codes from those threads
3) Classify: .com share → US, .ca share → CA, bare codes → both US and CA
4) Scan USA with 30 threads (US endpoints + Resi_Lightning)
5) Scan CAD with 30 threads (CA endpoints + Resi_LightningCA)
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse

from market import get_market
from promo import (
    format_promo_result,
    has_required_api_pricing,
    is_valid_promo,
    is_confirmed_dead_code,
    resolve_share_link_with_retries,
)
from reddit_fetch import fetch_comments, find_share_code_threads
from vouchers import (
    canonical_share_link,
    exists,
    inactive_voucher_doc,
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
PROMO_WORKERS = 30
PROMO_MAX_ATTEMPTS = 5
MARKET_SCAN_ORDER = ("US", "CA")


def _landing_url_for_code(code: str, *, market: str = "US") -> str:
    origin = get_market(market)["origin"]
    return f"{origin}/pages/meal-kit-delivery?c={quote(code)}"


def detect_item_market(value: str, *, kind: str) -> str:
    """Classify a share link as US or CA.

    - hellofresh.ca share/landing URL → CA
    - hellofresh.com → US
    Bare codes are handled separately (scanned in both markets).
    """
    text = (value or "").strip().lower()
    if kind == "share" or text.startswith("http"):
        host = (urlparse(text).hostname or "").lower()
        if host.endswith("hellofresh.ca") or ".hellofresh.ca" in text:
            return "CA"
        return "US"
    return "US"


def collect_from_threads(
    thread_urls: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Visit threads sequentially; return (comments, inventory items).

    Each inventory item:
      {kind, value, resolve_url, comment, market}

    Bare promo codes (no share URL) are enqueued for **both** US and CA.
    """
    from monitor import walk_comments

    all_comments: list[dict[str, Any]] = []
    inventory: list[dict[str, Any]] = []
    seen_links: set[str] = set()
    # (code, market) so the same bare code can be scanned US + CA once each
    seen_codes: set[tuple[str, str]] = set()

    for url in thread_urls:
        print(f"\n📡 Collecting from thread: {url}", flush=True)
        comments = walk_comments(fetch_comments(url))
        all_comments.extend(comments)
        for comment in comments:
            for link in comment.get("share_links") or []:
                market = detect_item_market(link, kind="share")
                key = share_link_key(link, market=market)
                if key in seen_links:
                    continue
                seen_links.add(key)
                inventory.append(
                    {
                        "kind": "share",
                        "value": link,
                        "resolve_url": link,
                        "comment": comment,
                        "market": market,
                    }
                )
            for code in comment.get("promo_codes") or []:
                code_u = str(code).strip().upper()
                if not code_u:
                    continue
                # Pure codes → scan USA and CAD (each market's landing URL / proxies)
                for market in ("US", "CA"):
                    dedupe = (code_u, market)
                    if dedupe in seen_codes:
                        continue
                    seen_codes.add(dedupe)
                    inventory.append(
                        {
                            "kind": "code",
                            "value": code_u,
                            "resolve_url": _landing_url_for_code(
                                code_u, market=market
                            ),
                            "comment": comment,
                            "market": market,
                        }
                    )

    us_n = sum(1 for i in inventory if i["market"] == "US")
    ca_n = sum(1 for i in inventory if i["market"] == "CA")
    print(
        f"\n📋 Inventory | threads={len(thread_urls)} comments={len(all_comments)} "
        f"share_links={sum(1 for i in inventory if i['kind']=='share')} "
        f"bare_codes={sum(1 for i in inventory if i['kind']=='code')} "
        f"total={len(inventory)} | US={us_n} CA={ca_n}",
        flush=True,
    )
    for i, item in enumerate(inventory, 1):
        print(
            f"  {i:>3}. [{item['market']}] [{item['kind']}] {item['value']}",
            flush=True,
        )
    return all_comments, inventory


def _resolve_one(
    index: int,
    total: int,
    item: dict[str, Any],
    *,
    market: str,
    known_lock: threading.Lock,
    known: dict[str, set[str]],
) -> dict[str, Any]:
    """Worker: resolve one share link / bare code and insert if new."""
    mkt = get_market(market)
    comment = item["comment"]
    resolve_url = item["resolve_url"]
    kind = item["kind"]
    value = item["value"]
    author = comment.get("author", "?")
    out = {"resolved": 0, "inserted": 0, "skipped_exists": 0, "failed": 0}
    tag = f"[{mkt['code']} {index}/{total}]"

    # Skip if already in Mongo for this market
    if kind == "share":
        key = share_link_key(value, market=mkt["code"])
        with known_lock:
            already = key in known["share_links"]
        if already or share_link_exists(value, market=mkt["code"]):
            print(
                f"{tag} skip existing share | "
                f"{canonical_share_link(value, market=mkt['code'])}",
                flush=True,
            )
            with known_lock:
                known["share_links"].add(key)
            out["skipped_exists"] += 1
            return out
    else:
        with known_lock:
            already = value in known["promo_codes"]
        if already or exists(promo_code=value, market=mkt["code"]):
            print(f"{tag} skip existing code | {value}", flush=True)
            with known_lock:
                known["promo_codes"].add(value)
            out["skipped_exists"] += 1
            return out

    print(f"{tag} {author} | [{kind}] {value}", flush=True)
    promo = resolve_share_link_with_retries(
        resolve_url,
        market=mkt["code"],
        max_attempts=PROMO_MAX_ATTEMPTS,
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
        print(f"  → skip save (promo_code already in {mkt['collections']['vouchers']})", flush=True)
        out["skipped_exists"] += 1
        with known_lock:
            if kind == "share":
                known["share_links"].add(share_link_key(value, market=mkt["code"]))
            if code:
                known["promo_codes"].add(code)
        return out

    if kind == "share" and share_link_exists(value, market=mkt["code"]):
        print("  → skip save (share_link already in Mongo)", flush=True)
        out["skipped_exists"] += 1
        return out

    # Confirmed not-working (voucher 404 / inactive) → save active:false for skip
    if promo.get("dead") or is_confirmed_dead_code(promo):
        doc = inactive_voucher_doc(promo, comment=comment, market=mkt["code"])
        if not doc:
            print("  → skip save (dead code but missing promo/share)", flush=True)
            out["failed"] += 1
            return out
        doc["market"] = mkt["code"]
        doc["share_link"] = canonical_share_link(
            doc.get("share_link") or resolve_url, market=mkt["code"]
        )
        doc["share_link_key"] = share_link_key(doc["share_link"], market=mkt["code"])
        action = insert_voucher(doc, market=mkt["code"])
        if action == "inserted":
            print(
                f"  → saved inactive code to {mkt['collections']['vouchers']} "
                f"(active:false)",
                flush=True,
            )
            out["inserted"] += 1
        else:
            print(f"  → skip save ({action}) dead/existing", flush=True)
            out["skipped_exists"] += 1
        with known_lock:
            known["promo_codes"].add(doc["promo_code"])
            known.setdefault("bad_codes", set()).add(doc["promo_code"])
            if doc.get("share_link_key"):
                known["share_links"].add(doc["share_link_key"])
                known.setdefault("bad_links", set()).add(doc["share_link_key"])
        return out

    # Incomplete API pricing after retries — do NOT save (unknown good/bad)
    if promo.get("incomplete") or not has_required_api_pricing(promo):
        print(
            "  → skip save (incomplete zip_code/recipes_per_week/"
            "servings_per_recipe/shipping_at_max)",
            flush=True,
        )
        out["failed"] += 1
        return out

    doc = promo_result_to_doc(promo, comment=comment, market=mkt["code"])
    if not doc:
        print("  → skip save (no promo doc / incomplete pricing)", flush=True)
        out["failed"] += 1
        return out

    doc["market"] = mkt["code"]
    doc["share_link"] = canonical_share_link(doc["share_link"], market=mkt["code"])
    doc["share_link_key"] = share_link_key(doc["share_link"], market=mkt["code"])

    doc["valid"] = bool(is_valid_promo(promo))

    # Complete API data but bad offer — still save so later runs skip it
    if is_bad_voucher(doc):
        action = insert_voucher(doc, market=mkt["code"])
        if action == "inserted":
            print(
                f"  → saved bad code to {mkt['collections']['vouchers']} "
                f"(skip later runs)",
                flush=True,
            )
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

    action = insert_voucher(doc, market=mkt["code"])
    if action == "inserted":
        print(f"  → saved to HelloFresh.{mkt['collections']['vouchers']}", flush=True)
        out["inserted"] += 1
        with known_lock:
            known["share_links"].add(
                doc.get("share_link_key")
                or share_link_key(doc["share_link"], market=mkt["code"])
            )
            known["promo_codes"].add(doc["promo_code"])
            if doc.get("reddit_comment_id"):
                known["comment_ids"].add(str(doc["reddit_comment_id"]))
    else:
        print(f"  → skip save ({action}) — existing doc left untouched", flush=True)
        out["skipped_exists"] += 1
        with known_lock:
            known["promo_codes"].add(doc["promo_code"])
    return out


def _scan_market(
    items: list[dict[str, Any]],
    *,
    market: str,
    workers: int,
) -> dict[str, int]:
    """Scan one market with N workers using that market's endpoints + proxies."""
    from proxies import begin_proxy_cycle, cycle_ips_used, ensure_market_proxies, load_proxies_at_start

    mkt = get_market(market)
    stats = {
        "resolved": 0,
        "inserted": 0,
        "skipped_exists": 0,
        "failed": 0,
        "skipped_bad": 0,
    }
    if not items:
        print(f"\n[{mkt['code']}] Nothing to scan.", flush=True)
        return stats

    begin_proxy_cycle(f"reddit-scan-{mkt['code']}")
    n_proxies = ensure_market_proxies(mkt["code"])
    load_proxies_at_start(market=mkt["code"])
    print(
        f"\n=== [{mkt['code']}] Scan | items={len(items)} workers={workers} "
        f"proxy={mkt['proxy_collection']} (n={n_proxies}) "
        f"origin={mkt['origin']} → {mkt['collections']['vouchers']} ===",
        flush=True,
    )

    known = load_known(market=mkt["code"])
    print(
        f"[{mkt['code']}] Mongo preload | share_links={len(known['share_links'])} "
        f"promo_codes={len(known['promo_codes'])} "
        f"bad_skip={len(known.get('bad_codes') or set())}",
        flush=True,
    )

    to_resolve: list[dict[str, Any]] = []
    skipped = 0
    skipped_bad = 0
    bad_codes = known.get("bad_codes") or set()
    bad_links = known.get("bad_links") or set()
    for item in items:
        if item["kind"] == "share":
            key = share_link_key(item["value"], market=mkt["code"])
            if key in known["share_links"] or share_link_exists(
                item["value"], market=mkt["code"]
            ):
                skipped += 1
                if key in bad_links:
                    skipped_bad += 1
                known["share_links"].add(key)
                continue
        else:
            code = item["value"]
            if code in known["promo_codes"] or exists(
                promo_code=code, market=mkt["code"]
            ):
                skipped += 1
                if code in bad_codes:
                    skipped_bad += 1
                known["promo_codes"].add(code)
                continue
        to_resolve.append(item)

    stats["skipped_exists"] = skipped
    stats["skipped_bad"] = skipped_bad
    print(
        f"[{mkt['code']}] To scan: {len(to_resolve)} | already known/skipped: "
        f"{skipped} (bad={skipped_bad}) | workers={workers}",
        flush=True,
    )
    if not to_resolve:
        return stats

    known_lock = threading.Lock()
    total = len(to_resolve)
    print(
        f"\n🚀 [{mkt['code']}] Scanning {total} item(s) with {workers} workers…",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [
            pool.submit(
                _resolve_one,
                i,
                total,
                item,
                market=mkt["code"],
                known_lock=known_lock,
                known=known,
            )
            for i, item in enumerate(to_resolve, 1)
        ]
        for fut in as_completed(futures):
            try:
                delta = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"  [{mkt['code']}] worker error: {exc}", flush=True)
                stats["failed"] += 1
                continue
            for k in ("resolved", "inserted", "skipped_exists", "failed"):
                stats[k] += int(delta.get(k) or 0)

    print(
        f"\n{datetime.now(timezone.utc).isoformat()} | [{mkt['code']}] "
        f"resolved={stats['resolved']} inserted={stats['inserted']} "
        f"skipped_exists={stats['skipped_exists']} failed={stats['failed']} "
        f"unique_ips={len(cycle_ips_used())}",
        flush=True,
    )
    return stats


def parse_all_links_from_reddit(
    thread_url: str | None = None,
    *,
    country: str = "US",
    locale: str = "en-US",
    workers: int = PROMO_WORKERS,
    subreddit: str = "hellofresh",
) -> dict[str, int]:
    """Collect from Reddit → classify US/CA → scan US (30) then CA (30)."""
    del country, locale  # market comes from each item; kept for call-site compat

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

    by_market: dict[str, list[dict[str, Any]]] = {"US": [], "CA": []}
    for item in inventory:
        mkt = item.get("market") or "US"
        by_market.setdefault(mkt, []).append(item)

    print(
        f"\n🔀 Classified | US={len(by_market.get('US') or [])} "
        f"CA={len(by_market.get('CA') or [])}",
        flush=True,
    )

    totals = {
        "resolved": 0,
        "inserted": 0,
        "skipped_exists": 0,
        "failed": 0,
        "skipped_comments": 0,
        "skipped_links": 0,
        "skipped_bad": 0,
        "us_resolved": 0,
        "us_inserted": 0,
        "ca_resolved": 0,
        "ca_inserted": 0,
    }

    # --- Phase 2: US then CA, each with its own 10-worker pool ---
    for mkt_code in MARKET_SCAN_ORDER:
        items = by_market.get(mkt_code) or []
        delta = _scan_market(items, market=mkt_code, workers=workers)
        for k in ("resolved", "inserted", "skipped_exists", "failed", "skipped_bad"):
            totals[k] += int(delta.get(k) or 0)
        prefix = mkt_code.lower()
        totals[f"{prefix}_resolved"] = int(delta.get("resolved") or 0)
        totals[f"{prefix}_inserted"] = int(delta.get("inserted") or 0)

    totals["skipped_links"] = totals["skipped_exists"]
    print(
        f"\n✅ Reddit scan done | "
        f"US resolved={totals['us_resolved']} inserted={totals['us_inserted']} | "
        f"CA resolved={totals['ca_resolved']} inserted={totals['ca_inserted']} | "
        f"failed={totals['failed']}",
        flush=True,
    )
    return totals
