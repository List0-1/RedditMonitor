"""HelloFresh passwordless login + referral link fetch (US + CA)."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from curl_cffi import requests as crequests

from market import detect_market_from_url, get_market

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)

_CODE_RE = re.compile(r"[?&]code=([^&]+)")


def _market_cfg(market: str = "US") -> dict[str, Any]:
    return get_market(market)


def _origin(market: str = "US") -> str:
    return str(_market_cfg(market)["origin"])


def _redirect_url_encoded(market: str = "US") -> str:
    origin = _origin(market)
    return quote(f"{origin}/my-account/deliveries/menu", safe="")


def _session(proxy: dict[str, str] | None = None) -> crequests.Session:
    s = crequests.Session(impersonate="chrome131")
    if proxy:
        s.proxies = proxy
    return s


def _base_headers(market: str = "US", **extra: str) -> dict[str, str]:
    mkt = _market_cfg(market)
    headers = {
        "accept": "*/*",
        "accept-language": mkt.get("accept_language") or "en-US,en;q=0.9",
        "origin": mkt["origin"],
        "user-agent": UA,
        "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-request-id": str(uuid.uuid4()),
    }
    headers.update(extra)
    return headers


def _guest_token_from_json(data: Any) -> str | None:
    """access_token from passwordless/start JSON (flat or nested)."""
    if not isinstance(data, dict):
        return None
    for obj in (data, data.get("data"), data.get("token")):
        if not isinstance(obj, dict):
            continue
        token = obj.get("access_token")
        if isinstance(token, str) and token.startswith("eyJ"):
            return token
    return None


def request_passwordless_link(
    email: str,
    *,
    proxy: dict[str, str] | None = None,
    market: str = "US",
    country: str | None = None,
    locale: str | None = None,
) -> str | None:
    """POST /gw/v1/passwordless/start — triggers login email.

    Returns the guest JWT used for start (must be reused for magic-link/finish).
    """
    mkt = _market_cfg(market)
    country = country or mkt["country"]
    locale = locale or mkt["locale"]
    origin = mkt["origin"]
    target = (email or "").strip().lower()
    session = _session(proxy)
    try:
        login_resp = session.get(
            f"{origin}/login",
            headers=_base_headers(
                mkt["code"],
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                referer=f"{origin}/",
            ),
            timeout=25,
        )
        guest_token = extract_guest_token(login_resp.text or "")
        if not guest_token:
            home_resp = session.get(
                f"{origin}/",
                headers=_base_headers(
                    mkt["code"],
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    referer=f"{origin}/",
                ),
                timeout=25,
            )
            guest_token = extract_guest_token(home_resp.text or "")

        payload = {
            "email": target,
            "channel": "email",
            "send": "link",
            "redirect_url": _redirect_url_encoded(mkt["code"]),
            "public_id": str(uuid.uuid4()),
        }
        start_headers = _base_headers(
            mkt["code"],
            **{
                "content-type": "text/plain;charset=UTF-8",
                "referer": f"{origin}/login",
                "x-requested-by": "activations-rte",
            },
        )
        if guest_token:
            start_headers["authorization"] = f"Bearer {guest_token}"
        resp = session.post(
            f"{origin}/gw/v1/passwordless/start?country={country}&locale={locale}",
            data=json.dumps(payload, separators=(",", ":")),
            headers=start_headers,
            timeout=25,
        )
        if resp.status_code in (200, 204) and resp.text:
            try:
                body_token = _guest_token_from_json(resp.json())
            except json.JSONDecodeError:
                body_token = None
            if body_token:
                guest_token = body_token
        print(
            f"[HF] passwordless/start ({mkt['code']}) {target} → HTTP {resp.status_code} "
            f"(guest={'yes' if guest_token else 'no'})",
            flush=True,
        )
        if resp.status_code in (200, 204):
            return guest_token
        return None
    finally:
        session.close()


def extract_finish_code(url: str) -> str | None:
    if not url:
        return None
    qs = parse_qs(urlparse(url).query)
    if qs.get("code"):
        return qs["code"][0]
    m = _CODE_RE.search(url)
    return m.group(1) if m else None


def extract_guest_token(html: str) -> str | None:
    """Guest JWT from finish-page __NEXT_DATA__ (required for magic-link/finish)."""
    if not html:
        return None
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    token = (
        data.get("props", {})
        .get("pageProps", {})
        .get("ssrPayload", {})
        .get("serverAuth", {})
        .get("access_token")
    )
    if isinstance(token, str) and token.startswith("eyJ"):
        return token
    return None


def resolve_magic_link(
    login_url: str,
    *,
    proxy: dict[str, str] | None = None,
    market: str | None = None,
    prefer_start_guest: str | None = None,
) -> tuple[str, str, str]:
    """Follow click.link / finish URL → (code, guest_access_token, market).

    When ``prefer_start_guest`` is set (the JWT used for passwordless/start),
    return that token and only resolve the finish ``code`` — the finish-page
    SSR guest is a *new* session JWT and will 401 if used instead.
    """
    mkt_code = (market or detect_market_from_url(login_url) or "US").upper()
    origin = _origin(mkt_code)
    code = extract_finish_code(login_url)
    session = _session(proxy)
    try:
        if code and "passwordless/login/finish" in (login_url or ""):
            page_url = login_url
        elif code:
            page_url = f"{origin}/passwordless/login/finish?code={code}"
        else:
            page_url = login_url

        resp = session.get(
            page_url,
            headers=_base_headers(
                mkt_code,
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                referer=origin + "/",
            ),
            allow_redirects=True,
            timeout=30,
        )
        final = str(resp.url or "")
        mkt_code = detect_market_from_url(final) or mkt_code
        origin = _origin(mkt_code)
        code = extract_finish_code(final) or code
        if not code:
            code = extract_finish_code(resp.text or "")
        if not code:
            raise RuntimeError(f"No finish code in redirect chain ({final[:120]})")

        if prefer_start_guest:
            return code, prefer_start_guest, mkt_code

        guest = extract_guest_token(resp.text or "")
        if not guest:
            # Retry explicit finish page if we landed elsewhere
            resp2 = session.get(
                f"{origin}/passwordless/login/finish?code={code}",
                headers=_base_headers(
                    mkt_code,
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    referer=origin + "/",
                ),
                allow_redirects=True,
                timeout=30,
            )
            guest = extract_guest_token(resp2.text or "")
        if not guest:
            raise RuntimeError("No guest access_token in finish page __NEXT_DATA__")
        return code, guest, mkt_code
    finally:
        session.close()


def resolve_magic_link_code(
    login_url: str,
    *,
    proxy: dict[str, str] | None = None,
    market: str | None = None,
) -> str:
    """Back-compat: return only the finish JWT code."""
    code, _guest, _mkt = resolve_magic_link(login_url, proxy=proxy, market=market)
    return code


def finish_magic_link(
    code: str,
    *,
    guest_token: str,
    proxy: dict[str, str] | None = None,
    email: str | None = None,
    market: str = "US",
) -> dict[str, Any]:
    """GET magic-link/finish (Bearer guest JWT) → user access_token."""
    mkt = _market_cfg(market)
    origin = mkt["origin"]
    # HAR uses lowercase country on magic-link/finish (us / ca)
    country_q = str(mkt["country"]).lower()
    session = _session(proxy)
    try:
        params: dict[str, str] = {
            "channel": "email",
            "code": code,
            "country": country_q,
        }
        if email:
            params["email"] = email.strip().lower()
        resp = session.get(
            f"{origin}/gw/v1/passwordless/magic-link/finish",
            params=params,
            headers=_base_headers(
                mkt["code"],
                authorization=f"Bearer {guest_token}",
                referer=f"{origin}/passwordless/login/finish?code={code[:40]}",
                **{"x-requested-by": "reactivation"},
            ),
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"magic-link/finish HTTP {resp.status_code}: {(resp.text or '')[:200]}"
            )
        data = resp.json()
        if not data.get("access_token"):
            raise RuntimeError("magic-link/finish missing access_token")
        return data
    finally:
        session.close()


def _auth_headers(access_token: str, market: str = "US", **extra: str) -> dict[str, str]:
    return _base_headers(
        market,
        authorization=f"Bearer {access_token}",
        **extra,
    )


def fetch_me_info(
    access_token: str,
    *,
    proxy: dict[str, str] | None = None,
    market: str = "US",
) -> dict[str, Any]:
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
        return resp.json()
    finally:
        session.close()


def fetch_referral_profile(
    access_token: str,
    customer_uuid: str,
    *,
    proxy: dict[str, str] | None = None,
    market: str = "US",
) -> dict[str, Any]:
    mkt = _market_cfg(market)
    origin = mkt["origin"]
    session = _session(proxy)
    try:
        resp = session.get(
            f"{origin}/gw/referrals/profile/{customer_uuid}",
            params={
                "country": mkt["country"],
                "locale": mkt["locale"],
                "sharing_client": "web",
            },
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
                f"referrals/profile HTTP {resp.status_code}: {(resp.text or '')[:200]}"
            )
        return resp.json()
    finally:
        session.close()


def fetch_helloshare_link(
    access_token: str,
    *,
    customer_uuid: str,
    first_name: str,
    proxy: dict[str, str] | None = None,
    market: str = "US",
) -> dict[str, Any]:
    mkt = _market_cfg(market)
    origin = mkt["origin"]
    session = _session(proxy)
    try:
        resp = session.get(
            f"{origin}/gw/helloshare/v2/link",
            params={
                "country": mkt["country"],
                "customerFirstName": first_name or "Friend",
                "locale": mkt["locale"],
                "medium": "referral",
                "uuid": customer_uuid,
            },
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
                f"helloshare HTTP {resp.status_code}: {(resp.text or '')[:200]}"
            )
        return resp.json()
    finally:
        session.close()


def canonical_share_from_links(
    links: dict[str, Any] | None,
    code: str | None,
    *,
    market: str = "US",
) -> str:
    origin = _origin(market)
    if links:
        for key in ("copy_link", "referralLink", "native", "sms"):
            url = links.get(key)
            if isinstance(url, str) and "/gw/share/" in url:
                path = urlparse(url).path.rstrip("/")
                share_code = path.rsplit("/", 1)[-1]
                if share_code:
                    return f"{origin}/gw/share/{share_code}"
    if code:
        return f"{origin}/gw/share/{code}"
    return ""


def login_and_get_referral(
    email: str,
    login_url: str,
    *,
    proxy: dict[str, str] | None = None,
    market: str | None = None,
    start_guest_token: str | None = None,
) -> dict[str, Any]:
    """Finish passwordless login and return referral share link + metadata."""
    code, finish_guest, mkt_code = resolve_magic_link(
        login_url,
        proxy=proxy,
        market=market,
        prefer_start_guest=start_guest_token,
    )
    if start_guest_token:
        print(
            f"[HF] finish with start_guest (code={code[:12]}…)",
            flush=True,
        )
    else:
        print(
            f"[HF] finish with page_guest (code={code[:12]}…)",
            flush=True,
        )
    tokens = finish_magic_link(
        code,
        guest_token=finish_guest,
        proxy=proxy,
        email=email,
        market=mkt_code,
    )
    access_token = tokens["access_token"]
    user = tokens.get("user_data") or {}
    me = fetch_me_info(access_token, proxy=proxy, market=mkt_code)

    customer_uuid = me.get("uuid") or user.get("id") or ""
    first_name = me.get("firstName") or ""
    if not customer_uuid:
        raise RuntimeError("No customer uuid after login")

    profile: dict[str, Any] = {}
    helloshare: dict[str, Any] = {}
    try:
        profile = fetch_referral_profile(
            access_token, customer_uuid, proxy=proxy, market=mkt_code
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[HF] referrals/profile failed: {exc}", flush=True)
    try:
        helloshare = fetch_helloshare_link(
            access_token,
            customer_uuid=customer_uuid,
            first_name=first_name,
            proxy=proxy,
            market=mkt_code,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[HF] helloshare failed: {exc}", flush=True)

    invite_code = profile.get("invite_link_code")
    referral_link = canonical_share_from_links(
        profile.get("links"), invite_code, market=mkt_code
    )
    if not referral_link:
        referral_link = canonical_share_from_links(helloshare, None, market=mkt_code)
    if not referral_link:
        raise RuntimeError("No referral share link found")

    skipped_weeks: dict[str, Any] = {}
    try:
        from skip_weeks import skip_all_weeks_except_first

        skipped_weeks = skip_all_weeks_except_first(
            access_token,
            proxy=proxy,
            market=mkt_code,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[HF] skip-weeks failed: {exc}", flush=True)

    return {
        "email": (email or "").strip().lower(),
        "market": mkt_code,
        "access_token": access_token,
        "customer_uuid": customer_uuid,
        "first_name": first_name,
        "last_name": me.get("lastName"),
        "referral_link": referral_link,
        "invite_link_code": invite_code,
        "discount_voucher": profile.get("discount_voucher"),
        "referral_profile": profile,
        "helloshare": helloshare,
        "skipped_weeks": skipped_weeks,
    }


def fetch_referral_for_email(
    email: str,
    *,
    proxy: dict[str, str] | None = None,
    market: str = "US",
    max_rounds: int = 45,
    poll_seconds: int = 2,
) -> dict[str, Any]:
    """Full isolated flow: passwordless/start → Gmail link → login → referral.

    Does not touch MongoDB. Returns the same dict as ``login_and_get_referral``.
    """
    from datetime import datetime, timezone

    from gmail_imap import fetch_hellofresh_login_link

    target = (email or "").strip().lower()
    if not target or "@" not in target:
        raise ValueError(f"Invalid email: {email!r}")

    mkt = _market_cfg(market)
    after = datetime.now(timezone.utc)
    print(f"[HF] 1/3 passwordless/start ({mkt['code']}) → {target}", flush=True)
    start_guest = request_passwordless_link(target, proxy=proxy, market=mkt["code"])
    if not start_guest:
        raise RuntimeError("passwordless/start failed")

    print("[HF] 2/3 polling Gmail for login link…", flush=True)
    login_url = fetch_hellofresh_login_link(
        target,
        after_utc=after,
        max_rounds=max_rounds,
        poll_seconds=poll_seconds,
    )
    if not login_url:
        raise RuntimeError("No HelloFresh login link in Gmail")
    print(f"[HF] login link: {login_url[:80]}…", flush=True)

    print("[HF] 3/3 finishing login + fetching referral…", flush=True)

    def _finish(start_token: str | None, url: str) -> dict[str, Any]:
        return login_and_get_referral(
            target,
            url,
            proxy=proxy,
            market=mkt["code"],
            start_guest_token=start_token,
        )

    try:
        return _finish(start_guest, login_url)
    except Exception as exc:
        err = str(exc)
        err_l = err.lower()
        if "token does not match" not in err_l and "http 401" not in err_l:
            raise
        stale_code = extract_finish_code(login_url) or ""
        print(
            "[HF] magic-link token mismatch — restarting passwordless once "
            f"(excluding stale code={stale_code[:12]}…)",
            flush=True,
        )
        after = datetime.now(timezone.utc)
        start_guest = request_passwordless_link(target, proxy=proxy, market=mkt["code"])
        if not start_guest:
            raise RuntimeError("passwordless/start failed") from exc
        login_url = fetch_hellofresh_login_link(
            target,
            after_utc=after,
            max_rounds=max_rounds,
            poll_seconds=poll_seconds,
            exclude_links={login_url},
            exclude_codes={stale_code} if stale_code else None,
        )
        if not login_url:
            raise RuntimeError("No HelloFresh login link in Gmail") from exc
        print(f"[HF] login link (retry): {login_url[:80]}…", flush=True)
        return _finish(start_guest, login_url)
