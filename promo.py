"""Resolve HelloFresh share links to promo codes via HAR-captured flow.

Flow (TestPromo.har + hffindpromocode.har + boxprice.har):
1. GET /gw/share/{slug} -> redirect to landing/plans with ?c=PROMO_CODE
2. Read guest JWT from __NEXT_DATA__.props.pageProps.ssrPayload.serverAuth.access_token
3. GET /gw/vouchers/{code}?country=&locale=  (is_active, discount_settings)
4. POST /gw/voucher/validate  (checkout UI; guest customer_id=0)
   - error_code 1026 (customer_attachment_limit) is a hard fail — treat as
     dead/invalid for scan save (active:false, valid:false).
5. GET /gw/calculate/prospect/batch?voucherCode=&productIds=&zipCode=
   -> per-SKU pricing; find max recipes/week at implied box $0 (2 servings)
6. (UI only) GET /gw/price-presentation/v2/discount_communication/voucher/{code}

Scan "valid promo" = is_active is not False + zip + free-box pricing metrics
(recipes_per_week, servings_per_recipe, shipping_at_max). See is_valid_promo().
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


def _session_proxies(
    session: crequests.Session,
    *,
    market: str | None = None,
) -> dict[str, str] | None:
    """Reuse the proxy bound to this promo resolve (one proxy per code/attempt).

    Only assigns a new pretest-ed proxy when the session has none yet.
    """
    existing = _proxies_of(session)
    if existing is not None:
        return existing
    from proxies import assign_proxy

    mkt = market or getattr(session, "_rm_market", None)
    return assign_proxy(session, market=mkt)


def check_voucher(
    session: crequests.Session,
    code: str,
    access_token: str,
    referer: str,
    country: str = "US",
    locale: str = "en-US",
    origin: str = "https://www.hellofresh.com",
) -> dict[str, Any]:
    """HAR check-promo request: GET /gw/vouchers/{code}."""
    resp = session.get(
        f"{origin}/gw/vouchers/{code}",
        params={"country": country, "locale": locale},
        proxies=_session_proxies(session),
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


def validate_voucher(
    session: crequests.Session,
    code: str,
    access_token: str,
    referer: str,
    *,
    country: str = "US",
    locale: str = "en-US",
    product_ids: list[str] | None = None,
    origin: str = "https://www.hellofresh.com",
    customer_id: int = 0,
    subscription_id: int = 0,
) -> dict[str, Any]:
    """HAR checkout validate: POST /gw/voucher/validate.

    TestPromo.har uses guest ids (0). error_code 1026 (customer_attachment_limit)
    means the voucher is not usable for this scan — treated as validate hard-fail.
    """
    body = {
        "code": code,
        "country": country,
        "locale": locale,
        "systemCountry": country,
        "products": list(product_ids or DEFAULT_PRODUCT_IDS),
        "customer_id": customer_id,
        "subscription_id": subscription_id,
    }
    resp = session.post(
        f"{origin}/gw/voucher/validate",
        json=body,
        proxies=_session_proxies(session),
        headers=_browser_headers(
            Accept="application/json, text/plain, */*",
            Authorization=f"Bearer {access_token}",
            Referer=referer,
            Origin=origin,
            **{
                "Content-Type": "application/json",
                "x-requested-by": "client-platform",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            },
        ),
        timeout=15,
    )
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        payload = {"status": "error", "raw": (resp.text or "")[:500]}
    payload["_http_status"] = resp.status_code
    return payload


def summarize_voucher_validate(payload: dict[str, Any]) -> dict[str, Any]:
    """Compact validate response for logs / Mongo."""
    return {
        "http_status": payload.get("_http_status"),
        "status": payload.get("status"),
        "error_type": payload.get("error_type"),
        "error_code": payload.get("error_code"),
        "msg": payload.get("msg"),
    }


def is_soft_validate_error(payload: dict[str, Any] | None) -> bool:
    """True when validate failed for ignorable session reasons, not bad code.

    Note: 1026 / customer_attachment_limit is NOT soft — see is_validate_hard_fail.
    """
    if not payload:
        return False
    return False


def is_validate_hard_fail(payload: dict[str, Any] | None) -> bool:
    """True when validate says the voucher code itself is not usable."""
    if not payload:
        return False
    if payload.get("status") in {"success", "ok"}:
        return False
    if is_soft_validate_error(payload):
        return False
    err_type = str(payload.get("error_type") or "")
    code = str(payload.get("error_code") or "")
    # Unknown / generic validation noise — do not treat as dead code
    if err_type == "VOUCHER_VALIDATION_ERROR" and not code:
        return False
    # Explicit dead / unusable codes (guest validate + attachment limit)
    if code in {"1001", "1002", "1003", "1010", "1026"}:
        return True
    msg = str(payload.get("msg") or "").lower()
    if (
        "customer_attachment_limit" in msg
        or "attachment_limit" in msg
        or "not found" in msg
        or "invalid voucher" in msg
        or "does not exist" in msg
    ):
        return True
    return False


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
    origin: str = "https://www.hellofresh.com",
) -> tuple[dict[str, Any], str]:
    """HAR boxprice batch. Returns (payload, zip_used).

    ZIP from exit IP (ip-api), else NYC fallback 10001.
    """
    from geo import FALLBACK_ZIP, zipcode_for_exit_ip
    from proxies import get_active_ip

    del zip_attempts  # kept on signature for callers; ZIP always resolves via fallback
    proxies = _session_proxies(session)
    exit_ip = get_active_ip()
    forced_zip = str(zip_code).strip() if zip_code else ""
    # CA uses a Canadian postal fallback when geo lookup fails
    fallback = "H3X3S1" if str(country).upper() in {"CA", "CAD"} else FALLBACK_ZIP
    resolved_zip = forced_zip or (zipcode_for_exit_ip(exit_ip) or fallback)

    params: dict[str, str] = {
        "hfCountryCode": country,
        "hfPublicID": public_id or str(uuid.uuid4()),
        "productIds": ",".join(product_ids or DEFAULT_PRODUCT_IDS),
        "voucherCode": code,
        "zipCode": str(resolved_zip).strip(),
    }

    resp = session.get(
        f"{origin}/gw/calculate/prospect/batch",
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
    if resp.status_code >= 400:
        body = (resp.text or "")[:800].replace("\n", " ")
        raise RuntimeError(
            f"prospect/batch HTTP {resp.status_code} zip={resolved_zip} "
            f"code={code} body={body!r}"
        )
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
    market: str | None = None,
) -> dict[str, Any]:
    """Open share link, extract promo code, voucher details, and free-box max meals."""
    origin = "https://www.hellofresh.com"
    product_ids: list[str] | None = None
    if market:
        from market import get_market

        mkt = get_market(market)
        country = mkt["country"]
        locale = mkt["locale"]
        origin = mkt["origin"]
        product_ids = list(mkt["product_ids"])

    own_session = session is None
    session = session or create_hf_session(proxies=proxies)
    if proxies is not None:
        session._rm_proxies = proxies  # type: ignore[attr-defined]
    if market:
        session._rm_market = str(market).upper()  # type: ignore[attr-defined]
    # Bind one proxy for this entire resolve (share → voucher → validate → batch)
    _session_proxies(session, market=market)
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
        "voucher_validate": None,
    }
    notes: list[str] = []

    try:
        page = session.get(
            share_url,
            proxies=_session_proxies(session, market=market),
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
                    origin=origin,
                )
                result["voucher"] = summarize_voucher(voucher)
                try:
                    validate_payload = validate_voucher(
                        session,
                        code=code,
                        access_token=token,
                        referer=page.url,
                        country=country,
                        locale=locale,
                        product_ids=product_ids,
                        origin=origin,
                    )
                    result["voucher_validate"] = summarize_voucher_validate(
                        validate_payload
                    )
                    if is_validate_hard_fail(validate_payload):
                        result["code_invalid"] = True
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"voucher validate unavailable: {exc}")
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
                    product_ids=product_ids,
                    origin=origin,
                )
                result["zip_code"] = zip_used
                result["exit_ip"] = get_active_ip()
                result["box_pricing"] = summarize_box_prices(batch)
                result["box_pricing_raw"] = _summarize_batch_response(batch)
                # ZIP resolved — clear transient from voucher-only soft fails
                # if pricing metrics are present (checked by caller).
            except Exception as exc:  # noqa: BLE001
                notes.append(f"box pricing unavailable: {exc}")
                result["transient"] = True
                result["box_pricing_error"] = str(exc)
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
      - POST /gw/voucher/validate hard-fail (includes 1026 attachment limit)
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
    validate = result.get("voucher_validate")
    if isinstance(validate, dict) and is_validate_hard_fail(validate):
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


def is_valid_promo(result: dict[str, Any]) -> bool:
    """HAR-aligned "good promo" for voucher scan / BestVoucherCode.

    Requires:
      - not a confirmed dead code (404 / inactive / validate hard-fail)
      - voucher.is_active is not False when voucher details were returned
      - zip_code + recipes_per_week / servings_per_recipe / shipping_at_max
      - recipes_per_week > 0 and servings_per_recipe > 0 (free-box offer)
    """
    if not result.get("promo_code"):
        return False
    if is_confirmed_dead_code(result):
        return False
    voucher = result.get("voucher") or {}
    if voucher and voucher.get("is_active") is False:
        return False
    if not has_required_api_pricing(result):
        return False
    metrics = pricing_metrics_from_result(result)
    if not metrics:
        return False
    return metrics["recipes_per_week"] > 0 and metrics["servings_per_recipe"] > 0



def _summarize_batch_response(batch: dict[str, Any]) -> dict[str, Any]:
    """Compact prospect/batch payload for debug logs."""
    products = batch.get("products") if isinstance(batch, dict) else None
    if not isinstance(products, list):
        keys = list(batch.keys())[:20] if isinstance(batch, dict) else []
        return {"keys": keys, "products": 0, "sample": str(batch)[:400]}
    sample: list[dict[str, Any]] = []
    for product in products[:6]:
        if not isinstance(product, dict):
            continue
        sample.append(
            {
                "id": product.get("id"),
                "price": product.get("price"),
                "discount": product.get("discount"),
                "shippingFee": product.get("shippingFee"),
                "shippingDiscount": product.get("shippingDiscount"),
                "totalPrice": product.get("totalPrice"),
            }
        )
    return {
        "products": len(products),
        "sample": sample,
        "errors": batch.get("errors") or batch.get("error") or batch.get("message"),
    }


def incomplete_pricing_details(result: dict[str, Any]) -> dict[str, Any]:
    """What failed + response snippets for incomplete pricing retries."""
    pricing = result.get("box_pricing") if isinstance(result.get("box_pricing"), dict) else {}
    best = pricing.get("best_free") if isinstance(pricing, dict) else None
    missing: list[str] = []
    if not str(result.get("zip_code") or "").strip():
        missing.append("zip_code")
    if not isinstance(pricing, dict) or not pricing:
        missing.append("box_pricing")
    else:
        meals = pricing.get("recipes_per_week")
        if meals is None:
            meals = pricing.get("max_free_meals")
        if meals is None:
            missing.append("recipes_per_week")
        servings = None
        if isinstance(best, dict):
            servings = best.get("people")
        if servings is None:
            servings = pricing.get("servings_per_recipe")
        if servings is None:
            missing.append("servings_per_recipe")
        if not isinstance(best, dict):
            missing.append("best_free/shipping_at_max")
        elif (
            "shipping" not in best
            and "shipping_discount" not in best
            and "shipping_due" not in best
        ):
            missing.append("shipping_at_max")

    voucher = result.get("voucher") if isinstance(result.get("voucher"), dict) else {}
    return {
        "promo_code": result.get("promo_code"),
        "missing": missing,
        "zip_code": result.get("zip_code"),
        "exit_ip": result.get("exit_ip"),
        "error": result.get("error") or result.get("box_pricing_error"),
        "voucher_active": voucher.get("is_active") if voucher else None,
        "voucher_summary": {
            "discount_type": voucher.get("discount_type"),
            "discount_value": voucher.get("discount_value"),
            "channel": voucher.get("channel"),
        }
        if voucher
        else None,
        "box_pricing": {
            "recipes_per_week": pricing.get("recipes_per_week") if pricing else None,
            "servings_per_recipe": pricing.get("servings_per_recipe") if pricing else None,
            "best_free": best,
            "free_configs": len(pricing.get("free_configs") or []) if pricing else 0,
            "all_configs": len(pricing.get("all_configs") or []) if pricing else 0,
        },
        "box_pricing_raw": result.get("box_pricing_raw"),
        "box_pricing_error": result.get("box_pricing_error"),
    }


def print_incomplete_pricing_debug(result: dict[str, Any], *, prefix: str = "   ") -> None:
    """Print missing fields + API response when pricing is incomplete."""
    import json

    details = incomplete_pricing_details(result)
    print(
        f"{prefix}reason: missing={details['missing']} "
        f"code={details.get('promo_code')!r} zip={details.get('zip_code')!r} "
        f"exit_ip={details.get('exit_ip')!r}",
        flush=True,
    )
    if details.get("error"):
        print(f"{prefix}error: {details['error']}", flush=True)
    if details.get("voucher_summary"):
        print(f"{prefix}voucher: {details['voucher_summary']}", flush=True)
    print(f"{prefix}box_pricing: {details['box_pricing']}", flush=True)
    raw = details.get("box_pricing_raw") or details.get("box_pricing_error")
    if raw:
        try:
            text = json.dumps(raw, default=str) if not isinstance(raw, str) else raw
        except Exception:  # noqa: BLE001
            text = str(raw)
        print(f"{prefix}response: {text[:1200]}", flush=True)


def resolve_share_link_with_retries(
    share_url: str,
    *,
    session: crequests.Session | None = None,
    country: str = "US",
    locale: str = "en-US",
    max_attempts: int = 5,
    market: str | None = None,
) -> dict[str, Any]:
    """Resolve share link; one proxy per attempt; swap only between retries."""
    import time

    from proxies import assign_proxy, mark_proxy_broken

    own = session is None
    session = session or create_hf_session()
    if market:
        session._rm_market = str(market).upper()  # type: ignore[attr-defined]
    last: dict[str, Any] = {}

    def _bind_attempt_proxy() -> None:
        """One pretest-ed proxy for all HF calls in this promo attempt."""
        session._rm_proxies = None  # type: ignore[attr-defined]
        assign_proxy(session, market=market)

    try:
        for attempt in range(1, max_attempts + 1):
            _bind_attempt_proxy()
            try:
                promo = resolve_share_link(
                    share_url,
                    session=session,
                    country=country,
                    locale=locale,
                    market=market,
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
                    mark_proxy_broken(reason="dead code missing zip_code")
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

            # Success: HAR valid-promo criteria (active + free-box metrics + zip)
            if code and is_valid_promo(promo):
                promo["incomplete"] = False
                promo["valid"] = True
                return promo

            details = incomplete_pricing_details(promo)
            missing_fields = details.get("missing") or []
            missing = (
                "missing " + "/".join(missing_fields)
                if missing_fields
                else "incomplete pricing"
            )
            if not str(promo.get("zip_code") or "").strip():
                missing = (
                    f"zip_code unavailable (exit IP {promo.get('exit_ip') or '?'})"
                )
            reason = err or missing
            promo["incomplete"] = True
            promo["error"] = reason if not err else f"{err}; {missing}"
            promo["incomplete_details"] = details

            print(
                f"    incomplete pricing ({attempt}/{max_attempts}) "
                f"code={code!r}:",
                flush=True,
            )
            print_incomplete_pricing_debug(promo)

            # Got a valid batch with configs but no free-box metrics → code issue,
            # not a bad proxy. Don't burn the residential pool.
            pricing = promo.get("box_pricing") if isinstance(promo.get("box_pricing"), dict) else {}
            got_batch = bool(pricing.get("all_configs")) or bool(promo.get("box_pricing_raw"))
            proxy_problem = _is_proxy_error(err) or _is_proxy_error(
                promo.get("box_pricing_error")
            )
            if got_batch and not proxy_problem and "zip_code" not in missing_fields:
                print(
                    "    API responded but no free-box metrics — "
                    "not a proxy issue; will not save",
                    flush=True,
                )
                return promo

            if attempt >= max_attempts:
                print(
                    f"    give up after {max_attempts} tries — incomplete pricing, will not save",
                    flush=True,
                )
                return promo

            if proxy_problem or "zip_code" in missing_fields or not got_batch:
                mark_proxy_broken(reason=reason[:120])
                print(
                    f"    retry {attempt}/{max_attempts} after proxy swap: {reason}",
                    flush=True,
                )
            else:
                print(
                    f"    retry {attempt}/{max_attempts} (no proxy blacklist): {reason}",
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

    vv = result.get("voucher_validate")
    if vv is None:
        lines.append("  validate: (not run)")
    elif is_validate_hard_fail(vv):
        lines.append(
            f"  validate: REJECTED code={vv.get('error_code')} msg={vv.get('msg')}"
        )
    elif vv.get("http_status") in (200, 201) or vv.get("status") in ("success", "ok"):
        lines.append("  validate: ok")
    else:
        lines.append(
            f"  validate: HTTP {vv.get('http_status')} code={vv.get('error_code')} "
            f"msg={vv.get('msg')}"
        )

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
