"""Resolve HelloFresh share links to promo codes via HAR-captured flow.

Flow (from hffindpromocode.har + boxprice.har):
1. GET share URL  -> redirects to landing page with ?c=PROMO_CODE
2. Read guest JWT from __NEXT_DATA__.props.pageProps.ssrPayload.serverAuth.access_token
3. GET /gw/vouchers/{code}?country=US&locale=en-US  (optional details)
4. GET /gw/calculate/prospect/batch?voucherCode=...&productIds=US-CBU-...
   -> per-SKU box price after discount; find max meals/week still at $0

The promo code itself comes from step 1 (redirect ?c=). Steps 3–4 may fail
under rate limits — that is not fatal if promo_code is already known.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

from curl_cffi import requests as crequests

HF_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)

# Classic box unified SKUs from plans funnel (meals × people)
DEFAULT_MEALS = (2, 3, 4, 5, 6)
DEFAULT_PEOPLE = (2, 3, 4, 6)
DEFAULT_PRODUCT_IDS = [
    f"US-CBU-{meals}-{people}-0"
    for meals in DEFAULT_MEALS
    for people in DEFAULT_PEOPLE
]


def _browser_headers(**extra: str) -> dict[str, str]:
    headers = {
        "User-Agent": HF_UA,
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    }
    headers.update(extra)
    return headers


def extract_promo_code_from_url(url: str) -> str | None:
    """Pull promo code from redirect / landing URL query (?c=...)."""
    qs = parse_qs(urlparse(url).query)
    values = qs.get("c") or []
    if not values:
        return None
    code = values[0].strip()
    return code or None


def extract_access_token(html: str) -> str | None:
    match = NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None

    server_auth = (
        data.get("props", {})
        .get("pageProps", {})
        .get("ssrPayload", {})
        .get("serverAuth", {})
    )
    token = server_auth.get("access_token")
    if isinstance(token, str) and token:
        return token

    jwts = re.findall(r"eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+", html)
    return jwts[0] if jwts else None


def money_to_float(amount: dict[str, Any] | None) -> float:
    """HelloFresh money object {units, nanos} -> float dollars."""
    if not amount:
        return 0.0
    return float(amount.get("units") or 0) + float(amount.get("nanos") or 0) / 1e9


def parse_sku(sku: str) -> tuple[int, int] | None:
    """US-CBU-{meals}-{people}-0 -> (meals, people)."""
    parts = sku.split("-")
    if len(parts) < 4:
        return None
    try:
        return int(parts[2]), int(parts[3])
    except ValueError:
        return None


def summarize_voucher(voucher: dict[str, Any]) -> dict[str, Any]:
    """Keep the useful fields from /gw/vouchers response."""
    settings = voucher.get("discount_settings") or {}
    box_rule = ((settings.get("discount_rule") or {}).get("box_rule")) or {}
    boxes: dict[str, Any] = {}
    for box_num, rule in box_rule.items():
        cents = rule.get("discount_value")
        if isinstance(cents, (int, float)):
            boxes[str(box_num)] = f"${cents / 100:.2f}"
        else:
            boxes[str(box_num)] = cents

    return {
        "code": voucher.get("code"),
        "is_active": voucher.get("is_active"),
        "category": voucher.get("category"),
        "channel": voucher.get("channel"),
        "discount_type": voucher.get("discount_type"),
        "discount_value": voucher.get("discount_value"),
        "shipping_amount": voucher.get("shipping_amount"),
        "box_count": settings.get("box_count"),
        "box_discounts": boxes,
        "customer_status": voucher.get("customer_status"),
        "valid_from": voucher.get("valid_from"),
        "valid_to": voucher.get("valid_to"),
        "limit_per_code": voucher.get("limit_per_code"),
        "limit_per_subscription": voucher.get("limit_per_subscription"),
    }


def create_hf_session(
    impersonate: str = "chrome131",
    proxies: dict[str, str] | None = None,
) -> crequests.Session:
    session = crequests.Session(impersonate=impersonate)
    session.headers.update(_browser_headers())
    session._rm_proxies = proxies  # type: ignore[attr-defined]
    return session


def _proxies_of(session: crequests.Session) -> dict[str, str] | None:
    return getattr(session, "_rm_proxies", None)


def _fresh_proxies(session: crequests.Session) -> dict[str, str] | None:
    """Rotate to a new proxy for this HTTP call and attach it to the session."""
    from proxies import assign_proxy

    return assign_proxy(session)


def check_voucher(
    session: crequests.Session,
    code: str,
    access_token: str,
    referer: str,
    country: str = "US",
    locale: str = "en-US",
) -> dict[str, Any]:
    """HAR check-promo request: GET /gw/vouchers/{code}."""
    resp = session.get(
        f"https://www.hellofresh.com/gw/vouchers/{code}",
        params={"country": country, "locale": locale},
        proxies=_fresh_proxies(session),
        headers=_browser_headers(
            Accept="application/json, text/plain, */*",
            Authorization=f"Bearer {access_token}",
            Referer=referer,
            **{
                "x-requested-by": "client-platform",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            },
        ),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def calculate_prospect_batch(
    session: crequests.Session,
    code: str,
    access_token: str,
    referer: str,
    country: str = "US",
    product_ids: list[str] | None = None,
    public_id: str | None = None,
    zip_code: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    """HAR boxprice batch. Returns (payload, zip_used).

    Picks a fresh residential proxy, resolves its exit IP → lat/lng → ZIP via
    Uber mapsSearch (DDSignup pattern), then prices with that zipCode.
    """
    from geo import zipcode_for_exit_ip
    from proxies import get_active_ip

    proxies = _fresh_proxies(session)
    exit_ip = get_active_ip()
    resolved_zip = zip_code or zipcode_for_exit_ip(exit_ip)

    params: dict[str, str] = {
        "hfCountryCode": country,
        "hfPublicID": public_id or str(uuid.uuid4()),
        "productIds": ",".join(product_ids or DEFAULT_PRODUCT_IDS),
        "voucherCode": code,
    }
    if resolved_zip:
        params["zipCode"] = str(resolved_zip).strip()

    resp = session.get(
        "https://www.hellofresh.com/gw/calculate/prospect/batch",
        params=params,
        proxies=proxies,
        headers=_browser_headers(
            Accept="*/*",
            Authorization=f"Bearer {access_token}",
            Referer=referer,
            **{
                "x-requested-by": "activations-rte",
                "x-request-id": str(uuid.uuid4()),
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            },
        ),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json(), resolved_zip


def summarize_box_prices(batch: dict[str, Any]) -> dict[str, Any]:
    """From prospect/batch, find configs where box price (price-discount) is $0.

    Returns max meals/week still free, plus every free config.
    """
    configs: list[dict[str, Any]] = []
    free: list[dict[str, Any]] = []

    for product in batch.get("products") or []:
        sku = product.get("id") or ""
        parsed = parse_sku(sku)
        if not parsed:
            continue
        meals, people = parsed
        price = money_to_float(product.get("price"))
        discount = money_to_float(product.get("discount"))
        shipping = money_to_float(product.get("shippingFee"))
        shipping_discount = money_to_float(product.get("shippingDiscount"))
        total = money_to_float(product.get("totalPrice"))
        box = round(price - discount, 2)
        row = {
            "sku": sku,
            "meals": meals,
            "people": people,
            "price": round(price, 2),
            "discount": round(discount, 2),
            "box_price": box,
            "shipping": round(shipping, 2),
            "shipping_discount": round(shipping_discount, 2),
            "total": round(total, 2),
        }
        configs.append(row)
        if abs(box) < 0.01:
            free.append(row)

    configs.sort(key=lambda r: (r["meals"], r["people"]))
    free.sort(key=lambda r: (r["meals"], r["people"]))

    max_meals = max((r["meals"] for r in free), default=None)
    best = None
    if max_meals is not None:
        # Prefer highest people-count among max free meals (bigger free box)
        at_max = [r for r in free if r["meals"] == max_meals]
        best = max(at_max, key=lambda r: r["people"])

    return {
        "max_free_meals": max_meals,
        "best_free": best,
        "free_configs": free,
        "all_configs": configs,
    }


def resolve_share_link(
    share_url: str,
    session: crequests.Session | None = None,
    country: str = "US",
    locale: str = "en-US",
    check_details: bool = True,
    check_box_prices: bool = True,
    proxies: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Open share link, extract promo code, voucher details, and free-box max meals."""
    own_session = session is None
    session = session or create_hf_session(proxies=proxies)
    if proxies is not None:
        session._rm_proxies = proxies  # type: ignore[attr-defined]
    result: dict[str, Any] = {
        "share_url": share_url,
        "promo_code": None,
        "final_url": None,
        "voucher": None,
        "box_pricing": None,
        "zip_code": None,
        "exit_ip": None,
        "error": None,
        "transient": False,
    }
    notes: list[str] = []

    try:
        page = session.get(
            share_url,
            proxies=_fresh_proxies(session),
            headers=_browser_headers(Accept="text/html,application/xhtml+xml,*/*"),
            allow_redirects=True,
            timeout=10,
        )
        if page.status_code >= 400:
            result["error"] = f"Share link HTTP {page.status_code}"
            result["transient"] = True
            return result

        result["final_url"] = page.url

        code = extract_promo_code_from_url(page.url)
        if not code:
            for hist in page.history:
                loc = hist.headers.get("Location") or ""
                code = extract_promo_code_from_url(loc) or extract_promo_code_from_url(
                    hist.url
                )
                if code:
                    break
        if not code:
            # Often a soft-block / bad proxy HTML page — treat as retryable
            result["error"] = "No promo code (c=) found after following share link"
            result["transient"] = True
            return result

        result["promo_code"] = code

        if not check_details and not check_box_prices:
            return result

        token = extract_access_token(page.text)
        if not token:
            notes.append("no access token for voucher/box pricing")
            result["error"] = "; ".join(notes)
            result["transient"] = True
            return result

        # Exit IP tracked for logging; ZIP is resolved against the pricing-request proxy
        from proxies import get_active_ip

        result["exit_ip"] = get_active_ip()

        if check_details:
            try:
                voucher = check_voucher(
                    session,
                    code=code,
                    access_token=token,
                    referer=page.url,
                    country=country,
                    locale=locale,
                )
                result["voucher"] = summarize_voucher(voucher)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"voucher details unavailable: {exc}")
                result["transient"] = True

        if check_box_prices:
            try:
                batch, zip_used = calculate_prospect_batch(
                    session,
                    code=code,
                    access_token=token,
                    referer=page.url,
                    country=country,
                )
                result["zip_code"] = zip_used
                result["exit_ip"] = get_active_ip()
                result["box_pricing"] = summarize_box_prices(batch)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"box pricing unavailable: {exc}")
                result["transient"] = True

        if notes:
            result["error"] = "; ".join(notes)
    except Exception as exc:  # noqa: BLE001
        if not result["promo_code"]:
            result["error"] = str(exc)
            result["transient"] = True
        else:
            notes.append(str(exc))
            result["error"] = "; ".join(notes)
            result["transient"] = True
    finally:
        if own_session:
            session.close()

    return result


def resolve_share_link_with_retries(
    share_url: str,
    *,
    session: crequests.Session | None = None,
    country: str = "US",
    locale: str = "en-US",
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Resolve a share link; retry up to max_attempts on any proxy/transient failure."""
    import time

    own = session is None
    session = session or create_hf_session()
    last: dict[str, Any] = {}
    try:
        for attempt in range(1, max_attempts + 1):
            try:
                promo = resolve_share_link(
                    share_url,
                    session=session,
                    country=country,
                    locale=locale,
                )
            except Exception as exc:  # noqa: BLE001
                promo = {
                    "share_url": share_url,
                    "promo_code": None,
                    "error": str(exc),
                    "transient": True,
                }

            last = promo
            code = promo.get("promo_code")
            active = (promo.get("voucher") or {}).get("is_active")
            has_voucher = promo.get("voucher") is not None
            has_pricing = promo.get("box_pricing") is not None

            # Confirmed inactive — stop
            if code and active is False:
                return promo

            # Full success: code + voucher + box pricing, no transient flag
            if code and has_voucher and has_pricing and not promo.get("transient"):
                return promo

            # Soft success: code + voucher details (pricing optional soft-fail last attempt)
            if code and has_voucher and not promo.get("transient"):
                return promo

            # Last attempt: accept whatever we got if we at least have a code
            if attempt >= max_attempts and code:
                return promo

            err = promo.get("error") or "incomplete/proxy failure"
            print(
                f"    retry {attempt}/{max_attempts} (proxy/transient): {err}",
                flush=True,
            )
            time.sleep(0.6)
        return last
    finally:
        if own:
            session.close()


def format_offer_line(voucher: dict[str, Any]) -> str:
    """Human-readable offer summary, e.g. '$120 / $15 / $15 off boxes'."""
    boxes = voucher.get("box_discounts") or {}
    if boxes:
        ordered = sorted(
            boxes.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0
        )
        amounts = " / ".join(str(v) for _, v in ordered)
        return f"{amounts} off ({len(ordered)} boxes)"
    dtype = voucher.get("discount_type")
    dval = voucher.get("discount_value")
    if dtype and dval is not None:
        return f"{dtype} {dval}"
    return "details unknown"


def format_promo_result(result: dict[str, Any]) -> str:
    code = result.get("promo_code")
    if not code:
        return f"  error: {result.get('error') or 'no promo code'}"

    lines = [f"  promo_code: {code}"]
    if result.get("zip_code"):
        lines.append(f"  zip_code: {result['zip_code']} (from exit IP geo)")
    elif result.get("exit_ip"):
        lines.append(f"  zip_code: unavailable (exit IP {result['exit_ip']})")
    voucher = result.get("voucher")
    if voucher:
        lines.append(f"  offer: {format_offer_line(voucher)}")
        lines.append(f"  active: {voucher.get('is_active')}")
        lines.append(
            f"  discount: {voucher.get('discount_type')} "
            f"{voucher.get('discount_value')} | "
            f"channel={voucher.get('channel')}"
        )
        boxes = voucher.get("box_discounts") or {}
        if boxes:
            box_txt = ", ".join(f"box{k}={v}" for k, v in boxes.items())
            lines.append(f"  boxes: {box_txt}")

    pricing = result.get("box_pricing")
    if pricing:
        max_meals = pricing.get("max_free_meals")
        best = pricing.get("best_free")
        if max_meals is None:
            lines.append("  max_free_meals: none (no $0 box configs)")
            lines.append("  servings_at_max: none")
        else:
            people = best.get("people") if best else "?"
            lines.append(f"  max_free_meals: {max_meals}")
            lines.append(f"  servings_at_max: {people}")
            if best:
                ship = float(best.get("shipping") or 0)
                ship_disc = float(best.get("shipping_discount") or 0)
                ship_due = round(ship - ship_disc, 2)
                lines.append(
                    f"  shipping_at_max: ${ship_due:.2f} "
                    f"(fee ${ship:.2f} − discount ${ship_disc:.2f})"
                )
            free = pricing.get("free_configs") or []
            cfg = ", ".join(f"{r['meals']}m×{r['people']}p" for r in free)
            lines.append(f"  free_configs: {cfg}")

    if result.get("error") and (not voucher or not pricing):
        lines.append(f"  note: {result['error']}")
    return "\n".join(lines)
