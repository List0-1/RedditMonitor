"""Exit-IP geo helpers (same pattern as DDSignup.py).

Flow:
  proxy exit IP → ip-api.com (lat/lng)
  lat/lng → Uber mapsSearchV1 addresses
  parse US ZIP from addressLine2
"""

from __future__ import annotations

import re
import threading
import uuid
from typing import Any

from curl_cffi import requests as crequests

UBER_MAPS_SEARCH_URL = "https://www.ubereats.com/_p/api/mapsSearchV1"
ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

_geoip_cache: dict[str, dict[str, Any] | None] = {}
_geoip_lock = threading.Lock()
_zip_cache: dict[str, str | None] = {}
_zip_lock = threading.Lock()


def lookup_exit_geo(exit_ip: str) -> dict[str, Any] | None:
    """Resolve exit IP → {city, state, lat, lng, zip} via ip-api.com (cached)."""
    ip = str(exit_ip or "").strip()
    if not ip:
        return None
    with _geoip_lock:
        if ip in _geoip_cache:
            return _geoip_cache[ip]

    result: dict[str, Any] | None = None
    try:
        session = crequests.Session(impersonate="chrome131")
        resp = session.get(
            f"http://ip-api.com/json/{ip}",
            params={"fields": "status,city,region,lat,lon,zip"},
            timeout=8,
        )
        session.close()
        data = resp.json()
        if data.get("status") == "success":
            city = str(data.get("city") or "").strip()
            state = str(data.get("region") or "").strip().upper()
            lat = data.get("lat")
            lng = data.get("lon")
            zip_code = str(data.get("zip") or "").strip() or None
            if zip_code and ZIP_RE.fullmatch(zip_code.split("-")[0]):
                zip_code = zip_code.split("-")[0]
            if state and lat is not None and lng is not None:
                result = {
                    "city": city,
                    "state": state,
                    "lat": float(lat),
                    "lng": float(lng),
                    "zip": zip_code,
                }
    except Exception:  # noqa: BLE001
        result = None

    with _geoip_lock:
        _geoip_cache[ip] = result
    return result


def _uber_place_rank(item: dict[str, Any]) -> int:
    cats = {str(c) for c in (item.get("categories") or [])}
    if cats & {"RESIDENCE", "address_point", "addressPoint", "LANDMARK", "HOME_PRIVATE"}:
        return 0
    if "SHOPPING_CENTER_AND_MALL" in cats or "place" in cats:
        return 2
    return 1


def ubereats_maps_search(lat: float, lng: float) -> list[dict[str, Any]]:
    """Resolve lat/lng to nearby place dicts via Uber mapsSearchV1."""
    headers = {
        "sec-ch-ua-platform": '"macOS"',
        "x-csrf-token": "x",
        "Referer": "https://www.ubereats.com/",
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/147.0.0.0 Safari/537.36"
        ),
        "x-uber-ciid": str(uuid.uuid4()),
        "x-uber-request-id": str(uuid.uuid4()),
        "x-uber-session-id": str(uuid.uuid4()),
        "x-uber-client-gitref": uuid.uuid4().hex[:32],
    }
    try:
        session = crequests.Session(impersonate="chrome131")
        resp = session.post(
            UBER_MAPS_SEARCH_URL,
            headers=headers,
            json={"query": f"{lat},{lng}"},
            timeout=10,
        )
        session.close()
        payload = resp.json()
        if payload.get("status") != "success":
            return []
        items = [i for i in (payload.get("data") or []) if isinstance(i, dict)]
        return sorted(items, key=_uber_place_rank)
    except Exception:  # noqa: BLE001
        return []


def extract_zip_from_uber_places(places: list[dict[str, Any]]) -> str | None:
    """Pull first US ZIP from Uber addressLine1/addressLine2."""
    for item in places:
        for key in ("addressLine2", "addressLine1", "providerPlaceId"):
            text = str(item.get(key) or "")
            match = ZIP_RE.search(text)
            if match:
                return match.group(1)
    return None


def zipcode_for_exit_ip(exit_ip: str | None) -> str | None:
    """exit IP → lat/lng → Uber mapsSearch ZIP, else ip-api ZIP (cached)."""
    ip = str(exit_ip or "").strip()
    if not ip:
        return None
    with _zip_lock:
        if ip in _zip_cache:
            return _zip_cache[ip]

    zipcode: str | None = None
    geo = lookup_exit_geo(ip)
    if geo and geo.get("lat") is not None and geo.get("lng") is not None:
        places = ubereats_maps_search(float(geo["lat"]), float(geo["lng"]))
        zipcode = extract_zip_from_uber_places(places)
        if not zipcode:
            # Uber often omits ZIP in addressLine2 — fall back to ip-api zip
            zipcode = geo.get("zip")

    with _zip_lock:
        _zip_cache[ip] = zipcode
    return zipcode
