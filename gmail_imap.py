"""Gmail IMAP helpers for HelloFresh passwordless login emails (US + CA).

Backup inbox first: chaofengzhang90@gmail.com (forwarded copy)
Primary inbox second: williamturner19978@gmail.com

Robust IMAP (aligned with NewOTP):
- private sockets with connect/read timeout
- hard wall-clock timeout on SELECT / FETCH (kill socket on hang)
- 1h cooldown after too-many-connections / SELECT timeout
- header-first then body; only newest few UIDs
- SINCE 1 day + Date lookback (default 5m)
- close all connections when done
"""

from __future__ import annotations

import email as email_lib
import imaplib
import json
import os
import re
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from email import policy
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

WILLIAMTURNER_GMAIL_INBOX = os.environ.get(
    "GMAIL_IMAP_USER", "williamturner19978@gmail.com"
)
FALLBACK_GMAIL_INBOX = os.environ.get(
    "GMAIL_IMAP_FALLBACK_USER", "chaofengzhang90@gmail.com"
)
HELLOFRESH_LOGIN_SUBJECT = "Log in to HelloFresh"
HELLOFRESH_POLL_SECONDS = 2
IMAP_SOCK_TIMEOUT = 12
CONN_COOLDOWN_SEC = 3600  # skip inbox 1h after too-many-connections / SELECT hang
# Gmail IMAP OVERQUOTA / bandwidth suspension: typically ~1h, up to 24h.
OVERQUOTA_SLEEP_SECS = 90 * 60  # 1h 30m

# US HTML emails wrap the CTA in click.link.hellofresh.com/?qs=...
_HELLOFRESH_LOGIN_BTN_RE = re.compile(
    r'href=["\'](https://click\.link\.hellofresh\.com/\?qs=[^"\']+)["\'][^>]*'
    r"background-color:\s*#009646",
    re.IGNORECASE,
)
_HELLOFRESH_LOGIN_NEAR_SIGNIN_RE = re.compile(
    r'sign-in.{0,800}?href=["\'](https://click\.link\.hellofresh\.com/\?qs=[^"\']+)["\']',
    re.IGNORECASE | re.DOTALL,
)
_HELLOFRESH_LOGIN_QS_RE = re.compile(
    r"https://click\.link\.hellofresh\.com/\?qs=[^\"'\s<>]+",
    re.IGNORECASE,
)

# CA HTML emails use Iterable tracking links
_HELLOFRESH_GLOBAL_CLICK_RE = re.compile(
    r"https://links\.hellofresh\.global/a/click\?[^\s\"'<>]+",
    re.IGNORECASE,
)

# Direct finish URL (often plain-text body for CA; also used after redirects)
_PASSWORDLESS_FINISH_RE = re.compile(
    r"https://www\.hellofresh\.(?:com|ca)/passwordless/login/finish\?[^\s\"'<>]+",
    re.IGNORECASE,
)

_imap_json_cache: dict[str, Any] | None = None
_imap_password_cache: dict[str, str] = {}
# inbox (lower) → UTC datetime until which we refuse new IMAP logins
_INBOX_COOLDOWN_UNTIL: dict[str, datetime] = {}


def _imap_json_accounts() -> dict[str, Any]:
    global _imap_json_cache
    if _imap_json_cache is not None:
        return _imap_json_cache

    candidates = [
        os.environ.get("IMAP_JSON_PATH") or "",
        str(Path.home() / "Desktop/Old Files/Python/NewOTP/imap.json"),
        "imap.json",
    ]
    for path in candidates:
        if not path or not Path(path).is_file():
            continue
        try:
            data = json.loads(Path(path).read_text())
            accounts = data.get("email_accounts", data)
            if isinstance(accounts, dict):
                _imap_json_cache = accounts
                return _imap_json_cache
        except Exception as exc:  # noqa: BLE001
            print(f"[IMAP] Failed reading {path}: {exc}", flush=True)
    _imap_json_cache = {}
    return _imap_json_cache


def _password_from_account_entry(entry: Any) -> str | None:
    if isinstance(entry, str) and entry.strip():
        return entry.strip()
    if isinstance(entry, dict):
        for key in ("password", "app_password", "pass"):
            if entry.get(key):
                return str(entry[key]).strip()
    return None


def _load_imap_password_for(inbox: str) -> str | None:
    """Load app password for a specific Gmail inbox."""
    key = (inbox or "").strip().lower()
    if not key:
        return None
    if key in _imap_password_cache:
        return _imap_password_cache[key]

    primary = WILLIAMTURNER_GMAIL_INBOX.strip().lower()
    fallback = FALLBACK_GMAIL_INBOX.strip().lower()

    env_pw = ""
    if key == primary:
        env_pw = (
            os.environ.get("GMAIL_IMAP_PASSWORD")
            or os.environ.get("HELLOFRESH_GMAIL_APP_PASSWORD")
            or ""
        ).strip()
    elif key == fallback:
        env_pw = (
            os.environ.get("GMAIL_IMAP_FALLBACK_PASSWORD")
            or os.environ.get("GMAIL_IMAP_PASSWORD_FALLBACK")
            or ""
        ).strip()
    if env_pw:
        _imap_password_cache[key] = env_pw
        return env_pw

    accounts = _imap_json_accounts()
    for acc_key, entry in accounts.items():
        if str(acc_key).strip().lower() == key:
            pw = _password_from_account_entry(entry)
            if pw:
                _imap_password_cache[key] = pw
                return pw
    return None


def _imap_inboxes() -> list[tuple[str, str]]:
    """Return [(inbox, password), ...] — backup first (forwarded copy), then primary."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for inbox in (FALLBACK_GMAIL_INBOX, WILLIAMTURNER_GMAIL_INBOX):
        user = (inbox or "").strip()
        if not user:
            continue
        low = user.lower()
        if low in seen:
            continue
        pw = _load_imap_password_for(user)
        if not pw:
            print(f"[IMAP] No password for {user} — skipping inbox", flush=True)
            continue
        seen.add(low)
        out.append((user, pw))
    return out


def _is_dead_socket(exc_or_msg: object) -> bool:
    text = str(exc_or_msg).upper()
    return (
        "EOF" in text
        or "SSL" in text
        or "SOCKET" in text
        or "TIMED OUT" in text
        or "TIMEOUT" in text
        or "CONNECTION RESET" in text
        or "BROKEN PIPE" in text
        or "CANNOT READ" in text
    )


def _inbox_on_cooldown(inbox: str) -> bool:
    until = _INBOX_COOLDOWN_UNTIL.get((inbox or "").lower())
    if until is None:
        return False
    now = datetime.now(timezone.utc)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if now >= until:
        _INBOX_COOLDOWN_UNTIL.pop((inbox or "").lower(), None)
        return False
    return True


def _flag_inbox_cooldown(inbox: str, reason: str = "") -> None:
    until = datetime.now(timezone.utc) + timedelta(seconds=CONN_COOLDOWN_SEC)
    _INBOX_COOLDOWN_UNTIL[(inbox or "").lower()] = until
    print(
        f"[IMAP] flagged {inbox} — skip until {until.isoformat()} "
        f"({reason or 'cooldown'})",
        flush=True,
    )


def _clean_url(url: str) -> str:
    return unquote(url.strip().rstrip(".,;:!?*_~`\"')>]} "))


def extract_hellofresh_login_link(html: str) -> str | None:
    """Extract passwordless login URL from US or CA HelloFresh email body.

    Preference order:
    1. Direct ``/passwordless/login/finish?code=…`` (.com or .ca)
    2. US ``click.link.hellofresh.com/?qs=…`` CTA (prefer non-/u/)
    3. CA Iterable ``links.hellofresh.global/a/click?…`` (followed later)
    """
    if not html:
        return None
    text = html if isinstance(html, str) else str(html)

    finish = _PASSWORDLESS_FINISH_RE.findall(text)
    if finish:
        return _clean_url(finish[-1])

    m = _HELLOFRESH_LOGIN_BTN_RE.search(text)
    if m:
        return _clean_url(m.group(1))
    m = _HELLOFRESH_LOGIN_NEAR_SIGNIN_RE.search(text)
    if m:
        return _clean_url(m.group(1))
    links = _HELLOFRESH_LOGIN_QS_RE.findall(text)
    if links:
        cta = [u for u in links if "/u/" not in u.lower()]
        return _clean_url(cta[-1] if cta else links[-1])

    global_links = _HELLOFRESH_GLOBAL_CLICK_RE.findall(text)
    if global_links:
        return _clean_url(global_links[-1])

    return None


def _email_bodies(parsed: email_lib.message.Message) -> tuple[str, str]:
    """Return (html, plain) bodies when available."""
    html = ""
    plain = ""
    try:
        if parsed.is_multipart():
            for part in parsed.walk():
                ctype = (part.get_content_type() or "").lower()
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
                if ctype == "text/html" and not html:
                    html = text
                elif ctype == "text/plain" and not plain:
                    plain = text
        else:
            payload = parsed.get_payload(decode=True)
            if payload:
                charset = parsed.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
                ctype = (parsed.get_content_type() or "").lower()
                if ctype == "text/html":
                    html = text
                else:
                    plain = text
    except Exception:  # noqa: BLE001
        pass
    return html, plain


def _email_body(parsed: email_lib.message.Message) -> str:
    """Prefer a body that actually contains an extractable login link."""
    html, plain = _email_bodies(parsed)
    if extract_hellofresh_login_link(plain):
        if _PASSWORDLESS_FINISH_RE.search(plain or ""):
            return plain
    if extract_hellofresh_login_link(html):
        return html
    if extract_hellofresh_login_link(plain):
        return plain
    return html or plain or ""


def _recipients(parsed: email_lib.message.Message) -> str:
    parts: list[str] = []
    for header in (
        "To",
        "Delivered-To",
        "X-Original-To",
        "X-Forwarded-To",
        "X-Forwarded-For",
        "Cc",
    ):
        vals = parsed.get_all(header)
        if not vals:
            continue
        for val in vals:
            if val:
                parts.append(str(val))
    return " ".join(parts)


def _is_overquota(exc_or_msg: object) -> bool:
    text = str(exc_or_msg).upper()
    return (
        "OVERQUOTA" in text
        or "BANDWIDTH LIMITS" in text
        or "EXCEEDED COMMAND" in text
    )


def _is_conn_cap(exc_or_msg: object) -> bool:
    return "TOO MANY SIMULTANEOUS CONNECTIONS" in str(exc_or_msg).upper()


def _sleep_overquota() -> None:
    mins = OVERQUOTA_SLEEP_SECS // 60
    print(
        f"[IMAP] OVERQUOTA on all inboxes — sleeping {mins}m then retrying",
        flush=True,
    )
    time.sleep(OVERQUOTA_SLEEP_SECS)


def _is_hellofresh_login_mail(subj: str, from_l: str) -> bool:
    subj_l = (subj or "").lower()
    from_l = (from_l or "").lower()
    if "hellofresh" not in from_l and "hellofresh" not in subj_l:
        return False
    if (
        "log in to hellofresh" in subj_l
        or "magic login link" in subj_l
        or "login link" in subj_l
        or "passwordless" in subj_l
    ):
        return True
    return "hellofresh" in from_l


def _apply_timeout(mail: imaplib.IMAP4_SSL, timeout: float = IMAP_SOCK_TIMEOUT) -> None:
    try:
        if getattr(mail, "sock", None) is not None:
            mail.sock.settimeout(float(timeout))
        if getattr(mail, "sslobj", None) is not None:
            mail.sslobj.settimeout(float(timeout))
    except Exception:  # noqa: BLE001
        pass


def _kill_mail(mail: imaplib.IMAP4_SSL | None) -> None:
    if mail is None:
        return
    try:
        sock = getattr(mail, "sock", None) or getattr(mail, "sslobj", None)
        if sock is not None:
            try:
                sock.shutdown(2)
            except Exception:  # noqa: BLE001
                pass
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    try:
        mail.logout()
    except Exception:  # noqa: BLE001
        pass


def _close_mail(mail: imaplib.IMAP4_SSL | None) -> None:
    _kill_mail(mail)


def _connect(inbox: str, password: str) -> imaplib.IMAP4_SSL:
    prev = socket.getdefaulttimeout()
    socket.setdefaulttimeout(float(IMAP_SOCK_TIMEOUT))
    try:
        try:
            conn = imaplib.IMAP4_SSL(
                "imap.gmail.com", 993, timeout=IMAP_SOCK_TIMEOUT
            )
        except TypeError:
            conn = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    finally:
        socket.setdefaulttimeout(prev)
    conn.login(inbox, password)
    _apply_timeout(conn)
    return conn


def _imap_call(mail: imaplib.IMAP4_SSL, label: str, fn, *args, inbox: str = ""):
    """Run one IMAP call with hard wall-clock timeout; kill socket on hang."""
    _apply_timeout(mail)
    holder: dict[str, Any] = {}

    def _run() -> None:
        try:
            holder["result"] = fn(*args)
        except Exception as exc:  # noqa: BLE001
            holder["exc"] = exc

    th = threading.Thread(target=_run, daemon=True, name=f"hf-{label[:16]}")
    th.start()
    th.join(float(IMAP_SOCK_TIMEOUT) + 1.0)
    if th.is_alive():
        _kill_mail(mail)
        if inbox:
            _flag_inbox_cooldown(inbox, f"{label} timeout")
        raise TimeoutError(f"{label} timed out")
    if "exc" in holder:
        raise holder["exc"]
    return holder.get("result")


def _select_inbox(mail: imaplib.IMAP4_SSL, inbox: str) -> tuple[str, Any]:
    return _imap_call(
        mail, "SELECT", mail.select, "INBOX", True, inbox=inbox
    )


def _fetch_bytes(mail: imaplib.IMAP4_SSL, uid: str, spec: str, *, inbox: str) -> bytes | None:
    t_data = _imap_call(
        mail, f"FETCH-{uid}", mail.uid, "fetch", uid, spec, inbox=inbox
    )
    if not t_data:
        return None
    t, data = t_data
    if t != "OK" or not data:
        return None
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return None


def _search_login_link_in_mail(
    mail: imaplib.IMAP4_SSL,
    *,
    inbox: str,
    target: str,
    filter_time: datetime,
    date_since: str,
) -> str | None:
    """Search currently selected INBOX for a matching HelloFresh login link."""
    # Tight searches first — avoid huge UID lists on the backup inbox.
    searches = [
        f'(SUBJECT "{HELLOFRESH_LOGIN_SUBJECT}" TO "{target}" SINCE {date_since})',
        f'(SUBJECT "magic login link" TO "{target}" SINCE {date_since})',
        f'(FROM "h.hellofresh.com" TO "{target}" SINCE {date_since})',
        f'(FROM "hellofresh.com" TO "{target}" SINCE {date_since})',
        f'(FROM "hellofresh.global" TO "{target}" SINCE {date_since})',
        f'(TO "{target}" SINCE {date_since})',
    ]
    uid_set: list[str] = []
    seen: set[str] = set()
    for criteria in searches:
        try:
            _apply_timeout(mail)
            resp, data = mail.uid("search", None, criteria)
        except Exception as exc:  # noqa: BLE001
            if _is_dead_socket(exc):
                _kill_mail(mail)
                raise
            continue
        if resp != "OK" or not data or not data[0]:
            continue
        for uid_b in data[0].split():
            uid = uid_b.decode() if isinstance(uid_b, bytes) else str(uid_b)
            if uid not in seen:
                seen.add(uid)
                uid_set.append(uid)
        if uid_set and "SUBJECT" in criteria:
            break

    if not uid_set:
        return None

    # Newest first; keep tiny — FETCH on huge backup mailbox can hang.
    for uid in list(reversed(uid_set))[:3]:
        try:
            hdr_raw = _fetch_bytes(
                mail, uid, "(BODY.PEEK[HEADER])", inbox=inbox
            )
            if not hdr_raw:
                continue
            headers = email_lib.message_from_bytes(hdr_raw, policy=policy.default)
            date_header = headers.get("Date")
            if date_header:
                email_dt = parsedate_to_datetime(date_header)
                if email_dt.tzinfo is None:
                    email_dt = email_dt.replace(tzinfo=timezone.utc)
                if email_dt < filter_time:
                    continue
            recip_l = _recipients(headers).lower()
            to_l = (headers.get("To") or "").lower()
            local = target.split("@", 1)[0]
            if (
                target not in recip_l
                and target not in to_l
                and f"{local}@" not in recip_l
            ):
                continue
            subj = headers.get("Subject") or ""
            from_l = (headers.get("From") or "").lower()
            if not _is_hellofresh_login_mail(subj, from_l):
                continue

            body_raw = _fetch_bytes(
                mail, uid, "(BODY.PEEK[TEXT])", inbox=inbox
            )
            if not body_raw:
                body_raw = _fetch_bytes(
                    mail, uid, "(BODY.PEEK[])", inbox=inbox
                )
            if not body_raw:
                continue

            text = (
                body_raw.decode("utf-8", errors="replace")
                if isinstance(body_raw, bytes)
                else str(body_raw)
            )
            link = extract_hellofresh_login_link(text)
            if not link:
                try:
                    parsed = email_lib.message_from_bytes(
                        body_raw, policy=policy.default
                    )
                    link = extract_hellofresh_login_link(_email_body(parsed))
                except Exception:  # noqa: BLE001
                    pass
            if link:
                return link
        except TimeoutError:
            raise
        except Exception as exc:  # noqa: BLE001
            # One line only — do not print per-UID spam after socket dies.
            if _is_dead_socket(exc):
                print(
                    f"[IMAP] socket dead on {inbox} (uid={uid}): {exc}",
                    flush=True,
                )
                _kill_mail(mail)
                raise TimeoutError(str(exc)) from exc
            # Non-fatal parse/fetch issue for this uid only
            continue
    return None


def fetch_hellofresh_login_link(
    target_email: str,
    *,
    max_rounds: int = 45,
    lookback_minutes: int = 5,
    poll_seconds: int = HELLOFRESH_POLL_SECONDS,
    after_utc: datetime | None = None,
    exclude_links: set[str] | None = None,
    exclude_codes: set[str] | None = None,
) -> str | None:
    """Poll Gmail IMAP for HelloFresh passwordless LOGIN link (US or CA).

    Tries primary then fallback. Skips inboxes on 1h cooldown. Sleeps 90m only
    when every configured inbox is OVERQUOTA.

    ``exclude_links`` / ``exclude_codes`` skip magic links already tried (stale
    codes bound to a previous passwordless/start guest JWT).
    """
    target = (target_email or "").strip().lower()
    inboxes = _imap_inboxes()
    if not inboxes:
        raise RuntimeError(
            "Missing Gmail IMAP password "
            "(set GMAIL_IMAP_PASSWORD / GMAIL_IMAP_FALLBACK_PASSWORD or IMAP_JSON_PATH)"
        )

    # Same window as NewOTP: HelloFresh Date is often earlier than start return.
    # Stale links from prior starts are blocked via exclude_links/exclude_codes.
    filter_time = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    if after_utc is not None:
        if after_utc.tzinfo is None:
            after_utc = after_utc.replace(tzinfo=timezone.utc)
        filter_time = max(
            filter_time,
            after_utc - timedelta(minutes=lookback_minutes),
        )
    skip_links = {(_clean_url(u) if u else "") for u in (exclude_links or set())}
    skip_links.discard("")
    skip_codes = {c for c in (exclude_codes or set()) if c}
    date_since = (datetime.now() - timedelta(days=1)).strftime("%d-%b-%Y")
    sleep_s = max(1, int(poll_seconds))

    private_conns: dict[str, imaplib.IMAP4_SSL] = {}
    overquota: set[str] = set()

    def _ensure(inbox: str, password: str) -> imaplib.IMAP4_SSL | None:
        if _inbox_on_cooldown(inbox) or inbox.lower() in overquota:
            return None
        existing = private_conns.get(inbox)
        if existing is not None:
            try:
                _apply_timeout(existing)
                existing.noop()
                return existing
            except Exception:  # noqa: BLE001
                _kill_mail(existing)
                private_conns.pop(inbox, None)
        try:
            conn = _connect(inbox, password)
            private_conns[inbox] = conn
            return conn
        except Exception as exc:  # noqa: BLE001
            print(f"[IMAP] login failed {inbox}: {exc}", flush=True)
            if _is_overquota(exc):
                overquota.add(inbox.lower())
            elif _is_conn_cap(exc):
                _flag_inbox_cooldown(inbox, str(exc))
            return None

    try:
        attempt = 0
        while attempt < max_rounds:
            attempt += 1
            any_usable = False
            print(
                f"[IMAP] Attempt {attempt}/{max_rounds} for {target}",
                flush=True,
            )
            for inbox, password in inboxes:
                if _inbox_on_cooldown(inbox) or inbox.lower() in overquota:
                    continue
                any_usable = True
                mail = None
                try:
                    mail = _ensure(inbox, password)
                    if mail is None:
                        continue
                    status, select_data = _select_inbox(mail, inbox)
                    if status != "OK":
                        if _is_overquota(select_data):
                            overquota.add(inbox.lower())
                            _kill_mail(mail)
                            private_conns.pop(inbox, None)
                            continue
                        print(f"[IMAP] SELECT not OK on {inbox}: {select_data}", flush=True)
                        continue

                    overquota.discard(inbox.lower())
                    link = _search_login_link_in_mail(
                        mail,
                        inbox=inbox,
                        target=target,
                        filter_time=filter_time,
                        date_since=date_since,
                    )
                    if link:
                        cleaned = _clean_url(link)
                        code_m = re.search(
                            r"[?&]code=([^&]+)", cleaned, re.IGNORECASE
                        )
                        code = unquote(code_m.group(1)) if code_m else ""
                        if cleaned in skip_links or (code and code in skip_codes):
                            print(
                                f"[IMAP] skip stale login link for {target} "
                                f"(already tried)",
                                flush=True,
                            )
                            continue
                        print(
                            f"[IMAP] Found login link for {target} via {inbox}",
                            flush=True,
                        )
                        return cleaned
                except TimeoutError as exc:
                    print(f"[IMAP] stop {inbox}: {exc}", flush=True)
                    private_conns.pop(inbox, None)
                    continue
                except Exception as exc:  # noqa: BLE001
                    _kill_mail(private_conns.pop(inbox, None) or mail)
                    if _is_overquota(exc):
                        overquota.add(inbox.lower())
                        print(f"[IMAP] OVERQUOTA on {inbox}", flush=True)
                    elif _is_conn_cap(exc):
                        _flag_inbox_cooldown(inbox, str(exc))
                    elif _is_dead_socket(exc):
                        print(f"[IMAP] stop {inbox}: {exc}", flush=True)
                    else:
                        print(f"[IMAP] Error ({inbox}): {exc}", flush=True)

            if not any_usable:
                if overquota and len(overquota) >= len(inboxes):
                    _sleep_overquota()
                    overquota.clear()
                    attempt -= 1
                    continue
                # All on cooldown — wait a bit then retry rounds
                time.sleep(sleep_s)
                continue

            time.sleep(sleep_s)
    finally:
        for ib in list(private_conns.keys()):
            _kill_mail(private_conns.pop(ib, None))

    return None
