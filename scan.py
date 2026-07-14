"""Reddit scan: find new share links, resolve, save to HelloFresh.VoucherCodes."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from promo import (
    create_hf_session,
    format_promo_result,
    resolve_share_link_with_retries,
)
from reddit_fetch import fetch_comments
from vouchers import (
    canonical_share_link,
    insert_voucher,
    load_known,
    promo_result_to_doc,
    share_link_exists,
    share_link_key,
)

THREAD_URL = (
    "https://www.reddit.com/r/hellofresh/comments/1uv8bo8/"
    "share_weekly_trial_offer_and_free_box_codes_here/"
)


def parse_all_links_from_reddit(
    thread_url: str = THREAD_URL,
    *,
    country: str = "US",
    locale: str = "en-US",
) -> dict[str, int]:
    """Fetch Reddit, skip known share links, resolve+insert only brand-new ones."""
    from monitor import walk_comments
    from proxies import begin_proxy_cycle, cycle_ips_used

    begin_proxy_cycle("reddit-scan")
    print("\n📡 Fetching Reddit thread (pretested unique-IP proxies)...")
    comments = walk_comments(fetch_comments(thread_url))

    known = load_known()
    print(
        f"Mongo preload | share_links={len(known['share_links'])} "
        f"promo_codes={len(known['promo_codes'])} "
        f"comment_ids={len(known['comment_ids'])}"
    )

    to_resolve: list[tuple[dict, str]] = []
    skipped_comments = 0
    skipped_links = 0
    seen_this_run: set[str] = set()

    for comment in comments:
        for link in comment.get("share_links") or []:
            key = share_link_key(link)
            if key in known["share_links"] or key in seen_this_run:
                skipped_links += 1
                continue
            # Live Mongo check — never re-resolve / rewrite existing share links
            if share_link_exists(link):
                known["share_links"].add(key)
                skipped_links += 1
                print(
                    f"  skip existing share_link: {canonical_share_link(link)}",
                    flush=True,
                )
                continue
            seen_this_run.add(key)
            to_resolve.append((comment, link))

    print(
        f"Found {len(comments)} comments | "
        f"to_resolve={len(to_resolve)} | "
        f"skipped_comments={skipped_comments} skipped_links={skipped_links}"
    )

    stats = {
        "resolved": 0,
        "inserted": 0,
        "skipped_exists": 0,
        "failed": 0,
        "skipped_comments": skipped_comments,
        "skipped_links": skipped_links,
    }
    if not to_resolve:
        print("Nothing new to resolve.")
        return stats

    hf = create_hf_session()
    try:
        for i, (comment, link) in enumerate(to_resolve, 1):
            author = comment.get("author", "?")
            key = share_link_key(link)

            # Re-check right before resolve — never touch existing VoucherCodes rows
            if share_link_exists(link) or key in known["share_links"]:
                print(
                    f"\n[{i}/{len(to_resolve)}] skip existing share_link | {link}",
                    flush=True,
                )
                stats["skipped_exists"] += 1
                known["share_links"].add(key)
                continue

            print(f"\n[{i}/{len(to_resolve)}] {author} | {link}", flush=True)
            promo = resolve_share_link_with_retries(
                link,
                session=hf,
                country=country,
                locale=locale,
                max_attempts=3,
            )
            stats["resolved"] += 1
            print(format_promo_result(promo), flush=True)

            code = promo.get("promo_code")
            if code and code in known["promo_codes"]:
                print("  → skip save (promo_code already in Mongo)", flush=True)
                stats["skipped_exists"] += 1
                known["share_links"].add(key)
                time.sleep(0.8)
                continue

            # Final gate: share_link already in DB → skip, do not insert/update
            if share_link_exists(link):
                print("  → skip save (share_link already in Mongo)", flush=True)
                stats["skipped_exists"] += 1
                known["share_links"].add(key)
                time.sleep(0.8)
                continue

            doc = promo_result_to_doc(promo, comment=comment)
            if not doc:
                print("  → skip save (no promo_code)", flush=True)
                stats["failed"] += 1
                time.sleep(0.8)
                continue
            if doc.get("active") is False:
                print("  → skip save (inactive)", flush=True)
                known["share_links"].add(doc.get("share_link_key") or key)
                known["promo_codes"].add(doc["promo_code"])
                stats["skipped_exists"] += 1
                time.sleep(0.8)
                continue

            action = insert_voucher(doc)
            if action == "inserted":
                print("  → saved to HelloFresh.VoucherCodes", flush=True)
                stats["inserted"] += 1
                known["share_links"].add(doc.get("share_link_key") or key)
                known["promo_codes"].add(doc["promo_code"])
                if doc.get("reddit_comment_id"):
                    known["comment_ids"].add(str(doc["reddit_comment_id"]))
            else:
                print(f"  → skip save ({action}) — existing doc left untouched", flush=True)
                stats["skipped_exists"] += 1
                known["share_links"].add(doc.get("share_link_key") or key)
                known["promo_codes"].add(doc["promo_code"])

            time.sleep(0.8)
    finally:
        hf.close()

    print(
        f"\n{datetime.now(timezone.utc).isoformat()} | "
        f"resolved={stats['resolved']} inserted={stats['inserted']} "
        f"skipped_exists={stats['skipped_exists']} failed={stats['failed']} "
        f"unique_ips={len(cycle_ips_used())}"
    )
    return stats
