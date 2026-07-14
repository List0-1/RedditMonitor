"""Fetch Reddit thread JSON with pure HTTP (no browser / no manual cookies).

Reddit serves a lightweight JS challenge page, then blocks clients whose TLS
fingerprint does not look like a real browser. This module:

1. Uses curl_cffi to impersonate Chrome TLS
2. Solves the challenge: solution = challenge_hex + challenge_hex (from page JS)
3. GETs the form action with solution/token query params
4. Fetches {thread}/.json with the resulting session cookies
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse

from curl_cffi import requests as crequests

CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
CHALLENGE_RE = re.compile(
    r"await\(async e=>e\+e\)\(\"([0-9a-fA-F]+)\"\)"
)
FORM_ACTION_RE = re.compile(
    r'<form[^>]*action=["\']([^"\']*)["\']', re.IGNORECASE
)
INPUT_RE = re.compile(r"<input[^>]*>", re.IGNORECASE)
NAME_RE = re.compile(r'name=["\']([^"\']+)["\']', re.IGNORECASE)
VALUE_RE = re.compile(r'value=["\']([^"\']*)["\']', re.IGNORECASE)


def thread_json_url(thread_url: str, limit: int = 500) -> str:
    base = thread_url.rstrip("/")
    if base.endswith(".json"):
        return f"{base}?limit={limit}&raw_json=1"
    return f"{base}/.json?limit={limit}&raw_json=1"


def thread_html_url(thread_url: str) -> str:
    base = thread_url.rstrip("/")
    if base.endswith(".json"):
        base = base[: -len(".json")]
    return base + "/"


def _parse_hidden_inputs(html: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for tag in INPUT_RE.findall(html):
        name_m = NAME_RE.search(tag)
        if not name_m:
            continue
        val_m = VALUE_RE.search(tag)
        fields[name_m.group(1)] = val_m.group(1) if val_m else ""
    return fields


def is_verification_page(html: str) -> bool:
    return "Please wait for verification" in html or (
        "js_challenge" in html and CHALLENGE_RE.search(html) is not None
    )


def create_session(
    impersonate: str = "chrome131",
    proxies: dict[str, str] | None = None,
) -> crequests.Session:
    session = crequests.Session(impersonate=impersonate)
    session.headers.update(
        {
            "User-Agent": CHROME_UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
        }
    )
    # curl_cffi: attach for callers that pass session.proxies into get()
    session._rm_proxies = proxies  # type: ignore[attr-defined]
    return session


def _proxies_of(session: crequests.Session) -> dict[str, str] | None:
    return getattr(session, "_rm_proxies", None)


def solve_verification(session: crequests.Session, response: crequests.Response) -> crequests.Response:
    """Complete Reddit's GET js_challenge form and return the follow-up response."""
    html = response.text
    match = CHALLENGE_RE.search(html)
    if not match:
        raise RuntimeError("Reddit verification page missing challenge token")

    challenge = match.group(1)
    fields = _parse_hidden_inputs(html)
    fields["solution"] = challenge + challenge
    fields.setdefault("js_challenge", "1")

    action_m = FORM_ACTION_RE.search(html)
    action = action_m.group(1) if action_m else urlparse(response.url).path
    url = urljoin(response.url, action)

    return session.get(
        url,
        params=fields,
        proxies=_proxies_of(session),
        headers={
            "Referer": response.url,
            "Upgrade-Insecure-Requests": "1",
        },
        allow_redirects=True,
        timeout=10,
    )


def establish_reddit_session(
    session: crequests.Session,
    thread_url: str,
) -> None:
    """Hit the HTML thread once and clear the JS challenge if presented."""
    html_url = thread_html_url(thread_url)
    resp = session.get(
        html_url,
        proxies=_proxies_of(session),
        allow_redirects=True,
        timeout=10,
    )
    if is_verification_page(resp.text):
        resp = solve_verification(session, resp)
        if is_verification_page(resp.text):
            raise RuntimeError("Reddit verification failed (still on challenge page)")
    if resp.status_code == 403 and len(resp.text) > 50_000:
        raise RuntimeError(
            "Reddit returned a hard 403 block page. "
            "Try again later or from another network."
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Reddit HTML fetch failed: HTTP {resp.status_code}")


def fetch_thread_listing(
    thread_url: str,
    *,
    limit: int = 500,
    impersonate: str = "chrome131",
    session: crequests.Session | None = None,
    proxies: dict[str, str] | None = None,
    max_attempts: int = 3,
) -> list[dict[str, Any]]:
    """
    Return Reddit's native comment listing JSON:
      [post_listing, comments_listing]

    Retries up to max_attempts on proxy/network failures (new proxy each try).
    """
    import time

    from proxies import next_proxy

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        # Fresh proxy per attempt (sticky for challenge + JSON within the attempt)
        proxy = next_proxy() if proxies is None else proxies
        print(f"[REDDIT] attempt {attempt}/{max_attempts}", flush=True)

        own = session is None
        sess = session or create_session(impersonate=impersonate, proxies=proxy)
        sess._rm_proxies = proxy  # type: ignore[attr-defined]
        try:
            establish_reddit_session(sess, thread_url)
            json_url = thread_json_url(thread_url, limit=limit)
            resp = sess.get(
                json_url,
                proxies=_proxies_of(sess),
                headers={
                    "Accept": "application/json,text/html,*/*",
                    "Referer": thread_html_url(thread_url),
                },
                allow_redirects=True,
                timeout=10,
            )

            if is_verification_page(resp.text):
                resp = solve_verification(sess, resp)
                resp = sess.get(
                    json_url,
                    proxies=_proxies_of(sess),
                    headers={
                        "Accept": "application/json,text/html,*/*",
                        "Referer": thread_html_url(thread_url),
                    },
                    allow_redirects=True,
                    timeout=10,
                )

            if resp.status_code != 200:
                raise RuntimeError(
                    f"Reddit JSON fetch failed: HTTP {resp.status_code}"
                )

            text = resp.text.lstrip()
            if not text.startswith("["):
                raise RuntimeError(
                    "Reddit did not return JSON (blocked or unexpected HTML)"
                )

            payload = resp.json()
            if not isinstance(payload, list) or len(payload) < 2:
                raise RuntimeError("Unexpected Reddit JSON shape")
            return payload
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            print(
                f"[REDDIT] proxy/request failed "
                f"({attempt}/{max_attempts}): {exc}",
                flush=True,
            )
            if attempt < max_attempts:
                time.sleep(0.8)
                # Force a new proxy next loop (ignore caller-fixed proxies after fail)
                proxies = None
                continue
        finally:
            if own:
                sess.close()

    raise RuntimeError(
        f"Reddit fetch failed after {max_attempts} proxy retries: {last_error}"
    )


def fetch_comments(
    thread_url: str,
    *,
    limit: int = 500,
    impersonate: str = "chrome131",
    proxies: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return the raw comments listing children (t1 nodes, may include 'more')."""
    payload = fetch_thread_listing(
        thread_url,
        limit=limit,
        impersonate=impersonate,
        proxies=proxies,
    )
    return (payload[1].get("data") or {}).get("children") or []
