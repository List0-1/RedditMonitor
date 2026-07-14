#!/usr/bin/env python3
"""Monitor a Reddit thread for HelloFresh share links and resolve promo codes.

Reddit (pure HTTP, no browser / no manual cookies):
  1) curl_cffi Chrome TLS session
  2) Solve Reddit GET js_challenge (solution = token + token)
  3) GET {thread}/.json?limit=500&raw_json=1

HelloFresh promo resolution (from hffindpromocode.har):
  1) Follow share link redirects -> ?c=PROMO_CODE
  2) GET /gw/vouchers/{code}?country=US&locale=en-US with guest Bearer token
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from promo import (
    create_hf_session,
    format_promo_result,
    resolve_share_link,
    resolve_share_link_with_retries,
)
from proxies import get_active_proxy, load_proxies_at_start, proxy_label
from reddit_fetch import fetch_comments

THREAD_URL = (
    "https://www.reddit.com/r/hellofresh/comments/1uv8bo8/"
    "share_weekly_trial_offer_and_free_box_codes_here/"
)
SHARE_PREFIX = "https://www.hellofresh.com/gw/share"
SHARE_RE = re.compile(
    r"https://www\.hellofresh\.com/gw/share/[A-Za-z0-9][A-Za-z0-9_-]*"
    r"(?:\?[^\s<>\]\)\"'*]*)?",
    re.IGNORECASE,
)
# Bare promo codes posted without a share URL (e.g. FIH-XXXX, 8E-0HF4IFN8F82)
PROMO_CODE_RE = re.compile(
    r"\b([A-Z0-9]{1,4}-[A-Z0-9]{8,})\b",
    re.IGNORECASE,
)


def normalize_share_url(url: str) -> str | None:
    url = (
        url.replace("\\_", "_")
        .replace("&amp;", "&")
        .rstrip(".,;:!?*_~`\"')>]} ")
    )
    url = re.split(r"</", url, maxsplit=1)[0]
    if not url.lower().startswith(SHARE_PREFIX + "/"):
        return None

    parsed = urlparse(url)
    code = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,}", code):
        return None

    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, "")
    )


def extract_share_links(text: str) -> list[str]:
    if not text:
        return []
    cleaned = text.replace("\\_", "_").replace("\\*", "*").replace("&amp;", "&")
    found: list[str] = []
    seen: set[str] = set()
    for match in SHARE_RE.finditer(cleaned):
        normalized = normalize_share_url(match.group(0))
        if normalized and normalized not in seen:
            seen.add(normalized)
            found.append(normalized)
    return found


def extract_promo_codes(text: str) -> list[str]:
    """Bare promo codes in comment text (excluding ones inside share URLs)."""
    if not text:
        return []
    cleaned = text.replace("\\_", "_").replace("&amp;", "&")
    # Strip share URLs so we don't double-count path fragments
    cleaned = SHARE_RE.sub(" ", cleaned)
    found: list[str] = []
    seen: set[str] = set()
    for match in PROMO_CODE_RE.finditer(cleaned):
        code = match.group(1).strip().upper()
        if code and code not in seen:
            seen.add(code)
            found.append(code)
    return found


def walk_comments(children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for node in children or []:
        if node.get("kind") != "t1":
            continue
        data = node.get("data") or {}
        body = data.get("body") or ""
        links = extract_share_links(body)
        if not links:
            links = extract_share_links(data.get("body_html") or "")
        codes = extract_promo_codes(body)
        if not codes:
            codes = extract_promo_codes(data.get("body_html") or "")

        results.append(
            {
                "id": data.get("id"),
                "author": data.get("author"),
                "created_utc": data.get("created_utc"),
                "permalink": data.get("permalink"),
                "body": body,
                "share_links": links,
                "promo_codes": codes,
            }
        )

        replies = data.get("replies")
        if isinstance(replies, dict):
            nested = (replies.get("data") or {}).get("children") or []
            results.extend(walk_comments(nested))
    return results


def fetch_thread(thread_url: str) -> list[dict[str, Any]]:
    return walk_comments(fetch_comments(thread_url))


def load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")).get("seen_links") or [])
    except (json.JSONDecodeError, OSError):
        return set()


def save_seen(path: Path, seen: set[str]) -> None:
    path.write_text(
        json.dumps({"seen_links": sorted(seen)}, indent=2) + "\n",
        encoding="utf-8",
    )


def unique_links(comments: list[dict[str, Any]], limit: int = 0) -> list[str]:
    links: list[str] = []
    for comment in comments:
        for link in comment["share_links"]:
            if link not in links:
                links.append(link)
            if limit and len(links) >= limit:
                return links
    return links


def resolve_links(
    links: list[str],
    *,
    resolve: bool,
    country: str,
    locale: str,
) -> dict[str, dict[str, Any]]:
    if not resolve or not links:
        return {}
    hf = create_hf_session()
    out: dict[str, dict[str, Any]] = {}
    try:
        for i, link in enumerate(links, 1):
            print(f"  resolving [{i}/{len(links)}] {link}", flush=True)
            out[link] = resolve_share_link_with_retries(
                link,
                session=hf,
                country=country,
                locale=locale,
                max_attempts=3,
            )
            time.sleep(0.8)
    finally:
        hf.close()
    return out


def format_hit(
    comment: dict[str, Any],
    link: str,
    promo: dict[str, Any] | None = None,
) -> str:
    ts = comment.get("created_utc")
    when = (
        datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        if ts
        else "unknown"
    )
    permalink = comment.get("permalink") or ""
    if permalink.startswith("/"):
        permalink = f"https://www.reddit.com{permalink}"
    lines = [
        f"[{when}] u/{comment.get('author')} | {link}",
        f"  comment: {permalink}",
    ]
    if promo is not None:
        lines.append(format_promo_result(promo))
    return "\n".join(lines)


def collect_new_hits(
    comments: list[dict[str, Any]], seen: set[str]
) -> list[tuple[dict[str, Any], str]]:
    hits: list[tuple[dict[str, Any], str]] = []
    for comment in comments:
        for link in comment["share_links"]:
            if link in seen:
                continue
            seen.add(link)
            hits.append((comment, link))
    return hits


def refresh_voucher_statuses(
    *,
    country: str = "US",
    locale: str = "en-US",
) -> dict[str, int]:
    """Re-check every stored voucher; update changes; keep bad codes for skip."""
    from proxies import begin_proxy_cycle, cycle_ips_used
    from vouchers import (
        comparable_snapshot,
        is_bad_voucher,
        list_vouchers,
        promo_result_to_doc,
        update_voucher,
    )

    docs = list_vouchers()
    stats = {
        "checked": 0,
        "updated": 0,
        "deleted": 0,
        "ok": 0,
        "errors": 0,
        "retries": 0,
    }
    if not docs:
        print("Status check: no vouchers in Mongo.", flush=True)
        from vouchers import update_best_voucher_code

        try:
            update_best_voucher_code([])
        except Exception as exc:  # noqa: BLE001
            print(f"BestVoucherCode update error: {exc}", file=sys.stderr, flush=True)
        return stats

    begin_proxy_cycle("status-check")
    print(
        f"\n⏱ Status check: {len(docs)} voucher(s) "
        f"(pretested unique-IP proxies, 3 retries)",
        flush=True,
    )
    hf = create_hf_session()
    try:
        for i, doc in enumerate(docs, 1):
            code = doc.get("promo_code") or "?"
            share = doc.get("share_link")
            print(f"  [{i}/{len(docs)}] {code}", flush=True)
            stats["checked"] += 1
            if not share:
                print("    → keep (missing share_link — skip resolve)", flush=True)
                stats["ok"] += 1
                continue

            # Already known-bad: leave in Mongo for scan skip (don't re-burn proxies)
            if is_bad_voucher(doc):
                print("    → skip resolve (bad in Mongo — kept for scan skip)", flush=True)
                stats["ok"] += 1
                continue

            promo = resolve_share_link_with_retries(
                share,
                session=hf,
                country=country,
                locale=locale,
                max_attempts=3,
            )
            print(format_promo_result(promo), flush=True)

            from promo import has_required_api_pricing

            voucher = promo.get("voucher") or {}
            active = voucher.get("is_active")
            resolved_code = promo.get("promo_code")

            # Incomplete pricing after retries — keep existing Mongo row untouched
            if not has_required_api_pricing(promo):
                print(
                    f"    → keep (incomplete pricing after retries: {promo.get('error')})",
                    flush=True,
                )
                stats["errors"] += 1
                time.sleep(0.5)
                continue

            # Keep inactive/bad in Mongo so future scans skip them
            if active is False:
                update_voucher(
                    code,
                    {
                        "active": False,
                        "recipes_per_week": 0,
                        "servings_per_recipe": 0,
                    },
                )
                print("    → marked inactive (kept for scan skip)", flush=True)
                stats["updated"] += 1
                time.sleep(0.5)
                continue

            fresh = promo_result_to_doc(promo, comment=None)
            if fresh and is_bad_voucher(fresh):
                update_fields = {k: fresh[k] for k in comparable_snapshot(fresh) if k in fresh}
                update_fields.pop("share_link", None)
                update_fields.pop("share_link_key", None)
                update_voucher(code, update_fields)
                print("    → marked bad (no free meals/servings; kept for scan skip)", flush=True)
                stats["updated"] += 1
                time.sleep(0.5)
                continue

            # Proxy / transient failure after retries — keep in Mongo
            if not resolved_code:
                print(
                    f"    → keep (unresolvable after retries: {promo.get('error')})",
                    flush=True,
                )
                stats["errors"] += 1
                time.sleep(0.5)
                continue

            if not fresh:
                stats["errors"] += 1
                time.sleep(0.5)
                continue

            # If voucher details missing after retries, don't wipe good Mongo data
            if fresh.get("active") is None and doc.get("active") is True:
                print(
                    f"    → keep existing (details unavailable: {promo.get('error')})",
                    flush=True,
                )
                stats["ok"] += 1
                time.sleep(0.5)
                continue

            old_snap = comparable_snapshot(doc)
            new_snap = comparable_snapshot(fresh)
            if old_snap != new_snap:
                update_fields = {k: fresh[k] for k in new_snap if k in fresh}
                # Never rewrite share_link / wipe existing VoucherCodes identity
                update_fields.pop("share_link", None)
                update_fields.pop("share_link_key", None)
                update_voucher(code, update_fields)
                print("    → updated in Mongo", flush=True)
                stats["updated"] += 1
            else:
                print("    → ok (unchanged)", flush=True)
                stats["ok"] += 1
            time.sleep(0.5)
    finally:
        hf.close()

    print(
        f"Status done | checked={stats['checked']} ok={stats['ok']} "
        f"updated={stats['updated']} deleted={stats['deleted']} "
        f"errors={stats['errors']} | unique_ips={len(cycle_ips_used())}",
        flush=True,
    )

    from vouchers import update_best_voucher_code

    try:
        update_best_voucher_code()
    except Exception as exc:  # noqa: BLE001
        print(f"BestVoucherCode update error: {exc}", file=sys.stderr, flush=True)

    return stats


def run_monitor_loop(
    thread_url: str | None = None,
    *,
    reddit_interval: int = 30 * 60,
    status_interval: int = 5 * 60,
    country: str = "US",
    locale: str = "en-US",
) -> None:
    """Monitor: discover share-codes thread(s), scan every 30m; status every 5m."""
    from scan import parse_all_links_from_reddit

    target = thread_url or "auto-discover pinned HelloFresh share-codes thread(s)"
    print(
        f"Monitoring {target}\n"
        f"Reddit scan every {reddit_interval}s | "
        f"status check every {status_interval}s\n"
        f"Proxy: {proxy_label(get_active_proxy())}",
        flush=True,
    )

    next_reddit = 0.0
    next_status = 0.0
    try:
        while True:
            now = time.time()
            if now >= next_reddit:
                print(
                    f"\n=== Reddit scan @ {datetime.now(timezone.utc).isoformat()} ===",
                    flush=True,
                )
                try:
                    parse_all_links_from_reddit(
                        thread_url, country=country, locale=locale
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"Reddit scan error: {exc}", file=sys.stderr, flush=True)
                next_reddit = time.time() + max(reddit_interval, 60)

            if now >= next_status:
                try:
                    refresh_voucher_statuses(country=country, locale=locale)
                except Exception as exc:  # noqa: BLE001
                    print(f"Status check error: {exc}", file=sys.stderr, flush=True)
                next_status = time.time() + max(status_interval, 30)

            sleep_for = min(next_reddit, next_status) - time.time()
            time.sleep(max(5.0, min(sleep_for, 60.0)))
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Monitor Reddit HelloFresh share links and resolve promo codes"
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Optional Reddit thread URL (default: auto-discover pinned share-codes thread)",
    )
    parser.add_argument(
        "--reddit-interval",
        type=int,
        default=30 * 60,
        help="Seconds between Reddit scans (default 30m)",
    )
    parser.add_argument(
        "--status-interval",
        type=int,
        default=5 * 60,
        help="Seconds between voucher status checks (default 5m)",
    )
    parser.add_argument("--once", action="store_true", help="One Reddit scan then exit")
    parser.add_argument("--country", default="US")
    parser.add_argument("--locale", default="en-US")
    args = parser.parse_args()

    print("Loading proxies from MongoDB...")
    info = load_proxies_at_start()
    print(
        f"Proxy ready | {info.get('collection')} "
        f"({info.get('count')}) → {proxy_label(info.get('proxy'))}"
    )

    try:
        if args.once:
            from scan import parse_all_links_from_reddit

            parse_all_links_from_reddit(
                args.url, country=args.country, locale=args.locale
            )
            return 0

        run_monitor_loop(
            args.url,
            reddit_interval=args.reddit_interval,
            status_interval=args.status_interval,
            country=args.country,
            locale=args.locale,
        )
        return 0
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
