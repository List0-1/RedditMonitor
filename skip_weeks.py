"""Skip (pause) HelloFresh delivery weeks — keep first box, pause the rest.

From skipweek.har:
  GET  /gw/api/customers/me/deliveries
  PATCH /gw/api/subscriptions/{id}/delivery_dates/{week}
        body.status = "PAUSED"
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from hf_login import _auth_headers, _market_cfg, _session


def _iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _default_week_range(*, weeks_back: int = 2, weeks_forward: int = 12) -> tuple[str, str]:
    today = date.today()
    start = today - timedelta(weeks=weeks_back)
    end = today + timedelta(weeks=weeks_forward)
    return _iso_week(start), _iso_week(end)


def fetch_subscription_id(
    access_token: str,
    *,
    proxy: dict[str, str] | None = None,
    market: str = "US",
) -> int:
    mkt = _market_cfg(market)
    origin = mkt["origin"]
    session = _session(proxy)
    try:
        resp = session.get(
            f"{origin}/gw/api/customers/me/info",
            params={"country": mkt["country"], "locale": mkt["locale"]},
            headers=_auth_headers(
                access_token,
                mkt["code"],
                referer=f"{origin}/my-account/deliveries/menu",
                **{"x-requested-by": "client-platform"},
            ),
            timeout=25,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"me/info HTTP {resp.status_code}: {(resp.text or '')[:200]}"
            )
        data = resp.json()
        sub_id = data.get("activeSubscriptionId") or (data.get("subscriptionIds") or [None])[0]
        if not sub_id:
            raise RuntimeError("No activeSubscriptionId on me/info")
        return int(sub_id)
    finally:
        session.close()


def list_delivery_weeks(
    access_token: str,
    *,
    proxy: dict[str, str] | None = None,
    market: str = "US",
    range_start: str | None = None,
    range_end: str | None = None,
) -> list[dict[str, Any]]:
    mkt = _market_cfg(market)
    origin = mkt["origin"]
    if not range_start or not range_end:
        range_start, range_end = _default_week_range()
    session = _session(proxy)
    try:
        resp = session.get(
            f"{origin}/gw/api/customers/me/deliveries",
            params={
                "country": mkt["country"],
                "locale": mkt["locale"],
                "rangeStart": range_start,
                "rangeEnd": range_end,
            },
            headers=_auth_headers(
                access_token,
                mkt["code"],
                referer=f"{origin}/my-account/deliveries/menu",
                **{"x-requested-by": "reactivation"},
            ),
            timeout=25,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"me/deliveries HTTP {resp.status_code}: {(resp.text or '')[:200]}"
            )
        data = resp.json()
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return []
        return sorted(items, key=lambda w: str(w.get("id") or ""))
    finally:
        session.close()


def fetch_week_detail(
    access_token: str,
    subscription_id: int,
    week_id: str,
    *,
    proxy: dict[str, str] | None = None,
    market: str = "US",
) -> dict[str, Any]:
    mkt = _market_cfg(market)
    origin = mkt["origin"]
    session = _session(proxy)
    try:
        resp = session.get(
            f"{origin}/gw/api/subscriptions/{subscription_id}/delivery_dates/{week_id}",
            params={"country": mkt["country"], "locale": mkt["locale"]},
            headers=_auth_headers(
                access_token,
                mkt["code"],
                referer=f"{origin}/my-account/deliveries/menu",
                **{"x-requested-by": "merchandising-and-shopping-guidance"},
            ),
            timeout=25,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"delivery_dates/{week_id} HTTP {resp.status_code}: "
                f"{(resp.text or '')[:200]}"
            )
        return resp.json()
    finally:
        session.close()


def pause_week(
    access_token: str,
    subscription_id: int,
    week: dict[str, Any],
    *,
    proxy: dict[str, str] | None = None,
    market: str = "US",
) -> dict[str, Any]:
    """PATCH delivery week to status PAUSED (skip box)."""
    mkt = _market_cfg(market)
    origin = mkt["origin"]
    week_id = str(week.get("id") or "")
    if not week_id:
        raise ValueError("week missing id")

    cutoff = week.get("cutoffDate")
    delivery = week.get("deliveryDate")
    if not cutoff or not delivery:
        detail = fetch_week_detail(
            access_token,
            subscription_id,
            week_id,
            proxy=proxy,
            market=mkt["code"],
        )
        cutoff = cutoff or detail.get("cutoffDate")
        delivery = delivery or detail.get("deliveryDate")
        actions = detail.get("allowedActions") or {}
        if actions.get("pause") is False:
            raise RuntimeError(f"Week {week_id} pause not allowed")

    body = {
        "delivery": {
            "cutoffDate": cutoff,
            "deliveryDate": delivery,
            "status": "PAUSED",
            "subscriptionId": str(subscription_id),
            "id": week_id,
        }
    }
    session = _session(proxy)
    try:
        resp = session.patch(
            f"{origin}/gw/api/subscriptions/{subscription_id}/delivery_dates/{week_id}",
            params={"country": mkt["country"], "locale": mkt["locale"]},
            json=body,
            headers=_auth_headers(
                access_token,
                mkt["code"],
                referer=(
                    f"{origin}/my-account/deliveries/menu"
                    f"?week={week_id}&subscriptionId={subscription_id}"
                ),
                **{
                    "content-type": "application/json",
                    "x-requested-by": "merchandising-and-shopping-guidance",
                },
            ),
            timeout=25,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"pause {week_id} HTTP {resp.status_code}: {(resp.text or '')[:200]}"
            )
        return resp.json() if resp.text else {"id": week_id, "status": "PAUSED"}
    finally:
        session.close()


def _can_pause(week: dict[str, Any]) -> bool:
    actions = week.get("allowedActions")
    if isinstance(actions, dict) and "pause" in actions:
        return bool(actions.get("pause"))
    # If API omits allowedActions, treat RUNNING as pausable.
    return str(week.get("status") or "").upper() == "RUNNING"


def skip_all_weeks_except_first(
    access_token: str,
    *,
    proxy: dict[str, str] | None = None,
    market: str = "US",
    subscription_id: int | None = None,
) -> dict[str, Any]:
    """Keep the first RUNNING week; PAUSE every later pausable week.

    Matches the deliveries UI: first box ships, later weeks show skipped (X).
    """
    mkt = _market_cfg(market)
    sub_id = subscription_id or fetch_subscription_id(
        access_token, proxy=proxy, market=mkt["code"]
    )
    weeks = list_delivery_weeks(access_token, proxy=proxy, market=mkt["code"])
    running = [
        w
        for w in weeks
        if str(w.get("status") or "").upper() == "RUNNING" and w.get("id")
    ]
    if not running:
        print(f"[HF] skip-weeks ({mkt['code']}): no RUNNING weeks", flush=True)
        return {
            "subscription_id": sub_id,
            "kept_week": None,
            "paused_weeks": [],
            "failed_weeks": [],
        }

    kept = running[0]
    to_skip = running[1:]
    paused: list[str] = []
    failed: list[dict[str, str]] = []

    print(
        f"[HF] skip-weeks ({mkt['code']}): keep {kept.get('id')}, "
        f"pause {len(to_skip)} later week(s)",
        flush=True,
    )

    for week in to_skip:
        week_id = str(week["id"])
        if not _can_pause(week):
            print(f"[HF] skip-weeks: {week_id} pause not allowed — skip", flush=True)
            continue
        try:
            pause_week(
                access_token,
                sub_id,
                week,
                proxy=proxy,
                market=mkt["code"],
            )
            paused.append(week_id)
            print(f"[HF] skip-weeks: paused {week_id}", flush=True)
        except Exception as exc:  # noqa: BLE001
            failed.append({"week": week_id, "error": str(exc)})
            print(f"[HF] skip-weeks: failed {week_id}: {exc}", flush=True)

    return {
        "subscription_id": sub_id,
        "kept_week": kept.get("id"),
        "paused_weeks": paused,
        "failed_weeks": failed,
    }
