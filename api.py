"""HTTP API: accept checkout emails → CheckoutEmail / CheckoutEmailCA."""

from __future__ import annotations

import os
import secrets
from functools import wraps
from typing import Any, Callable

from flask import Flask, jsonify, request

from checkout_emails import is_allowed_email, save_checkout_email
from market import get_market

app = Flask(__name__)

# Set via env; required for /email. /health stays open.
API_KEY = (
    os.environ.get("HELLOFRESH_API_KEY")
    or os.environ.get("API_KEY")
    or ""
).strip()


def _extract_api_key() -> str:
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (
        request.headers.get("X-API-Key")
        or request.headers.get("x-api-key")
        or request.headers.get("api_key")
        or request.headers.get("API_KEY")
        or request.args.get("api_key")
        or ""
    ).strip()


def require_api_key(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        expected = API_KEY
        if not expected:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "server_misconfigured",
                        "message": "HELLOFRESH_API_KEY is not set",
                    }
                ),
                503,
            )
        provided = _extract_api_key()
        if not provided or not secrets.compare_digest(provided, expected):
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "unauthorized",
                        "message": "Invalid or missing API key",
                    }
                ),
                401,
            )
        return view(*args, **kwargs)

    return wrapped


def _extract_email(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("email", "Email", "mail", "address"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    raw = (request.args.get("email") or request.form.get("email") or "").strip()
    return raw


def _normalize_market(raw: Any) -> str:
    """Map US/USA/CA/CAD/Canada → US|CA."""
    text = str(raw or "US").strip().upper()
    if text in ("CA", "CAD", "CANADA", "CAN"):
        return "CA"
    if text in ("US", "USA", "UNITED STATES", "UNITEDSTATES"):
        return "US"
    raise ValueError(f"Unknown market {raw!r}; expected US or CA")


def _extract_market(payload: Any, *, default: str = "US") -> str:
    if isinstance(payload, dict):
        for key in ("market", "Market", "country", "Country", "region", "locale"):
            val = payload.get(key)
            if val is not None and str(val).strip():
                return _normalize_market(val)
    for key in ("market", "country"):
        val = request.args.get(key) or request.form.get(key)
        if val and str(val).strip():
            return _normalize_market(val)
    return _normalize_market(default)


def _read_payload() -> Any:
    payload: Any = None
    if request.is_json:
        payload = request.get_json(silent=True)
    elif request.data:
        try:
            payload = request.get_json(force=True, silent=True)
        except Exception:  # noqa: BLE001
            payload = None
        if payload is None:
            payload = request.data.decode("utf-8", errors="replace")
    return payload


def _accept_email_for_market(*, default_market: str = "US") -> Any:
    """Accept an email for US (CheckoutEmail) or CA (CheckoutEmailCA)."""
    payload = _read_payload()
    email = _extract_email(payload)
    try:
        market = _extract_market(payload, default=default_market)
        mkt = get_market(market)
    except ValueError as exc:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "bad_market",
                    "message": str(exc),
                }
            ),
            400,
        )

    saved = False
    exists = False
    collection = mkt["collections"]["checkout"]
    if email and is_allowed_email(email):
        result = save_checkout_email(email, market=mkt["code"])
        saved = bool(result.get("inserted"))
        exists = bool(result.get("exists"))
        collection = result.get("collection") or collection
        if saved:
            print(
                f"[API] Saved {collection} ({mkt['code']}) {email.lower()}",
                flush=True,
            )
        else:
            print(
                f"[API] {collection} already exists ({mkt['code']}) {email.lower()}",
                flush=True,
            )
    elif email:
        print(f"[API] Ignored non-matching email {email}", flush=True)

    return jsonify(
        {
            "success": True,
            "saved": saved,
            "exists": exists,
            "email": email.lower() if email else None,
            "market": mkt["code"],
            "collection": collection,
        }
    )


@app.get("/health")
def health() -> Any:
    return jsonify({"ok": True, "service": "HelloFreshMonitor"})


@app.post("/email")
@app.post("/api/email")
@require_api_key
def accept_email() -> Any:
    """Accept an email. Default market=US → HelloFresh.CheckoutEmail.

    Pass ``market``/``country`` as ``CA``/``CAD`` (body or query) for Canada,
    or use ``POST /email/ca``.

    Auth: ``X-API-Key`` header (or ``Authorization: Bearer …`` / ``?api_key=``).
    """
    return _accept_email_for_market(default_market="US")


@app.post("/email/ca")
@app.post("/email/cad")
@app.post("/api/email/ca")
@app.post("/api/email/cad")
@require_api_key
def accept_email_ca() -> Any:
    """Accept an email into HelloFresh.CheckoutEmailCA (Canada)."""
    return _accept_email_for_market(default_market="CA")


def run_api(*, host: str | None = None, port: int | None = None) -> None:
    host = host or os.environ.get("API_HOST", "0.0.0.0")
    port = int(port or os.environ.get("PORT") or os.environ.get("API_PORT") or 8080)
    if API_KEY:
        print(f"[API] Auth enabled (key length={len(API_KEY)})", flush=True)
    else:
        print(
            "[API] WARNING: HELLOFRESH_API_KEY unset — /email will return 503",
            flush=True,
        )
    print(f"[API] Listening on {host}:{port}", flush=True)
    app.run(host=host, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    run_api()
