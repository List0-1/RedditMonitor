#!/usr/bin/env python3
"""RedditMonitor interactive entry.

At start: load proxies from MongoDB (Proxies DB, same as UEControl.py).
Menu:
  1) Parse all Reddit comments and find links (skip known; save new to Mongo)
  2) Monitor (Reddit every 30m; promo status every 5m)
"""

from __future__ import annotations

from proxies import load_proxies_at_start, proxy_label
from scan import parse_all_links_from_reddit
from monitor import run_monitor_loop


def main() -> int:
    print("=" * 60)
    print(" RedditMonitor")
    print("=" * 60)
    print("Loading proxies from MongoDB...")
    info = load_proxies_at_start()
    print(
        f"Ready | collection={info.get('collection')} "
        f"count={info.get('count')} proxy={proxy_label(info.get('proxy'))}"
    )

    while True:
        print("\nOptions:")
        print("  1) Discover share-codes thread(s), collect links/codes, scan (10 workers)")
        print("  2) Monitor")
        print("  q) Quit")
        choice = input("Select: ").strip().lower()

        if choice in {"q", "quit", "exit"}:
            print("Bye.")
            return 0
        if choice == "1":
            try:
                parse_all_links_from_reddit()
            except Exception as exc:  # noqa: BLE001
                print(f"Error: {exc}")
            continue
        if choice == "2":
            try:
                run_monitor_loop()
            except Exception as exc:  # noqa: BLE001
                print(f"Error: {exc}")
            continue

        print("Unknown option.")


if __name__ == "__main__":
    raise SystemExit(main())
