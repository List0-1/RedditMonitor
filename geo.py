"""Exit-IP geo helpers.

Flow: proxy exit IP → ip-api.com → zip (+ lat/lng/city/state).
"""

from __future__ import annotations

import re
import threading
from typing import Any

from curl_cffi import requests as crequests

ZIP_RE = re.compile(r"^\d{5}$")

_geoip_cache: dict[str, dict[str, Any] | None] = {}
_geoip_lock = threading.Lock()


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
            zip_code = str(data.get("zip") or "").strip().split("-")[0] or None
            if zip_code and not ZIP_RE.fullmatch(zip_code):
                zip_code = None
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


def zipcode_for_exit_ip(exit_ip: str | None) -> str | None:
    """exit IP → ip-api.com ZIP (cached via lookup_exit_geo)."""
    ip = str(exit_ip or "").strip()
    if not ip:
        return None
    geo = lookup_exit_geo(ip)
    if not geo:
        return None
    return geo.get("zip")
