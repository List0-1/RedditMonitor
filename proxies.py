"""Load proxies from MongoDB (same pattern as UEControl.py).

DB: Proxies
Docs: { proxy: "host:port:user:pass", blacklist: bool }

Every outbound proxy is live-tested (exit IP API, 10s timeout).
Within a monitor cycle, each request must use a different exit IP.
"""

from __future__ import annotations

import os
import random
import threading
from typing import Any
from urllib.parse import quote

from curl_cffi import requests as crequests
from pymongo import MongoClient

# Same cluster as UEControl.py — override with REDDIT_MONITOR_MONGO_URI
MONGO_URI = os.environ.get(
    "REDDIT_MONITOR_MONGO_URI",
    "mongodb+srv://chaofengzhang90:Y7rLbu3q791a5MA0@discord.fawongf.mongodb.net/"
    "?retryWrites=true&w=majority&appName=discord&tlsAllowInvalidCertificates=true",
)
PROXIES_DB_NAME = "Proxies"
BOTCONFIG_DB = "BotConfig"
BOTCONFIG_COL = "DBControl"
DEFAULT_PROXY_COLLECTION = "Resi_Lightning"

IP_TEST_URLS = (
    "https://api.ipify.org",
    "https://icanhazip.com",
    "https://checkip.amazonaws.com",
)
IP_TEST_TIMEOUT = 10
PROXY_PICK_TRIES = 25

_proxies_raw: list[list[str]] = []
_proxies_lock = threading.Lock()
_active_proxy: dict[str, str] | None = None
_active_ip: str | None = None
_cycle_ips: set[str] = set()
_broken_proxies: set[str] = set()  # identities that failed this process
_market_rows: dict[str, list[list[str]]] = {}  # US/CA pools (Resi_Lightning / Resi_LightningCA)
_test_headers = {
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/134.0.6807.70 Safari/537.36"
    ),
    "accept-language": "en-US,en;q=0.9",
}


def list_proxy_collections() -> list[str]:
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=15000)
        names = sorted(client[PROXIES_DB_NAME].list_collection_names())
        client.close()
        return names
    except Exception as exc:  # noqa: BLE001
        print(f"[PROXIES] Failed to list collections: {exc}")
        return []


def load_proxies_from_db_collection(collection_name: str) -> list[list[str]]:
    """Return list of [host, port, user, pass] (UEControl format)."""
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=15000)
        col = client[PROXIES_DB_NAME][collection_name]
        docs = col.find({"blacklist": {"$ne": True}}, {"proxy": 1})
        result: list[list[str]] = []
        for doc in docs:
            raw = (doc.get("proxy") or "").strip()
            parts = raw.split(":")
            if len(parts) >= 4:
                host, port, user = parts[0], parts[1], parts[2]
                passw = ":".join(parts[3:])
                result.append([host, port, user, passw])
        client.close()
        random.shuffle(result)
        print(f"[PROXIES] Loaded {len(result)} from Proxies.{collection_name}")
        return result
    except Exception as exc:  # noqa: BLE001
        print(f"[PROXIES] Failed to load '{collection_name}': {exc}")
        return []


def default_proxy_collection() -> str | None:
    """Prefer BotConfig.DBControl.default_proxy if it is db:<name>."""
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
        doc = client[BOTCONFIG_DB][BOTCONFIG_COL].find_one({}) or {}
        client.close()
        raw = doc.get("default_proxy") or ""
        if isinstance(raw, str) and raw.startswith("db:"):
            return raw[3:]
    except Exception:  # noqa: BLE001
        pass
    return None


def to_proxy_dict(host: str, port: str, user: str, passw: str) -> dict[str, str]:
    user_q = quote(user, safe="")
    pass_q = quote(passw, safe="")
    url = f"http://{user_q}:{pass_q}@{host}:{port}"
    return {"http": url, "https": url}


def proxy_label(proxy: dict[str, str] | None) -> str:
    if not proxy:
        return "direct (no proxy)"
    raw = proxy.get("http") or proxy.get("https") or ""
    if "@" in raw:
        return raw.split("@", 1)[1]
    return raw


def proxy_identity(proxy: dict[str, str] | None) -> str:
    if not proxy:
        return ""
    return proxy.get("http") or proxy.get("https") or ""


def begin_proxy_cycle(label: str = "monitor") -> None:
    """Reset used-IP set for a new Reddit/status cycle."""
    global _cycle_ips
    with _proxies_lock:
        _cycle_ips = set()
    print(f"[PROXIES] cycle start: {label} (broken={len(_broken_proxies)})", flush=True)


def mark_proxy_broken(proxy: dict[str, str] | None = None, *, reason: str = "") -> None:
    """Blacklist current (or given) proxy for the rest of this process — swap away."""
    global _active_proxy, _active_ip
    target = proxy or _active_proxy
    ident = proxy_identity(target)
    if not ident:
        return
    with _proxies_lock:
        _broken_proxies.add(ident)
    label = proxy_label(target)
    why = f" ({reason})" if reason else ""
    print(f"[PROXIES] ❌ broken → blacklist {label}{why}", flush=True)
    if proxy_identity(_active_proxy) == ident:
        _active_proxy = None
        _active_ip = None


def swap_proxy_on_failure(*, reason: str = "request failed") -> dict[str, str] | None:
    """Mark current proxy broken and immediately pick a fresh pretest-ed one."""
    mark_proxy_broken(reason=reason)
    return next_proxy(prefer_different=True)


def cycle_ips_used() -> set[str]:
    with _proxies_lock:
        return set(_cycle_ips)


def test_proxy_exit_ip(
    proxy: dict[str, str],
    *,
    timeout: int = IP_TEST_TIMEOUT,
) -> str | None:
    """Live-test proxy via public IP API. Returns exit IP or None."""
    for test_url in IP_TEST_URLS:
        try:
            session = crequests.Session(impersonate="chrome131")
            resp = session.get(
                test_url,
                proxies=proxy,
                headers=_test_headers,
                timeout=timeout,
            )
            session.close()
            if resp.status_code == 200 and resp.text.strip():
                ip = resp.text.strip().split()[0]
                if ip and "." in ip:
                    return ip
        except Exception:  # noqa: BLE001
            continue
    return None


def pick_working_proxy(
    rows: list[list[str]],
    *,
    max_tries: int = 12,
    exclude_ips: set[str] | None = None,
    quiet: bool = False,
) -> tuple[dict[str, str], str] | tuple[None, None]:
    """Test proxies until one returns a usable exit IP (optionally unique)."""
    if not rows:
        return None, None
    exclude = exclude_ips or set()
    candidates = rows[:]
    random.shuffle(candidates)
    for host, port, user, passw in candidates[:max_tries]:
        proxy = to_proxy_dict(host, port, user, passw)
        ip = test_proxy_exit_ip(proxy, timeout=IP_TEST_TIMEOUT)
        if not ip:
            continue
        if ip in exclude:
            continue
        if not quiet:
            print(f"[PROXIES] ✅ pretest ok {host}:{port} | exit IP {ip}", flush=True)
        return proxy, ip
    return None, None


def _market_proxy_collection(market: str) -> str:
    """Market code (US/CA) → Proxies collection name."""
    try:
        from market import get_market

        return str(get_market(market)["proxy_collection"])
    except Exception:  # noqa: BLE001
        return DEFAULT_PROXY_COLLECTION


def ensure_market_proxies(market: str) -> int:
    """Load the market proxy pool (Resi_Lightning / Resi_LightningCA) once.

    Returns the number of proxies available for that market.
    """
    key = (market or "US").upper()
    with _proxies_lock:
        cached = _market_rows.get(key)
    if cached:
        return len(cached)

    rows = load_proxies_from_db_collection(_market_proxy_collection(key))
    with _proxies_lock:
        _market_rows[key] = rows
    return len(rows)


def load_proxies_at_start(
    collection_name: str | None = None,
    market: str | None = None,
) -> dict[str, Any]:
    """Load proxies from MongoDB and select one working pretest-ed proxy."""
    global _proxies_raw, _active_proxy, _active_ip

    if market and not collection_name:
        collection_name = _market_proxy_collection(market)

    collections = list_proxy_collections()
    if not collections:
        print("[PROXIES] No collections in Proxies DB — continuing without proxy.")
        _proxies_raw = []
        _active_proxy = None
        _active_ip = None
        return {"collection": None, "count": 0, "proxy": None}

    preferred = collection_name or DEFAULT_PROXY_COLLECTION
    bot_default = default_proxy_collection()
    if preferred in collections:
        chosen = preferred
    elif bot_default and bot_default in collections:
        chosen = bot_default
    elif "proxies_oxy" in collections:
        chosen = "proxies_oxy"
    else:
        chosen = collections[0]

    print(f"[PROXIES] Using collection: {chosen}")
    rows = load_proxies_from_db_collection(chosen)
    with _proxies_lock:
        _proxies_raw = rows

    begin_proxy_cycle("startup")
    proxy, ip = pick_working_proxy(rows, exclude_ips=set(_cycle_ips))
    if proxy and ip:
        _active_proxy = proxy
        _active_ip = ip
        with _proxies_lock:
            _cycle_ips.add(ip)
    else:
        _active_proxy = None
        _active_ip = None
        print("[PROXIES] No pretest-passing proxy found at startup")

    print(f"[PROXIES] Active: {proxy_label(_active_proxy)} ip={_active_ip}")
    return {"collection": chosen, "count": len(rows), "proxy": _active_proxy}


def get_active_proxy() -> dict[str, str] | None:
    return _active_proxy


def get_active_ip() -> str | None:
    return _active_ip


def next_proxy(*, prefer_different: bool = True) -> dict[str, str] | None:
    """Pick + pretest a proxy with an exit IP not yet used in this cycle.

    Skips proxies already marked broken this process.
    """
    global _active_proxy, _active_ip
    with _proxies_lock:
        rows = list(_proxies_raw)
        used = set(_cycle_ips)
        broken = set(_broken_proxies)

    if not rows:
        return _active_proxy

    current_id = proxy_identity(_active_proxy) if prefer_different else None
    order = rows[:]
    random.shuffle(order)

    tries = 0
    for host, port, user, passw in order:
        if tries >= PROXY_PICK_TRIES:
            break
        proxy = to_proxy_dict(host, port, user, passw)
        ident = proxy_identity(proxy)
        if ident in broken:
            continue
        if current_id and ident == current_id:
            continue
        tries += 1
        ip = test_proxy_exit_ip(proxy, timeout=IP_TEST_TIMEOUT)
        if not ip:
            # Pretest failed — don't keep offering this credential
            with _proxies_lock:
                _broken_proxies.add(ident)
            continue
        if ip in used:
            continue

        _active_proxy = proxy
        _active_ip = ip
        with _proxies_lock:
            _cycle_ips.add(ip)
        return _active_proxy

    # Fallback: allow IP reuse but still skip broken
    candidates = [
        row
        for row in rows
        if proxy_identity(to_proxy_dict(*row)) not in broken
    ]
    proxy, ip = pick_working_proxy(
        candidates or rows, max_tries=PROXY_PICK_TRIES, quiet=True
    )
    if proxy and ip:
        ident = proxy_identity(proxy)
        if ident not in broken:
            _active_proxy = proxy
            _active_ip = ip
            with _proxies_lock:
                _cycle_ips.add(ip)
            return _active_proxy

    print("[PROXIES] ❌ no working proxy available", flush=True)
    return None


def _next_market_proxy(market: str) -> dict[str, str] | None:
    """Pick + pretest a proxy from the market pool (skips broken ones)."""
    key = (market or "US").upper()
    ensure_market_proxies(key)
    with _proxies_lock:
        rows = list(_market_rows.get(key) or [])
        broken = set(_broken_proxies)
    if not rows:
        return None

    candidates = [
        row for row in rows if proxy_identity(to_proxy_dict(*row)) not in broken
    ]
    proxy, ip = pick_working_proxy(
        candidates or rows, max_tries=PROXY_PICK_TRIES, quiet=True
    )
    if proxy and ip:
        with _proxies_lock:
            _cycle_ips.add(ip)
        return proxy
    return None


def assign_proxy(
    session: Any | None = None,
    *,
    max_attempts: int = 3,
    market: str | None = None,
) -> dict[str, str] | None:
    """Rotate to a pretest-ed unique-IP proxy; retry up to 3 times if pick fails."""
    proxy = None
    for attempt in range(1, max_attempts + 1):
        proxy = _next_market_proxy(market) if market else next_proxy()
        if proxy:
            if session is not None:
                session._rm_proxies = proxy  # type: ignore[attr-defined]
            return proxy
        print(
            f"[PROXIES] assign failed ({attempt}/{max_attempts}) — retrying",
            flush=True,
        )
    if session is not None:
        session._rm_proxies = None  # type: ignore[attr-defined]
    return None


def rotate_proxy() -> dict[str, str] | None:
    """Alias for next_proxy (live-tested + unique IP in cycle)."""
    return next_proxy()
