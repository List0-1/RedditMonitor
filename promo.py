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
    *,
    zip_attempts: int = 3,
) -> tuple[dict[str, Any], str]:
    """HAR boxprice batch. Returns (payload, zip_used).

    Requires a ZIP from the exit IP (ip-api). If ZIP is unavailable, blacklist
    the current proxy, swap, and retry until zip_attempts is exhausted.
    """
    from geo import zipcode_for_exit_ip
    from proxies import get_active_ip, swap_proxy_on_failure

    forced_zip = str(zip_code).strip() if zip_code else ""
    resolved_zip = forced_zip
    proxies = None
    exit_ip: str | None = None

    for attempt in range(1, max(1, zip_attempts) + 1):
        proxies = _fresh_proxies(session)
        exit_ip = get_active_ip()
        resolved_zip = forced_zip or (zipcode_for_exit_ip(exit_ip) or "")
        if resolved_zip:
            break
        reason = f"zip_code unavailable (exit IP {exit_ip or '?'})"
        print(f"    {reason} — swapping proxy ({attempt}/{zip_attempts})", flush=True)
        swap_proxy_on_failure(reason=reason)

    if not resolved_zip:
        raise RuntimeError(
            f"zip_code unavailable after {zip_attempts} proxies "
            f"(last exit IP {exit_ip or '?'})"
        )

    params: dict[str, str] = {
        "hfCountryCode": country,
        "hfPublicID": public_id or str(uuid.uuid4()),
        "productIds": ",".join(product_ids or DEFAULT_PRODUCT_IDS),
        "voucherCode": code,
        "zipCode": str(resolved_zip).strip(),
    }

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
    return resp.json(), str(resolved_zip).strip()


def summarize_box_prices(batch: dict[str, Any]) -> dict[str, Any]:
    """From prospect/batch, find free-box configs (box $0 in checkout UI).

    Selection rule (matches HelloFresh plan UI test):
      1) Fix Servings per recipe = 2 (default)
      2) Increase Your recipes per week 2→3→4→5→6
      3) Keep the last recipes/week that is still free (implied box $0)

    Free shipping alone does NOT count — e.g. 5×2 can be $9.90 box + $0 ship.
    """
    configs: list[dict[str, Any]] = []
    free: list[dict[str, Any]] = []
    default_servings = 2  # Servings per recipe default

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
        ship_due = round(max(shipping - shipping_discount, 0.0), 2)
        # Match checkout UI: box due ≈ total − shipping due
        implied_box = round(total - ship_due, 2)
        row = {
            "sku": sku,
            "meals": meals,
            "people": people,
            "price": round(price, 2),
            "discount": round(discount, 2),
            "box_price": box,
            "implied_box": implied_box,
            "shipping": round(shipping, 2),
            "shipping_discount": round(shipping_discount, 2),
            "shipping_due": ship_due,
            "total": round(total, 2),
        }
        configs.append(row)
        if abs(implied_box) < 0.01:
            free.append(row)

    configs.sort(key=lambda r: (r["meals"], r["people"]))
    free.sort(key=lambda r: (r["meals"], r["people"]))

    # Walk recipes/week upward at servings_per_recipe=2; keep last free
    free_at_default = [r for r in free if r["people"] == default_servings]
    best = None
    max_meals = None
    if free_at_default:
        # Already sorted by meals; last free meals value is the max still free
        best = max(free_at_default, key=lambda r: r["meals"])
        max_meals = best["meals"]

    return {
        "recipes_per_week": max_meals,  # UI: Your recipes per week
        "servings_per_recipe": default_servings if max_meals is not None else None,
        "best_free": best,
        "free_configs": free,
        "all_configs": configs,
        # legacy aliases
        "max_free_meals": max_meals,
    }


def _is_proxy_error(err: str | Exception | None) -> bool:
    text = str(err or "").lower()
    markers = (
        "timed out",
        "timeout",
        "connect tunnel failed",
        "curl: (28)",
        "curl: (56)",
        "curl: (7)",
        "curl: (35)",
        "proxy",
        "503",
        "502",
        "connection reset",
        "connection refused",
        "ssl",
        "failed to perform",
    )
    return any(m in text for m in markers)


def _is_permanent_voucher_miss(err: str | Exception | None) -> bool:
    """HTTP 404 on /gw/vouchers — code itself is invalid, not a proxy issue."""
    text = str(err or "").lower()
    return "http error 404" in text or "http 404" in text or ": 404" in text


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
        "permanent": False,
        "code_invalid": False,
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
            result["transient"] = page.status_code in {429, 500, 502, 503, 504}
            result["permanent"] = page.status_code == 404
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
                if _is_permanent_voucher_miss(exc):
                    # Confirmed invalid / not-working code (save as active:false)
                    result["code_invalid"] = True
                else:
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
                # ZIP resolved — clear transient from voucher-only soft fails
                # if pricing metrics are present (checked by caller).
            except Exception as exc:  # noqa: BLE001
                notes.append(f"box pricing unavailable: {exc}")
                result["transient"] = True
                if "zip_code unavailable" in str(exc).lower():
                    # Force outer retry / proxy swap
                    result["error"] = "; ".join(notes + [str(exc)])

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


def pricing_metrics_from_result(result: dict[str, Any]) -> dict[str, Any] | None:
    """Require recipes_per_week, servings_per_recipe, shipping_at_max from API.

    Field mapping (HelloFresh UI):
      - recipes_per_week   = Your recipes per week
      - servings_per_recipe = Servings per recipe
      - shipping_at_max    = shipping due on that free-box config

    Only true free-box configs are included (box $0 in checkout UI).
    """
    pricing = result.get("box_pricing")
    if not isinstance(pricing, dict):
        return None
    meals = pricing.get("recipes_per_week")
    if meals is None:
        meals = pricing.get("max_free_meals")
    best = pricing.get("best_free")
    if meals is None or not isinstance(best, dict):
        return None
    servings = best.get("people")
    if servings is None:
        servings = pricing.get("servings_per_recipe")
    if servings is None:
        return None
    if "shipping" not in best and "shipping_discount" not in best and "shipping_due" not in best:
        return None
    try:
        meals_i = int(meals)
        servings_i = int(servings)
        ship_fee = float(best.get("shipping") or 0)
        ship_disc = float(best.get("shipping_discount") or 0)
        if best.get("shipping_due") is not None:
            ship_due = float(best.get("shipping_due"))
        else:
            ship_due = round(max(ship_fee - ship_disc, 0.0), 2)
    except (TypeError, ValueError):
        return None
    return {
        "recipes_per_week": meals_i,
        "servings_per_recipe": servings_i,
        "shipping_at_max": round(ship_due, 2),
        "shipping_fee": round(ship_fee, 2),
        "shipping_discount": round(ship_disc, 2),
    }


def is_confirmed_dead_code(result: dict[str, Any]) -> bool:
    """True when HelloFresh confirms the code does not work.

    Observed for dead codes:
      - GET /gw/vouchers/{code} → HTTP 404, voucher=null
      - box pricing may still return with recipes_per_week=None / best_free=None
      - OR voucher.is_active is False
    """
    if not result.get("promo_code"):
        return False
    voucher = result.get("voucher") or {}
    if voucher.get("is_active") is False:
        return True
    if result.get("code_invalid"):
        return True
    if _is_permanent_voucher_miss(result.get("error")):
        return True
    return False


def has_required_api_pricing(result: dict[str, Any]) -> bool:
    """True when ZIP + the 3 pricing metrics are all present from the API path."""
    zip_code = str(result.get("zip_code") or "").strip()
    if not zip_code:
        return False
    return pricing_metrics_from_result(result) is not None


def resolve_share_link_with_retries(
    share_url: str,
    *,
    session: crequests.Session | None = None,
    country: str = "US",
    locale: str = "en-US",
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Resolve share link; require pricing metrics; swap proxy and retry up to 3x."""
    import time

    from proxies import swap_proxy_on_failure

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
                    "permanent": False,
                }

            last = promo
            code = promo.get("promo_code")
            active = (promo.get("voucher") or {}).get("is_active")
            err = promo.get("error") or ""
            complete = has_required_api_pricing(promo)
            dead = is_confirmed_dead_code(promo)

            # Confirmed not-working (404 / inactive) — stop; caller saves active:false
            if code and dead:
                # Prefer having a ZIP on the doc; one more swap if pricing never ran
                if not str(promo.get("zip_code") or "").strip() and attempt < max_attempts:
                    swap_proxy_on_failure(reason="dead code missing zip_code")
                    print(
                        f"    dead code {code} — retry {attempt}/{max_attempts} for zip_code",
                        flush=True,
                    )
                    time.sleep(0.4)
                    continue
                promo["incomplete"] = False
                promo["dead"] = True
                print(f"    confirmed dead code ({code}) — will save active:false", flush=True)
                return promo

            # Confirmed inactive with full pricing — stop
            if code and active is False and complete:
                promo["incomplete"] = False
                promo["dead"] = True
                return promo

            # Share-link hard miss (no promo code, page 404) — stop
            if promo.get("permanent") and not code:
                print(f"    permanent fail (no retry): {err or '404'}", flush=True)
                promo["incomplete"] = True
                return promo

            # Success: promo code + zip + the 3 required API pricing fields
            if code and complete:
                promo["incomplete"] = False
                return promo

            missing = (
                "missing zip_code or recipes_per_week/servings_per_recipe/shipping_at_max"
            )
            if not str(promo.get("zip_code") or "").strip():
                missing = (
                    f"zip_code unavailable (exit IP {promo.get('exit_ip') or '?'})"
                )
            reason = err or missing
            promo["incomplete"] = True
            promo["error"] = reason if not err else f"{err}; {missing}"

            if attempt >= max_attempts:
                print(
                    f"    give up after {max_attempts} tries — incomplete pricing, will not save",
                    flush=True,
                )
                return promo

            swap_proxy_on_failure(reason=reason[:120])
            print(
                f"    retry {attempt}/{max_attempts} after proxy swap: {reason}",
                flush=True,
            )
            time.sleep(0.4)
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
        lines.append(f"  zip_code: {result['zip_code']} (ip-api)")
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
        max_meals = pricing.get("recipes_per_week")
        if max_meals is None:
            max_meals = pricing.get("max_free_meals")
        best = pricing.get("best_free")
        if max_meals is None:
            lines.append("  recipes_per_week: none (no $0 box configs)")
            lines.append("  servings_per_recipe: none")
        else:
            people = best.get("people") if best else "?"
            lines.append(f"  recipes_per_week: {max_meals}  # Your recipes per week")
            lines.append(f"  servings_per_recipe: {people}  # Servings per recipe")
            if best:
                if best.get("shipping_due") is not None:
                    ship_due = float(best.get("shipping_due"))
                    ship = float(best.get("shipping") or 0)
                    ship_disc = float(best.get("shipping_discount") or 0)
                else:
                    ship = float(best.get("shipping") or 0)
                    ship_disc = float(best.get("shipping_discount") or 0)
                    ship_due = round(max(ship - ship_disc, 0.0), 2)
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
