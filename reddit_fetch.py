"""Fetch Reddit threads with pure HTTP (no browser / no manual cookies).

Reddit serves a lightweight JS challenge, then often hard-blocks Chrome TLS
impersonation and the /.json API (HTTP 403). This module:

1. Uses curl_cffi with rotating browser TLS fingerprints
2. Solves the JS challenge: solution = challenge_hex + challenge_hex
3. Tries old.reddit HTML, then /.json; if both fail, waits 3 minutes,
   rotates TLS + picks a pretested proxy with a new exit IP, and retries
   until success
"""

from __future__ import annotations

import re
import time
from html import unescape
from typing import Any, Callable, TypeVar
from urllib.parse import urljoin, urlparse

from curl_cffi import requests as crequests

DEFAULT_IMPERSONATE = "safari180"
RETRY_SLEEP_SECS = 180  # 3 minutes between full HTML+JSON failures
TLS_ROTATION = (
    "safari180",
    "safari184",
    "chrome142",
    "chrome145",
    "chrome136",
    "firefox135",
    "safari172_ios",
    "chrome133a",
)
SAFARI_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/18.0 Safari/605.1.15"
)
CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
)
FIREFOX_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:135.0) "
    "Gecko/20100101 Firefox/135.0"
)
BROWSER_UA = SAFARI_UA  # back-compat alias
T = TypeVar("T")
CHALLENGE_RE = re.compile(
    r"await\(async e=>e\+e\)\(\"([0-9a-fA-F]+)\"\)"
)
FORM_ACTION_RE = re.compile(
    r'<form[^>]*action=["\']([^"\']*)["\']', re.IGNORECASE
)
INPUT_RE = re.compile(r"<input[^>]*>", re.IGNORECASE)
NAME_RE = re.compile(r'name=["\']([^"\']+)["\']', re.IGNORECASE)
VALUE_RE = re.compile(r'value=["\']([^"\']*)["\']', re.IGNORECASE)
COMMENT_THING_RE = re.compile(
    r'<div class=" thing id-t1_[^"]*"[^>]*data-fullname="t1_([^"]+)"[^>]*>',
    re.IGNORECASE,
)
ATTR_RE = re.compile(r'([a-zA-Z_:][\w:.-]*)="([^"]*)"')
MD_BODY_RE = re.compile(
    r'<div class="usertext-body[^"]*"[^>]*>\s*<div class="md">(.*?)</div>\s*</div>',
    re.IGNORECASE | re.DOTALL,
)
POST_THING_RE = re.compile(
    r'<div class=" thing id-t3_([^"]+)"[^>]*>',
    re.IGNORECASE,
)
TITLE_LINK_RE = re.compile(
    r'<a class="title[^"]*"\s+[^>]*href="([^"]+)"[^>]*>([^<]+)</a>',
    re.IGNORECASE,
)
SHARE_THREAD_TITLE_RE = re.compile(
    r"share\s+weekly\s+trial",
    re.IGNORECASE,
)
MORECHILDREN_ONCLICK_RE = re.compile(
    r"morechildren\s*\(\s*this\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']*)'\s*\)",
    re.IGNORECASE,
)
COMMENTS_COUNT_RE = re.compile(
    r'data-comments-count="(\d+)"',
    re.IGNORECASE,
)
T1_FULLNAME_RE = re.compile(r'data-fullname="t1_([a-z0-9]+)"', re.IGNORECASE)


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


def to_old_reddit_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host in {"old.reddit.com", "www.reddit.com", "reddit.com"}:
        netloc = "old.reddit.com"
    else:
        netloc = parsed.netloc
    return parsed._replace(netloc=netloc).geturl()


def to_www_reddit_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host in {"old.reddit.com", "www.reddit.com", "reddit.com"}:
        netloc = "www.reddit.com"
    else:
        netloc = parsed.netloc
    return parsed._replace(netloc=netloc).geturl()


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


def is_hard_block_response(resp: crequests.Response) -> bool:
    return resp.status_code == 403 and len(resp.text or "") > 50_000


def _ua_for_impersonate(impersonate: str) -> str:
    name = (impersonate or "").lower()
    if name.startswith("safari") or "ios" in name:
        return SAFARI_UA
    if name.startswith("firefox"):
        return FIREFOX_UA
    return CHROME_UA


def create_session(
    impersonate: str = DEFAULT_IMPERSONATE,
    proxies: dict[str, str] | None = None,
) -> crequests.Session:
    session = crequests.Session(impersonate=impersonate)
    session.headers.update(
        {
            "User-Agent": _ua_for_impersonate(impersonate),
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


def _tls_for_round(round_n: int, preferred: str | None = None) -> str:
    if round_n <= 1 and preferred:
        return preferred
    return TLS_ROTATION[(round_n - 1) % len(TLS_ROTATION)]


def _pick_fresh_proxy(
    *,
    last_ip: str | None,
    fixed: dict[str, str] | None = None,
) -> tuple[dict[str, str] | None, str | None]:
    """Pick a pretested proxy whose exit IP differs from last_ip."""
    from proxies import (
        get_active_ip,
        mark_proxy_broken,
        next_proxy,
        proxy_label,
        test_proxy_exit_ip,
    )

    if fixed is not None:
        ip = test_proxy_exit_ip(fixed) or get_active_ip()
        print(
            f"[REDDIT] proxy fixed exit_ip={ip or '?'} ({proxy_label(fixed)})",
            flush=True,
        )
        return fixed, ip

    proxy: dict[str, str] | None = None
    ip: str | None = None
    for _ in range(12):
        proxy = next_proxy(prefer_different=True)
        ip = get_active_ip()
        if not proxy or not ip:
            continue
        if last_ip and ip == last_ip:
            mark_proxy_broken(proxy, reason=f"same exit IP as last Reddit attempt ({ip})")
            continue
        print(
            f"[REDDIT] proxy ok exit_ip={ip} ({proxy_label(proxy)})",
            flush=True,
        )
        return proxy, ip

    proxy = next_proxy(prefer_different=True)
    ip = get_active_ip()
    print(
        f"[REDDIT] proxy fallback exit_ip={ip or '?'} ({proxy_label(proxy)})",
        flush=True,
    )
    return proxy, ip


def _retry_until_works(
    label: str,
    attempt_fn: Callable[[crequests.Session], T],
    *,
    preferred_tls: str | None = None,
    fixed_proxy: dict[str, str] | None = None,
) -> T:
    """Run attempt_fn; on failure wait 3m, rotate TLS, new proxy IP, retry forever."""
    from proxies import begin_proxy_cycle

    last_ip: str | None = None
    last_error: Exception | None = None
    round_n = 0
    use_fixed = fixed_proxy

    while True:
        round_n += 1
        tls = _tls_for_round(round_n, preferred_tls)
        proxy, ip = _pick_fresh_proxy(last_ip=last_ip, fixed=use_fixed)
        if ip:
            last_ip = ip
        print(
            f"[REDDIT] {label} round {round_n} tls={tls} ip={ip or '?'}",
            flush=True,
        )
        sess = create_session(impersonate=tls, proxies=proxy)
        try:
            return attempt_fn(sess)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            _mark_reddit_proxy_broken(proxy, reason=str(exc)[:120])
            use_fixed = None  # never stick to a failing fixed proxy
            print(
                f"[REDDIT] {label} round {round_n} failed: {exc}",
                flush=True,
            )
            print(
                f"[REDDIT] both paths failed — sleep {RETRY_SLEEP_SECS}s, "
                f"then rotate TLS + new proxy IP (last_error={last_error})",
                flush=True,
            )
            time.sleep(RETRY_SLEEP_SECS)
            begin_proxy_cycle(f"reddit-retry-{label}-{round_n}")
        finally:
            sess.close()


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


def _get_html(session: crequests.Session, url: str, *, timeout: int = 20) -> crequests.Response:
    resp = session.get(
        url,
        proxies=_proxies_of(session),
        allow_redirects=True,
        timeout=timeout,
    )
    if is_verification_page(resp.text):
        resp = solve_verification(session, resp)
        if is_verification_page(resp.text):
            raise RuntimeError("Reddit verification failed (still on challenge page)")
    if is_hard_block_response(resp):
        raise RuntimeError(
            "Reddit returned a hard 403 block page. "
            "Try again later or from another network."
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Reddit HTML fetch failed: HTTP {resp.status_code}")
    return resp


def establish_reddit_session(
    session: crequests.Session,
    thread_url: str,
) -> None:
    """Hit the HTML thread once and clear the JS challenge if presented."""
    # old.reddit HTML is more scrape-friendly and often less aggressively blocked
    html_url = to_old_reddit_url(thread_html_url(thread_url))
    _get_html(session, html_url, timeout=15)


def _html_to_text(body_html: str) -> str:
    text = body_html
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text).replace("\xa0", " ").strip()


def _attrs_before(html: str, end: int) -> dict[str, str]:
    """Parse attributes from the opening tag that ends at end."""
    start = html.rfind("<", 0, end)
    if start < 0:
        return {}
    tag = html[start:end]
    return {m.group(1): m.group(2) for m in ATTR_RE.finditer(tag)}


def parse_old_reddit_comments(html: str) -> list[dict[str, Any]]:
    """Parse old.reddit comment HTML into Reddit listing-style t1 children.

    Walks every `thing id-t1_*` node (including nested replies). Body is taken
    from the first `.usertext-body .md` before the next t1 sibling/child.
    """
    children: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in COMMENT_THING_RE.finditer(html):
        cid = match.group(1)
        if cid in seen:
            continue
        seen.add(cid)
        attrs = _attrs_before(html, match.end())
        # Nested replies are themselves t1 things inside the parent HTML.
        # Cut at the next t1 so we only take this comment's own md body.
        next_m = COMMENT_THING_RE.search(html, match.end())
        chunk_end = next_m.start() if next_m else len(html)
        chunk = html[match.start() : chunk_end]
        md_m = MD_BODY_RE.search(chunk)
        body_html = md_m.group(1) if md_m else ""
        body = _html_to_text(body_html)
        ts_ms = attrs.get("data-timestamp") or "0"
        try:
            created = float(ts_ms) / 1000.0
        except ValueError:
            created = 0.0
        permalink = attrs.get("data-permalink") or ""
        children.append(
            {
                "kind": "t1",
                "data": {
                    "id": cid,
                    "author": attrs.get("data-author") or "[deleted]",
                    "body": body,
                    "body_html": body_html,
                    "created_utc": created,
                    "permalink": permalink,
                    "replies": "",
                },
            }
        )
    return children


def _thread_fullname_from_url(thread_url: str) -> str | None:
    m = re.search(r"/comments/([a-z0-9]+)/", thread_url, re.I)
    return f"t3_{m.group(1)}" if m else None


def _expand_morechildren(
    session: crequests.Session,
    html: str,
    *,
    thread_url: str,
) -> str:
    """Fetch truncated comment batches via old.reddit /api/morechildren."""
    link_fallback = _thread_fullname_from_url(thread_url) or ""
    batches: list[tuple[str, list[str]]] = []
    for m in MORECHILDREN_ONCLICK_RE.finditer(html):
        link_id = m.group(1) or link_fallback
        raw_ids = [x.strip() for x in m.group(2).split(",") if x.strip()]
        # onclick ids are bare (no t1_ prefix)
        child_ids = [x.removeprefix("t1_") for x in raw_ids]
        if link_id and child_ids:
            batches.append((link_id, child_ids))

    if not batches:
        return html

    print(f"[REDDIT] Expanding {len(batches)} morechildren batch(es)", flush=True)
    extra_html_parts: list[str] = []
    for link_id, child_ids in batches:
        # Reddit caps children per request; chunk to stay safe.
        for i in range(0, len(child_ids), 100):
            chunk = child_ids[i : i + 100]
            try:
                resp = session.post(
                    "https://old.reddit.com/api/morechildren",
                    proxies=_proxies_of(session),
                    data={
                        "api_type": "json",
                        "link_id": link_id,
                        "children": ",".join(chunk),
                        "sort": "new",
                        "limit_children": "False",
                    },
                    headers={
                        "Referer": to_old_reddit_url(thread_html_url(thread_url)),
                        "Origin": "https://old.reddit.com",
                        "X-Requested-With": "XMLHttpRequest",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    timeout=25,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[REDDIT] morechildren request failed: {exc}", flush=True)
                continue
            if resp.status_code != 200:
                print(
                    f"[REDDIT] morechildren HTTP {resp.status_code}",
                    flush=True,
                )
                continue
            try:
                payload = resp.json()
            except Exception:  # noqa: BLE001
                # Sometimes returns HTML fragments
                if "id-t1_" in (resp.text or ""):
                    extra_html_parts.append(resp.text)
                continue

            things = (
                ((payload.get("json") or {}).get("data") or {}).get("things")
                or payload.get("things")
                or []
            )
            for thing in things:
                if not isinstance(thing, dict):
                    continue
                data = thing.get("data") or {}
                # JSON morechildren may include rendered HTML content
                content_html = data.get("contentHTML") or data.get("content") or ""
                if content_html and "id-t1_" in content_html:
                    extra_html_parts.append(content_html)
                    continue
                # Or structured t1 data — synthesize a minimal HTML thing block
                if thing.get("kind") == "t1" and data.get("id"):
                    body = data.get("body") or ""
                    body_html = data.get("body_html") or ""
                    author = data.get("author") or "[deleted]"
                    permalink = data.get("permalink") or ""
                    created = data.get("created_utc") or 0
                    cid = data.get("id")
                    # Append as a fake thing so parse_old_reddit_comments can read it
                    # via a dedicated structured merge below instead.
                    extra_html_parts.append(
                        f'<div class=" thing id-t1_{cid}" data-fullname="t1_{cid}" '
                        f'data-author="{author}" data-permalink="{permalink}" '
                        f'data-timestamp="{int(float(created) * 1000)}">'
                        f'<div class="usertext-body"><div class="md">'
                        f"{body_html or body}</div></div></div>"
                    )

    if not extra_html_parts:
        return html
    return html + "\n" + "\n".join(extra_html_parts)


def _html_comment_coverage(html: str, comments: list[dict[str, Any]]) -> dict[str, int]:
    html_t1 = len(set(T1_FULLNAME_RE.findall(html)))
    claimed_m = COMMENTS_COUNT_RE.search(html)
    claimed = int(claimed_m.group(1)) if claimed_m else 0
    with_body = sum(
        1 for c in comments if str((c.get("data") or {}).get("body") or "").strip()
    )
    return {
        "html_t1": html_t1,
        "parsed": len(comments),
        "with_body": with_body,
        "claimed": claimed,
    }


def parse_old_reddit_posts(html: str) -> list[dict[str, Any]]:
    """Parse old.reddit listing HTML into thread info dicts."""
    posts: list[dict[str, Any]] = []
    for match in POST_THING_RE.finditer(html):
        attrs = _attrs_before(html, match.end())
        next_m = POST_THING_RE.search(html, match.end())
        chunk_end = next_m.start() if next_m else min(len(html), match.end() + 4000)
        chunk = html[match.start() : chunk_end]
        title_m = TITLE_LINK_RE.search(chunk)
        title = unescape(title_m.group(2)).strip() if title_m else ""
        href = title_m.group(1) if title_m else (attrs.get("data-permalink") or "")
        pid = attrs.get("data-fullname", "").removeprefix("t3_") or match.group(1)
        permalink = attrs.get("data-permalink") or href
        if permalink.startswith("/"):
            url = f"https://www.reddit.com{permalink}"
        elif permalink.startswith("http"):
            url = to_www_reddit_url(permalink)
        else:
            continue
        open_end = html.find(">", match.start(), match.end() + 20)
        open_tag = html[match.start() : open_end + 1] if open_end > 0 else ""
        stickied = "stickied" in (attrs.get("class") or open_tag)
        ts_ms = attrs.get("data-timestamp") or "0"
        try:
            created = float(ts_ms) / 1000.0
        except ValueError:
            created = 0.0
        try:
            num_comments = int(attrs.get("data-comments-count") or 0)
        except ValueError:
            num_comments = 0
        posts.append(
            {
                "url": url.rstrip("/") + "/",
                "title": title,
                "id": pid,
                "created_utc": created,
                "num_comments": num_comments,
                "author": attrs.get("data-author"),
                "stickied": stickied,
            }
        )
    return posts


def _mark_reddit_proxy_broken(proxy: dict[str, str] | None, reason: str) -> None:
    if not proxy:
        return
    try:
        from proxies import mark_proxy_broken

        mark_proxy_broken(proxy, reason=reason)
    except Exception:  # noqa: BLE001
        pass


def _fetch_thread_listing_json(
    session: crequests.Session,
    thread_url: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    establish_reddit_session(session, thread_url)
    json_url = thread_json_url(to_www_reddit_url(thread_url), limit=limit)
    resp = session.get(
        json_url,
        proxies=_proxies_of(session),
        headers={
            "Accept": "application/json,text/html,*/*",
            "Referer": thread_html_url(to_www_reddit_url(thread_url)),
        },
        allow_redirects=True,
        timeout=15,
    )
    if is_verification_page(resp.text):
        resp = solve_verification(session, resp)
        resp = session.get(
            json_url,
            proxies=_proxies_of(session),
            headers={
                "Accept": "application/json,text/html,*/*",
                "Referer": thread_html_url(to_www_reddit_url(thread_url)),
            },
            allow_redirects=True,
            timeout=15,
        )
    if is_hard_block_response(resp) or resp.status_code == 403:
        raise RuntimeError(
            "Reddit returned a hard 403 block page. "
            "Try again later or from another network."
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Reddit JSON fetch failed: HTTP {resp.status_code}")

    text = resp.text.lstrip()
    if not text.startswith("["):
        raise RuntimeError("Reddit did not return JSON (blocked or unexpected HTML)")

    payload = resp.json()
    if not isinstance(payload, list) or len(payload) < 2:
        raise RuntimeError("Unexpected Reddit JSON shape")
    return payload


def _fetch_thread_listing_html(
    session: crequests.Session,
    thread_url: str,
    *,
    limit: int = 500,
) -> list[dict[str, Any]]:
    html_url = to_old_reddit_url(thread_html_url(thread_url))
    qs = f"limit={limit}&depth=50&sort=new"
    if "?" in html_url:
        page_url = f"{html_url}&{qs}"
    else:
        page_url = f"{html_url.rstrip('/')}/?{qs}"
    print(f"[REDDIT] HTML fallback: {page_url}", flush=True)
    resp = _get_html(session, page_url, timeout=45)
    html = _expand_morechildren(session, resp.text, thread_url=thread_url)
    comments = parse_old_reddit_comments(html)
    if not comments:
        raise RuntimeError("old.reddit HTML parse found no comments")

    cov = _html_comment_coverage(html, comments)
    print(
        f"[REDDIT] HTML parsed {cov['parsed']} comments "
        f"(html_t1={cov['html_t1']} with_body={cov['with_body']} "
        f"post_claimed={cov['claimed']})",
        flush=True,
    )
    if cov["parsed"] < cov["html_t1"]:
        raise RuntimeError(
            f"HTML parse incomplete: parsed {cov['parsed']} < html_t1 {cov['html_t1']}"
        )
    if cov["claimed"] and cov["parsed"] < cov["claimed"]:
        # Reddit's count includes deleted/removed comments that are not rendered.
        print(
            f"[REDDIT] Note: {cov['claimed'] - cov['parsed']} claimed comments "
            f"not present in HTML (usually deleted/removed)",
            flush=True,
        )
    return [
        {"kind": "Listing", "data": {"children": []}},
        {"kind": "Listing", "data": {"children": comments}},
    ]


def fetch_thread_listing(
    thread_url: str,
    *,
    limit: int = 500,
    impersonate: str = DEFAULT_IMPERSONATE,
    session: crequests.Session | None = None,  # noqa: ARG001 — kept for API compat
    proxies: dict[str, str] | None = None,
    max_attempts: int = 3,  # noqa: ARG001 — kept for API compat; retries until success
) -> list[dict[str, Any]]:
    """
    Return Reddit's native comment listing JSON:
      [post_listing, comments_listing]

    Tries old.reddit HTML then /.json. If both fail, waits 3 minutes, rotates
    TLS fingerprint, picks a pretested proxy with a new exit IP, and retries
    until one path succeeds.
    """

    def _attempt(sess: crequests.Session) -> list[dict[str, Any]]:
        html_err: Exception | None = None
        json_err: Exception | None = None
        try:
            return _fetch_thread_listing_html(sess, thread_url, limit=limit)
        except Exception as exc:  # noqa: BLE001
            html_err = exc
            print(f"[REDDIT] HTML path failed: {exc}", flush=True)
        try:
            return _fetch_thread_listing_json(sess, thread_url, limit=limit)
        except Exception as exc:  # noqa: BLE001
            json_err = exc
            print(f"[REDDIT] JSON path failed: {exc}", flush=True)
        raise RuntimeError(
            f"HTML failed ({html_err}); JSON failed ({json_err})"
        )

    return _retry_until_works(
        "thread",
        _attempt,
        preferred_tls=impersonate,
        fixed_proxy=proxies,
    )


def fetch_reddit_json(
    url: str,
    *,
    referer: str | None = None,
    impersonate: str = DEFAULT_IMPERSONATE,
    max_attempts: int = 3,
) -> Any:
    """GET a Reddit JSON endpoint with challenge solve + proxy retries."""
    from proxies import next_proxy

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        proxy = next_proxy()
        print(f"[REDDIT] attempt {attempt}/{max_attempts}", flush=True)
        sess = create_session(impersonate=impersonate, proxies=proxy)
        try:
            html_seed = url
            if html_seed.endswith(".json"):
                html_seed = html_seed[: -len(".json")]
            if "?" in html_seed:
                html_seed = html_seed.split("?", 1)[0]
            if not html_seed.endswith("/"):
                html_seed += "/"
            establish_reddit_session(sess, html_seed)

            resp = sess.get(
                url,
                proxies=_proxies_of(sess),
                headers={
                    "Accept": "application/json,text/html,*/*",
                    "Referer": referer or html_seed,
                },
                allow_redirects=True,
                timeout=15,
            )
            if is_verification_page(resp.text):
                resp = solve_verification(sess, resp)
                resp = sess.get(
                    url,
                    proxies=_proxies_of(sess),
                    headers={
                        "Accept": "application/json,text/html,*/*",
                        "Referer": referer or html_seed,
                    },
                    allow_redirects=True,
                    timeout=15,
                )
            if is_hard_block_response(resp) or resp.status_code == 403:
                raise RuntimeError(
                    "Reddit returned a hard 403 block page. "
                    "Try again later or from another network."
                )
            if resp.status_code != 200:
                raise RuntimeError(f"Reddit JSON HTTP {resp.status_code}")
            text = resp.text.lstrip()
            if not (text.startswith("{") or text.startswith("[")):
                raise RuntimeError("Reddit did not return JSON")
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            _mark_reddit_proxy_broken(proxy, reason=str(exc)[:120])
            print(
                f"[REDDIT] proxy/request failed ({attempt}/{max_attempts}): {exc}",
                flush=True,
            )
            if attempt < max_attempts:
                time.sleep(0.8)
                continue
        finally:
            sess.close()
    raise RuntimeError(f"Reddit JSON fetch failed after {max_attempts} retries: {last_error}")


def _discover_share_threads_html(
    session: crequests.Session,
    subreddit: str,
) -> list[dict[str, Any]]:
    """Discover share-code threads via old.reddit HTML."""
    matches: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for path in (
        f"https://old.reddit.com/r/{subreddit}/hot/?limit=50",
        (
            f"https://old.reddit.com/r/{subreddit}/search"
            f"?q=title%3AShare+Weekly+Trial&restrict_sr=on&sort=new&limit=5"
        ),
    ):
        print(f"[REDDIT] HTML discover: {path}", flush=True)
        resp = _get_html(session, path, timeout=20)
        for post in parse_old_reddit_posts(resp.text):
            if not SHARE_THREAD_TITLE_RE.search(post.get("title") or ""):
                continue
            pid = str(post.get("id") or "")
            if pid and pid in seen_ids:
                continue
            if pid:
                seen_ids.add(pid)
            matches.append(post)
        if matches:
            break
    if not matches:
        raise RuntimeError(
            f"No HelloFresh share-codes thread found on r/{subreddit} (HTML)"
        )
    return matches


def _discover_share_threads_json(
    session: crequests.Session,
    subreddit: str,
) -> list[dict[str, Any]]:
    """Discover share-code threads via Reddit JSON (secondary path)."""
    establish_reddit_session(session, f"https://www.reddit.com/r/{subreddit}/")
    matches: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def _ingest(listing: dict[str, Any], *, stickied_only: bool) -> None:
        for node in ((listing.get("data") or {}).get("children")) or []:
            if node.get("kind") != "t3":
                continue
            data = node.get("data") or {}
            if stickied_only and not (data.get("stickied") or data.get("pinned")):
                continue
            title = str(data.get("title") or "")
            if not SHARE_THREAD_TITLE_RE.search(title):
                continue
            permalink = data.get("permalink") or ""
            if permalink.startswith("/"):
                url = f"https://www.reddit.com{permalink}"
            else:
                url = str(data.get("url") or "")
            if not url:
                continue
            pid = str(data.get("id") or "")
            if pid and pid in seen_ids:
                continue
            if pid:
                seen_ids.add(pid)
            matches.append(
                {
                    "url": url.rstrip("/") + "/",
                    "title": title,
                    "id": data.get("id"),
                    "created_utc": data.get("created_utc") or 0,
                    "num_comments": data.get("num_comments") or 0,
                    "author": data.get("author"),
                    "stickied": bool(data.get("stickied") or data.get("pinned")),
                }
            )

    hot_url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit=50&raw_json=1"
    resp = session.get(
        hot_url,
        proxies=_proxies_of(session),
        headers={
            "Accept": "application/json,text/html,*/*",
            "Referer": f"https://www.reddit.com/r/{subreddit}/",
        },
        allow_redirects=True,
        timeout=15,
    )
    if is_hard_block_response(resp) or resp.status_code == 403:
        raise RuntimeError(
            "Reddit returned a hard 403 block page. "
            "Try again later or from another network."
        )
    if resp.status_code != 200 or not (resp.text or "").lstrip().startswith("{"):
        raise RuntimeError(f"Reddit hot.json failed: HTTP {resp.status_code}")
    _ingest(resp.json(), stickied_only=True)

    if not matches:
        search_url = (
            f"https://www.reddit.com/r/{subreddit}/search.json"
            f"?q=title%3AShare+Weekly+Trial&restrict_sr=1&sort=new&limit=5&raw_json=1"
        )
        resp = session.get(
            search_url,
            proxies=_proxies_of(session),
            headers={
                "Accept": "application/json,text/html,*/*",
                "Referer": f"https://www.reddit.com/r/{subreddit}/",
            },
            allow_redirects=True,
            timeout=15,
        )
        if is_hard_block_response(resp) or resp.status_code != 200:
            raise RuntimeError(f"Reddit search.json failed: HTTP {resp.status_code}")
        if not (resp.text or "").lstrip().startswith("{"):
            raise RuntimeError("Reddit search.json did not return JSON")
        _ingest(resp.json(), stickied_only=False)

    if not matches:
        raise RuntimeError(
            f"No HelloFresh share-codes thread found on r/{subreddit} (JSON)"
        )
    return matches


def find_share_code_threads(
    subreddit: str = "hellofresh",
) -> list[dict[str, Any]]:
    """Find HelloFresh share-codes threads (stickied/pinned first, else search).

    Returns newest-first list of {url, title, id, ...}.
    Tries HTML then JSON; if both fail, waits 3 minutes with TLS + new-IP
    proxy rotation until discovery succeeds.
    """

    def _attempt(sess: crequests.Session) -> list[dict[str, Any]]:
        html_err: Exception | None = None
        json_err: Exception | None = None
        try:
            return _discover_share_threads_html(sess, subreddit)
        except Exception as exc:  # noqa: BLE001
            html_err = exc
            print(f"[REDDIT] HTML discover failed: {exc}", flush=True)
        try:
            return _discover_share_threads_json(sess, subreddit)
        except Exception as exc:  # noqa: BLE001
            json_err = exc
            print(f"[REDDIT] JSON discover failed: {exc}", flush=True)
        raise RuntimeError(
            f"HTML discover failed ({html_err}); JSON discover failed ({json_err})"
        )

    matches = _retry_until_works("discover", _attempt)

    stickied = [m for m in matches if m.get("stickied")]
    if stickied:
        matches = stickied

    matches.sort(key=lambda m: float(m.get("created_utc") or 0), reverse=True)
    print(f"[REDDIT] Found {len(matches)} share-codes thread(s):", flush=True)
    for m in matches:
        print(
            f"  • {m['title'][:60]} | comments={m['num_comments']} | {m['url']}",
            flush=True,
        )
    return matches


def find_pinned_share_thread(subreddit: str = "hellofresh") -> dict[str, Any]:
    """Newest share-codes thread (compat wrapper)."""
    return find_share_code_threads(subreddit=subreddit)[0]


def fetch_comments(
    thread_url: str,
    *,
    limit: int = 500,
    impersonate: str = DEFAULT_IMPERSONATE,
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
