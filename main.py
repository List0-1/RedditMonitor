#!/usr/bin/env python3
"""HelloFreshMonitor entrypoint.

Starts everything:
  - HTTP API (checkout emails → CheckoutEmail / CheckoutEmailCA)
  - Referral workers US+CA (passwordless login → referral → VoucherCodes*)
  - Skip-weeks backup workers US+CA (retry missed week pauses every 30m)
  - Monitor loop (Reddit scan every 3h + voucher status every 5m)
"""

from __future__ import annotations

import argparse
import os
import threading

from api import run_api
from referral_worker import start_referral_workers
from skip_weeks_worker import start_skip_weeks_workers


def _start_monitor_loop() -> None:
    from monitor import run_monitor_loop
    from proxies import load_proxies_at_start

    load_proxies_at_start()
    run_monitor_loop()


def run_service(
    *,
    workers: int = 10,
    skip_workers: int = 10,
    api_only: bool = False,
    no_monitor: bool = False,
) -> int:
    stop_event: threading.Event | None = None

    api_thread = threading.Thread(target=run_api, name="api", daemon=True)
    api_thread.start()

    try:
        if not api_only:
            stop_event = threading.Event()
            stop_event, _ref_threads = start_referral_workers(
                worker_count=workers,
                stop_event=stop_event,
            )
            start_skip_weeks_workers(
                worker_count=skip_workers,
                stop_event=stop_event,
            )
            print(
                f"[MAIN] Skip-weeks backup workers started (n={skip_workers})",
                flush=True,
            )
            if not no_monitor:
                monitor_thread = threading.Thread(
                    target=_start_monitor_loop,
                    name="monitor",
                    daemon=True,
                )
                monitor_thread.start()
                print(
                    "[MAIN] Monitor loop started (Reddit 3h / status 5m)",
                    flush=True,
                )
        api_thread.join()
    finally:
        if stop_event is not None:
            stop_event.set()
    return 0


def run_menu() -> int:
    from monitor import run_monitor_loop
    from proxies import load_proxies_at_start, proxy_label
    from scan import parse_all_links_from_reddit

    print("=" * 60)
    print(" HelloFreshMonitor")
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
        print("  2) Monitor only")
        print("  3) Full service (API + referral workers + monitor)")
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
        if choice == "3":
            return run_service()

        print("Unknown option.")


def main() -> int:
    parser = argparse.ArgumentParser(description="HelloFreshMonitor")
    parser.add_argument(
        "--menu",
        action="store_true",
        help="Interactive Reddit scan/monitor menu",
    )
    parser.add_argument(
        "--api-only",
        action="store_true",
        help="Run HTTP API only",
    )
    parser.add_argument(
        "--no-monitor",
        action="store_true",
        help="Skip Reddit/status monitor loop",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("REFERRAL_WORKERS", "10")),
        help="Total referral workers split across US+CA (default 10 → 5+5)",
    )
    parser.add_argument(
        "--skip-workers",
        type=int,
        default=int(os.environ.get("SKIP_WEEKS_WORKERS", "10")),
        help="Total skip-weeks backup workers split across US+CA (default 10)",
    )
    args = parser.parse_args()

    if args.menu:
        return run_menu()
    return run_service(
        workers=args.workers,
        skip_workers=args.skip_workers,
        api_only=args.api_only,
        no_monitor=args.no_monitor,
    )


if __name__ == "__main__":
    raise SystemExit(main())
