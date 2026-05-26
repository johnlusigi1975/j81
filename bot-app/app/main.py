"""J81 Bot — execution layer (system 3 of 3).

  GET  /                     homepage
  GET  /health               status incl. DRY_RUN flag
  GET  /oauth/start          redirects user to Deriv to authorize
  GET  /oauth/callback       Deriv calls this with the user's account tokens
  GET  /accounts             list connected accounts (no tokens leaked)
  PATCH /accounts/{id}       enable/disable + edit risk limits
  DELETE /accounts/{id}      forget this user's account
  POST /accounts/{id}/trade  manually trigger one decision/trade cycle
  GET  /trades               recent trade log (dry-run + live)
  GET  /stats                accounts, trades, projected markup
  GET  /loop/status          autonomous loop status
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote as urlquote, urlencode

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from app.config import APP_NAME, APP_VERSION, get_settings
from app.deriv import (
    DerivBotError,
    authorize_account,
    estimate_markup,
    get_proposal,
    is_demo_account,
)
from app.executor import (
    MANUAL_TRADE_TYPES,
    execute_decision_for_account,
    execute_manual_trade,
    get_decision,
    settle_pending_for_account,
)
from app.store import get_store
from app.trading_loop import loop as trading_loop


@asynccontextmanager
async def lifespan(_: FastAPI):
    trading_loop.ensure_running()
    from app.mpro import engine as mpro_engine
    mpro_engine.ensure_running()
    yield


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)
_HOMEPAGE = Path(__file__).parent / "web" / "index.html"

# ---- per-visitor session: each browser sees only the account(s) IT connected,
# so a client opening the link gets their own Connect→Deriv flow instead of
# landing on whoever connected first. ----
SESSION_COOKIE = "j81_sid"
_SESSION_MAX_AGE = 60 * 60 * 24 * 180  # 180 days


def _new_sid() -> str:
    return _secrets.token_urlsafe(24)


def _set_session_cookie(resp: Response, sid: str) -> None:
    resp.set_cookie(SESSION_COOKIE, sid, max_age=_SESSION_MAX_AGE,
                    httponly=True, samesite="lax", secure=True, path="/")


def _no_cache(resp):
    """Tell browsers to ALWAYS revalidate the HTML with the server (304 when
    unchanged, full content when changed). Without this, every device caches
    the SPA and refreshes can show a stale version of the app — exactly the
    "I updated but it didn't change on my phone" symptom."""
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/", include_in_schema=False)
def home(request: Request) -> FileResponse:
    resp = FileResponse(_HOMEPAGE, media_type="text/html")
    if not request.cookies.get(SESSION_COOKIE):
        _set_session_cookie(resp, _new_sid())  # give every visitor a session up front
    return _no_cache(resp)


@app.get("/owner", include_in_schema=False)
def owner_page() -> FileResponse:
    """Owner console to mint/copy membership codes (guarded by ADMIN_KEY on the
    API calls it makes; the page itself holds no secret)."""
    return _no_cache(FileResponse(Path(__file__).parent / "web" / "owner.html", media_type="text/html"))


# ---------------------------------------------------------------------------
# Paid access (membership paywall) — honest model: pay for the TOOLS.
# ---------------------------------------------------------------------------


@app.get("/access/status")
def access_status(request: Request, response: Response) -> dict:
    """Is the caller a paid member? Also returns the offer (price + buy link) so
    the paywall can render. require_access=false means the app is open to all."""
    s = get_settings()
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        sid = _new_sid(); _set_session_cookie(response, sid)
    st = get_store().access_status(sid)
    return {**st, "require_access": s.require_access,
            "price_label": s.access_price_label, "buy_url": s.access_buy_url,
            "days_per_membership": s.access_days}


class RedeemCode(BaseModel):
    code: str


@app.post("/access/redeem")
def access_redeem(body: RedeemCode, request: Request, response: Response) -> dict:
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        sid = _new_sid(); _set_session_cookie(response, sid)
    res = get_store().redeem_license(body.code, sid)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "could not redeem code"))
    return {**res, **get_store().access_status(sid)}


def _require_admin(request: Request) -> None:
    """Owner-only guard for license management. Disabled until ADMIN_KEY is set."""
    expected = get_settings().admin_key
    if not expected:
        raise HTTPException(403, "admin disabled — set ADMIN_KEY in the dashboard")
    got = request.headers.get("X-Admin-Key") or request.query_params.get("key", "")
    if got != expected:
        raise HTTPException(403, "bad admin key")


class MintLicenses(BaseModel):
    count: int = 1
    note: str | None = None


@app.post("/admin/licenses")
def admin_mint(body: MintLicenses, request: Request) -> dict:
    """Owner: mint membership codes (one per paying customer). Honest model —
    these unlock the TOOLS, not guaranteed wins."""
    _require_admin(request)
    days = get_settings().access_days
    codes = get_store().create_licenses(max(1, min(body.count, 1000)), days, body.note)
    return {"created": len(codes), "days": days, "codes": codes}


@app.get("/admin/licenses")
def admin_list(request: Request) -> list[dict]:
    _require_admin(request)
    return get_store().list_licenses()


def _verify_stripe_sig(payload: bytes, sig_header: str, secret: str, tolerance: int = 300) -> bool:
    """Verify a Stripe webhook signature with stdlib only (no `stripe` dep).
    Header form: 't=<ts>,v1=<hmac>,v1=<hmac>'. Valid if any v1 matches."""
    import hmac, hashlib, time
    if not sig_header or not secret:
        return False
    try:
        ts = None
        sigs = []
        for part in sig_header.split(","):
            k, _, v = part.partition("=")
            if k.strip() == "t":
                ts = v.strip()
            elif k.strip() == "v1":
                sigs.append(v.strip())
        if not ts or not sigs:
            return False
        if tolerance and abs(time.time() - int(ts)) > tolerance:
            return False
        expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload,
                            hashlib.sha256).hexdigest()
        return any(hmac.compare_digest(expected, s) for s in sigs)
    except Exception:
        return False


@app.post("/webhooks/stripe", include_in_schema=False)
async def stripe_webhook(request: Request) -> dict:
    """Stripe calls this when a payment completes. We verify the signature, then
    mint ONE membership code tied to the checkout session (idempotent), which the
    buyer's success page picks up via /access/code. Needs STRIPE_WEBHOOK_SECRET."""
    secret = get_settings().stripe_webhook_secret
    if not secret:
        raise HTTPException(503, "stripe webhook not configured")
    payload = await request.body()
    if not _verify_stripe_sig(payload, request.headers.get("Stripe-Signature", ""), secret):
        raise HTTPException(400, "bad signature")
    import json as _json
    try:
        event = _json.loads(payload)
    except Exception:
        raise HTTPException(400, "bad payload")
    if event.get("type") in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        obj = (event.get("data") or {}).get("object") or {}
        sid = obj.get("id")
        paid = obj.get("payment_status") in ("paid", "no_payment_required") or obj.get("status") == "complete"
        if sid and paid:
            code = get_store().mint_for_ref(f"stripe:{sid}", get_settings().access_days)
            return {"ok": True, "code_issued": True, "code": code}
    return {"ok": True, "code_issued": False}


@app.get("/access/code")
def access_code(session_id: str) -> dict:
    """The buyer's success page calls this with the Stripe checkout session id to
    fetch the code minted for their payment. (The session id is the buyer's own
    one-time token, so it's the proof of purchase.)"""
    lic = get_store().license_by_note(f"stripe:{session_id}")
    if not lic:
        return {"ready": False}
    return {"ready": True, "code": lic["code"]}


@app.get("/robots.txt", include_in_schema=False)
def robots() -> Response:
    """Let search engines crawl the public app; keep API/OAuth paths out of the
    index (they're not pages)."""
    body = ("User-agent: *\n"
            "Allow: /$\n"
            "Disallow: /oauth/\n"
            "Disallow: /accounts\n"
            "Disallow: /trade/\n"
            "Sitemap: https://j81-trade-desk.onrender.com/sitemap.xml\n")
    return Response(body, media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap() -> Response:
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            '  <url><loc>https://j81-trade-desk.onrender.com/</loc>'
            '<changefreq>weekly</changefreq><priority>1.0</priority></url>\n'
            '</urlset>\n')
    return Response(body, media_type="application/xml")


@app.get("/og-image.svg", include_in_schema=False)
def og_image() -> Response:
    """Branded 1200×630 link-preview card (SVG — self-contained, no binary asset).
    Rendered by Slack/Discord/LinkedIn; X/Facebook may fall back to text."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">'
        '<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#090d16"/><stop offset="1" stop-color="#0d1424"/></linearGradient>'
        '<linearGradient id="gold" x1="0" y1="0" x2="1" y2="0">'
        '<stop offset="0" stop-color="#ffeaa6"/><stop offset="1" stop-color="#d39f2c"/></linearGradient></defs>'
        '<rect width="1200" height="630" fill="url(#bg)"/>'
        '<rect x="64" y="64" width="120" height="120" rx="28" fill="#080709"/>'
        '<text x="124" y="148" font-family="Georgia,serif" font-size="56" font-weight="800" '
        'text-anchor="middle" fill="#f0c64a">J81</text>'
        '<text x="220" y="150" font-family="Georgia,serif" font-size="64" font-weight="800" '
        'fill="url(#gold)">Trade Desk</text>'
        '<text x="66" y="330" font-family="Helvetica,Arial,sans-serif" font-size="52" font-weight="700" '
        'fill="#eef3fb">AI-assisted trading on Deriv</text>'
        '<text x="66" y="398" font-family="Helvetica,Arial,sans-serif" font-size="30" '
        'fill="#9fb6c5">Live charts · one-tap connect · auto-trading · self-testing strategy engine</text>'
        '<rect x="66" y="470" width="320" height="64" rx="14" fill="url(#gold)"/>'
        '<text x="226" y="512" font-family="Helvetica,Arial,sans-serif" font-size="28" font-weight="800" '
        'text-anchor="middle" fill="#2a1f05">Connect with Deriv</text>'
        '<text x="66" y="588" font-family="Helvetica,Arial,sans-serif" font-size="22" '
        'fill="#6f7d8c">Trading carries risk · synthetics are an audited RNG with a house edge</text>'
        '</svg>'
    )
    return Response(svg, media_type="image/svg+xml")


@app.get("/health")
def health() -> dict:
    s = get_settings()
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "status": "ok",
        "dry_run": s.dry_run,
        "deriv_app_registered": bool(s.deriv_app_id),
        "encryption_configured": bool(s.bot_encryption_key),
        "markup_percent": s.deriv_markup_percent,
        "analyser_url": s.analyser_url,
        "referral_url": s.deriv_referral_url,
        "oauth_ready": bool(s.deriv_app_id),
    }


@app.get("/health/tree")
async def health_tree() -> dict:
    """Whole-tree check-up: this bot's vitals + a live ping of the analyser and
    researcher. One call to see if every organ is alive. Soft-fails per service."""
    import time as _t
    import httpx
    s = get_settings()
    store = get_store()
    accts = store.list_accounts_public()
    enabled = [a for a in accts if a["enabled"]]
    proven = store.list_proven_strategies(limit=1000)

    async def _ping(url: str) -> dict:
        if not url:
            return {"configured": False}
        t0 = _t.monotonic()
        try:
            async with httpx.AsyncClient(timeout=6.0) as c:
                r = await c.get(url.rstrip("/") + "/health")
                return {"configured": True, "ok": r.status_code < 400,
                        "status": r.status_code, "ms": round((_t.monotonic() - t0) * 1000)}
        except Exception as exc:
            return {"configured": True, "ok": False, "error": str(exc)[:80]}

    analyser = await _ping(s.analyser_url)
    cycle = {}
    if analyser.get("ok"):
        try:
            async with httpx.AsyncClient(timeout=6.0) as c:
                cycle = (await c.get(s.analyser_url.rstrip("/") + "/cycle/status")).json()
        except Exception:
            cycle = {}
    return {
        "bot": {"ok": True, "version": APP_VERSION, "dry_run": s.dry_run,
                "loop_alive": trading_loop.status.get("loop_alive"),
                "cycles": trading_loop.status.get("cycles"),
                "last_error": trading_loop.status.get("last_error"),
                "accounts": len(accts), "enabled": len(enabled),
                "proven_strategies": len(proven)},
        "analyser": analyser,
        "researcher": await _ping(_researcher_url()),
        "cycle": {"tested": cycle.get("tested"), "proven": cycle.get("proven_count"),
                  "next_in_seconds": cycle.get("next_in_seconds")},
    }


def _researcher_url() -> str:
    """Researcher URL is configured on the analyser, not the bot — best-effort
    derive it from the analyser host so the tree check can ping it too."""
    s = get_settings()
    base = (s.analyser_url or "").rstrip("/")
    return base.replace("analyser", "researcher") if "analyser" in base else ""


# ---------------------------------------------------------------------------
# OAuth flow — users authorize our app to trade on their Deriv account
# ---------------------------------------------------------------------------


# PKCE state store: state -> (code_verifier, created_at). Server-side because
# J81's OAuth is server-rendered (Twinmil does the equivalent client-side).
import base64 as _b64
import hashlib as _hashlib
import secrets as _secrets
import time as _time

_PKCE_STATES: dict[str, tuple[str, float]] = {}


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for OAuth2 PKCE (S256)."""
    verifier = _secrets.token_urlsafe(64)[:96]
    challenge = _b64.urlsafe_b64encode(
        _hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


def _prune_pkce(ttl: float = 600.0) -> None:
    now = _time.time()
    for k in [k for k, (_, t) in _PKCE_STATES.items() if now - t > ttl]:
        _PKCE_STATES.pop(k, None)


@app.get("/oauth/start", include_in_schema=False)
def oauth_start() -> RedirectResponse:
    """Bounce the user to Deriv's authorize page. Uses OAuth2 + PKCE (the new
    platform) when DERIV_OAUTH_TOKEN_URL is configured, otherwise the legacy
    flow. Either way Deriv redirects back to /oauth/callback, which handles both."""
    s = get_settings()
    if not s.deriv_app_id:
        raise HTTPException(
            500,
            "DERIV_APP_ID is not set — register an app at api.deriv.com/dashboard first",
        )
    if s.deriv_oauth_token_url:  # NEW platform: OAuth2 + PKCE
        verifier, challenge = _pkce_pair()
        state = _secrets.token_urlsafe(24)
        _PKCE_STATES[state] = (verifier, _time.time())
        _prune_pkce()
        # NOTE: this Deriv app is NOT allowed to request `offline_access` (Ory
        # rejects it: "client is not allowed to request scope 'offline_access'"),
        # so we send ONLY the configured scope. Without a refresh token the
        # access token expires (~1h) and the user reconnects via the graceful
        # "Reconnect Deriv ↻" prompt. The refresh machinery stays dormant in case
        # the app's capabilities are later enabled in the Deriv dashboard.
        q = urlencode({
            "response_type": "code",
            "client_id": s.deriv_app_id,
            "redirect_uri": s.deriv_oauth_redirect_uri,
            "scope": s.deriv_oauth_scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        return RedirectResponse(f"{s.deriv_oauth_authorize_url}?{q}")
    # LEGACY platform: app_id only; Deriv returns token1/acct1/cur1.
    q = urlencode({"app_id": s.deriv_app_id})
    return RedirectResponse(f"{s.deriv_oauth_authorize_url}?{q}")


def _auth_ok(n: int) -> RedirectResponse:
    """Bounce the freshly-connected user back into the app UI (loading → home)."""
    return RedirectResponse(f"/?connected={n}", status_code=303)


def _auth_fail(msg: str) -> RedirectResponse:
    """Bounce to the connect screen with a friendly message instead of a raw error."""
    return RedirectResponse(f"/?auth_error={urlquote(msg)}", status_code=303)


@app.get("/oauth/callback", include_in_schema=False)
async def oauth_callback(request: Request) -> RedirectResponse:
    """After Deriv authorizes the user, save their account(s) and redirect them
    back into the app (loading → home). Any failure redirects to the connect
    screen with a readable message rather than dumping JSON or an error page.

    Routing (like Twinmil's getInitialView):
      * ?error=…                 → surface it on the connect screen
      * ?token1=…&acct1=…&cur1=… → LEGACY (tokens straight in the URL)
      * ?code=…&state=…          → NEW OAuth2 PKCE (exchange code for a token)
    """
    params = dict(request.query_params)
    store = get_store()
    # Bind the connected account(s) to THIS browser session.
    sid = request.cookies.get(SESSION_COOKIE) or _new_sid()

    def _ok(n: int) -> RedirectResponse:
        resp = _auth_ok(n); _set_session_cookie(resp, sid); return resp

    if params.get("error"):
        return _auth_fail(params.get("error_description") or params["error"])

    # ---- LEGACY: token1/acct1/cur1 triples ----
    if "token1" in params:
        saved = 0
        i = 1
        while f"token{i}" in params and f"acct{i}" in params:
            try:
                store.upsert_account(
                    deriv_account_id=params[f"acct{i}"],
                    token=params[f"token{i}"],
                    currency=params.get(f"cur{i}"),
                    session_id=sid,
                )
                saved += 1
            except RuntimeError as exc:
                return _auth_fail(f"Could not store your token: {exc}")
            i += 1
        if not saved:
            return _auth_fail("No account came back from Deriv — the sign-in may have been cancelled.")
        return _ok(saved)

    # ---- NEW: OAuth2 PKCE code exchange ----
    if "code" in params:
        s = get_settings()
        state = params.get("state")
        entry = _PKCE_STATES.pop(state, None) if state else None
        if not entry:
            return _auth_fail("Your sign-in session expired — please connect again.")
        if not s.deriv_oauth_token_url:
            return _auth_fail("Server is missing DERIV_OAUTH_TOKEN_URL — cannot finish sign-in.")
        verifier = entry[0]
        import httpx
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.post(s.deriv_oauth_token_url, data={
                    "grant_type": "authorization_code",
                    "code": params["code"],
                    "redirect_uri": s.deriv_oauth_redirect_uri,
                    "client_id": s.deriv_app_id,
                    "code_verifier": verifier,
                })
                r.raise_for_status()
                tok = r.json()
        except Exception as exc:
            return _auth_fail(f"Could not finish sign-in with Deriv: {exc}")
        access = tok.get("access_token")
        if not access:
            return _auth_fail("Deriv did not return an access token — please try again.")
        refresh = tok.get("refresh_token")          # present iff offline_access granted
        expires_in = tok.get("expires_in")
        # New-platform token: read the user's accounts (demo + real) from the
        # new Options API, NOT the legacy v3 authorize (which rejects this token).
        from app import deriv_new
        try:
            accounts = await deriv_new.list_accounts(access)
        except Exception as exc:
            return _auth_fail(f"Signed in, but could not read your accounts: {exc}")
        saved = 0
        for a in accounts:
            try:
                store.upsert_account(
                    deriv_account_id=a["loginid"], token=access,
                    currency=a.get("currency"), platform="new", session_id=sid,
                    refresh_token=refresh, expires_in=expires_in)
                saved += 1
            except RuntimeError as exc:
                return _auth_fail(f"Could not store your account: {exc}")
        if not saved:
            return _auth_fail("Signed in, but found no tradable accounts.")
        return _ok(saved)

    return _auth_fail("No account came back from Deriv — the sign-in may have been cancelled.")


# ---------------------------------------------------------------------------
# Account management
# ---------------------------------------------------------------------------


import os as _os
from pathlib import Path as _Path

_BOT_ENV_FILE = _Path(__file__).parent.parent / ".env"


@app.post("/setup/encryption-key")
def setup_encryption_key() -> dict:
    """One-click: generate a Fernet key and save it to bot-app/.env so tokens
    can be stored encrypted. Refuses if one already exists (changing it would
    make existing stored tokens unreadable — that must be a deliberate edit)."""
    if get_settings().bot_encryption_key:
        raise HTTPException(409, "An encryption key is already configured.")
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    _BOT_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = _BOT_ENV_FILE.read_text() if _BOT_ENV_FILE.exists() else ""
    if "BOT_ENCRYPTION_KEY" in existing:
        raise HTTPException(409, "BOT_ENCRYPTION_KEY already present in .env")
    _BOT_ENV_FILE.write_text((existing.rstrip() + "\n" if existing else "") + f"BOT_ENCRYPTION_KEY={key}\n")
    try:
        _os.chmod(_BOT_ENV_FILE, 0o600)
    except OSError:
        pass
    get_settings.cache_clear()
    return {"ok": True, "note": "Encryption key generated and saved locally — you can now connect accounts."}


class ConnectPATRequest(BaseModel):
    token: str


@app.post("/connect/pat")
async def connect_pat(body: ConnectPATRequest, request: Request, response: Response) -> dict:
    """Connect a Deriv account by pasting a Personal Access Token (Read+Trade).
    Simpler than OAuth for local/desktop use — no app registration or redirect
    URLs. The token is authorized, then stored ENCRYPTED; it never returns in
    any response and never goes through chat. Also tells us if the account is
    on the legacy API (if this succeeds, it is)."""
    sid = request.cookies.get(SESSION_COOKIE) or _new_sid()
    _set_session_cookie(response, sid)
    token = body.token.strip()
    if not token:
        raise HTTPException(400, "paste your Deriv API token first")
    if not get_settings().bot_encryption_key:
        raise HTTPException(
            500, "BOT_ENCRYPTION_KEY is not set — generate one and put it in "
            "bot-app/.env so tokens can be stored encrypted.")
    try:
        info = await authorize_account(token)
    except DerivBotError as exc:
        raise HTTPException(
            400, f"Deriv rejected the token: {exc}. Make sure it has the "
            "'Read' and 'Trade' scopes — or your account may be on the new "
            "platform (then we build the new auth).")
    except Exception as exc:
        raise HTTPException(502, f"couldn't reach Deriv: {exc!r}")

    loginid = info.get("loginid")
    if not loginid:
        raise HTTPException(400, "authorized, but Deriv returned no account id")
    internal_id = get_store().upsert_account(
        deriv_account_id=loginid, token=token, currency=info.get("currency"),
        session_id=sid)
    return {
        "connected": loginid,
        "is_demo": is_demo_account(loginid),
        "kind": "demo" if is_demo_account(loginid) else "REAL MONEY",
        "currency": info.get("currency"),
        "internal_id": internal_id,
        "platform": "legacy",  # authorize succeeded → legacy API works
        "all_accounts": [
            {"loginid": a.get("loginid"),
             "is_demo": is_demo_account(a.get("loginid") or ""),
             "currency": a.get("currency")}
            for a in info.get("account_list", [])
        ],
        "next": "Enable autotrade on this account below, or use the Trade panel. "
                "Keep DRY_RUN on until you've tested on the demo (VRTC) account.",
    }


@app.get("/accounts")
def accounts_list(request: Request) -> list[dict]:
    # Only this browser session's accounts (privacy). No cookie → nothing yet.
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return []
    return get_store().list_accounts_public(session_id=sid)


_BALANCE_CACHE: dict[str, tuple[float, dict]] = {}
_BALANCE_TTL = 8.0  # collapse rapid polls into one real Deriv round-trip


@app.get("/accounts/{account_id}/balance")
async def account_balance(account_id: str, request: Request) -> dict:
    """Live Deriv balance for one of the caller's accounts. New-platform
    accounts read it over the OTP socket; legacy via authorize.

    Never 502s: balance is a best-effort display value, so on a Deriv hiccup or
    an EXPIRED TOKEN it returns 200 with balance=null (+ needs_reconnect) so the
    UI shows "—" / a reconnect hint instead of spamming errors. Reads are cached
    briefly and stale cache is served through transient failures."""
    import time
    _require_own(request, account_id)
    store = get_store()
    acct = store.get_internal(account_id)
    if acct is None:
        raise HTTPException(404, "account not found")
    now = time.monotonic()
    hit = _BALANCE_CACHE.get(account_id)
    if hit and (now - hit[0]) < _BALANCE_TTL:
        return hit[1]
    deriv_id = acct["deriv_account_id"]
    cur_fallback = acct.get("currency") or "USD"
    from app import tokens
    token = await tokens.get_access_token(account_id)
    if not token:
        return {"balance": None, "currency": cur_fallback, "deriv_account_id": deriv_id,
                "error": "no stored token", "needs_reconnect": True}
    platform = (acct.get("platform") or "legacy").lower()
    try:
        if platform == "new":
            from app import deriv_new
            async def _read(tk):
                ws_url = await deriv_new.request_otp_ws(tk, deriv_id)
                return await deriv_new.balance(ws_url)
            b = await tokens.with_fresh_token(account_id, _read)
            bal, cur = b.get("balance"), b.get("currency")
        else:
            info = await authorize_account(token)
            bal, cur = info.get("balance"), info.get("currency")
    except Exception as exc:
        if hit:  # serve last good value through a transient blip
            return hit[1]
        msg = str(exc).lower()
        needs = any(k in msg for k in ("auth", "token", "otp", "401", "unauthor",
                                       "invalid", "expire", "403"))
        return {"balance": None, "currency": cur_fallback, "deriv_account_id": deriv_id,
                "error": str(exc)[:140], "needs_reconnect": needs}
    result = {"balance": bal, "currency": cur or cur_fallback, "deriv_account_id": deriv_id}
    _BALANCE_CACHE[account_id] = (now, result)
    return result


@app.post("/logout")
def logout(response: Response) -> dict:
    """Disconnect THIS browser: clear its session cookie so it no longer sees
    any account. The stored account is untouched (a fresh sign-in re-binds it)."""
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


class AccountPatch(BaseModel):
    enabled: bool | None = None
    max_stake_per_trade: float | None = None
    max_trades_per_day: int | None = None
    min_confidence: float | None = None
    allowed_trade_types: list[str] | None = None
    allowed_symbols: list[str] | None = None
    label: str | None = None
    take_profit: float | None = None
    daily_loss_limit: float | None = None
    mpro_enabled: bool | None = None
    mpro_config: dict | None = None
    rf_config: dict | None = None
    proven_auto: bool | None = None


def _require_own(request: Request, account_id: str) -> None:
    """403/404 unless this account belongs to the caller's session."""
    sid = request.cookies.get(SESSION_COOKIE)
    if not get_store().account_owned_by(account_id, sid):
        raise HTTPException(404, "account not found")


@app.patch("/accounts/{account_id}")
def account_patch(account_id: str, body: AccountPatch, request: Request) -> dict:
    _require_own(request, account_id)
    if not get_store().update_account_settings(
        account_id,
        enabled=body.enabled,
        max_stake_per_trade=body.max_stake_per_trade,
        max_trades_per_day=body.max_trades_per_day,
        min_confidence=body.min_confidence,
        allowed_trade_types=body.allowed_trade_types,
        allowed_symbols=body.allowed_symbols,
        label=body.label,
        take_profit=body.take_profit,
        daily_loss_limit=body.daily_loss_limit,
        mpro_enabled=body.mpro_enabled,
        mpro_config=body.mpro_config,
        rf_config=body.rf_config,
        proven_auto=body.proven_auto,
    ):
        raise HTTPException(404, "account not found or no changes")
    return {"updated": account_id}


@app.delete("/accounts/{account_id}")
def account_delete(account_id: str, request: Request) -> dict:
    _require_own(request, account_id)
    if not get_store().delete_account(account_id):
        raise HTTPException(404, "account not found")
    return {"deleted": account_id}


@app.post("/accounts/{account_id}/trade")
async def account_trade_now(account_id: str, request: Request, symbol: str = "R_100") -> dict:
    """One-shot: ask analyser for a decision and execute through the
    risk gates. Useful for testing without waiting for the autonomous loop."""
    _require_own(request, account_id)
    acct = get_store().get_internal(account_id)
    if acct is None:
        raise HTTPException(404, "account not found")
    decision = await get_decision(symbol)
    if not decision:
        return {"outcome": "skipped", "reason": "analyser unreachable"}
    return await execute_decision_for_account(acct, decision)


def _structural_winprob(trade_type: str | None, direction: str | None,
                        prediction: int | None) -> float:
    """The TRUE win chance of a Deriv synthetic bet (audited RNG):
      rise_fall / even_odd → 0.5
      over N  → digits N+1..9 = (9-N)/10 ; under N → digits 0..N-1 = N/10
      matches a digit → 0.1 ; differs → 0.9
    Recent streaks don't move these — that's the honest point."""
    tt = (trade_type or "").lower()
    d = (direction or "").lower()
    if tt in ("rise_fall", "even_odd"):
        return 0.5
    if tt == "over_under":
        n = int(prediction) if prediction is not None else 5
        n = max(0, min(9, n))
        return (9 - n) / 10.0 if d == "over" else n / 10.0
    if tt == "matches_differs":
        return 0.9 if d == "differs" else 0.1
    return 0.5


@app.get("/quote")
async def quote(
    symbol: str = "R_100",
    trade_type: str = "rise_fall",
    direction: str | None = "up",
    prediction: int | None = None,
    duration: int = 5,
    duration_unit: str = "t",
    stake: float = 1.0,
) -> dict:
    """Real Deriv price quote — NO account, NO token, NO trade placed.
    Shows what a contract would pay out and the markup you'd earn, so you
    can sanity-check the economics before ever going live."""
    try:
        q = await get_proposal(
            symbol=symbol,
            trade_type=trade_type,
            direction=direction,
            prediction=prediction,
            duration=duration,
            duration_unit=duration_unit,
            stake=stake,
        )
    except DerivBotError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"deriv quote failed: {exc!r}")
    payout = q.get("payout")
    out = {
        "symbol": symbol,
        "trade_type": trade_type,
        "stake": stake,
        "payout": payout,
        "ask_price": q.get("ask_price"),
        "spot": q.get("spot"),
        "your_markup_estimate": estimate_markup(payout) if payout else None,
        "markup_percent": get_settings().deriv_markup_percent,
        "longcode": q.get("longcode"),
    }
    # Honest pre-trade math from the contract's STRUCTURAL win probability
    # (RNG, so this is the real chance — recent streaks don't change it).
    if payout:
        wp = _structural_winprob(trade_type, direction, prediction)
        s = float(stake)
        ev = round(wp * (payout - s) - (1 - wp) * s, 4)
        be = round(s / payout, 4) if payout else 1.0
        out.update({
            "win_prob": round(wp, 4),
            "win_prob_pct": round(wp * 100, 1),
            "expected_value": ev,
            "break_even_pct": round(be * 100, 2),
            "edge_pct": round((wp - be) * 100, 2),
            "verdict": "positive EV" if ev > 0 else "negative EV — house edge",
        })
    return out


# ---------------------------------------------------------------------------
# Simple manual trading — the grandma "Trade" panel (Rise/Fall + Even/Odd)
# ---------------------------------------------------------------------------

# A short, friendly list of common synthetic indices (always open, 24/7).
# Continuous Volatility indices — 24/7, uniform last digit, so they support
# BOTH Rise/Fall and all digit trades. (2s tick = R_*, 1s tick = 1HZ*V.)
TRADEABLE_SYMBOLS = [
    {"code": "R_10", "name": "Volatility 10"},
    {"code": "R_25", "name": "Volatility 25"},
    {"code": "R_50", "name": "Volatility 50"},
    {"code": "R_75", "name": "Volatility 75"},
    {"code": "R_100", "name": "Volatility 100"},
    {"code": "1HZ10V", "name": "Volatility 10 (1s)"},
    {"code": "1HZ25V", "name": "Volatility 25 (1s)"},
    {"code": "1HZ50V", "name": "Volatility 50 (1s)"},
    {"code": "1HZ75V", "name": "Volatility 75 (1s)"},
    {"code": "1HZ100V", "name": "Volatility 100 (1s)"},
]


@app.get("/symbols")
def symbols() -> dict:
    return {"symbols": TRADEABLE_SYMBOLS, "trade_types": list(MANUAL_TRADE_TYPES)}


# ---------------------------------------------------------------------------
# M Pro — Even/Odd confidence engine (10-market scanner + auto-cycle)
# ---------------------------------------------------------------------------

# The strategy menu (Twinmil-style). Only M Pro is live; the rest are planned.
STRATEGIES = [
    {"id": "m_pro", "name": "M Pro", "tag": "Even/Odd confidence engine · 10-market auto", "status": "live"},
    {"id": "m_digit", "name": "M Digit", "tag": "Adaptive single-digit predictor · EV-optimized", "status": "soon"},
    {"id": "sniper_x", "name": "Sniper X", "tag": "Best over/under pairs · 11-market scan", "status": "soon"},
    {"id": "digit_scanner", "name": "Digit Scanner", "tag": "Probability edge · 10-index scan", "status": "soon"},
    {"id": "r1_match", "name": "Deriv R1 Match", "tag": "Top digit-match auto bot", "status": "soon"},
    {"id": "rise_fall", "name": "Rise & Fall", "tag": "Up/down direction", "status": "soon"},
    {"id": "higher_lower", "name": "Higher / Lower", "tag": "Price vs barrier", "status": "soon"},
    {"id": "over_under", "name": "Over / Under", "tag": "Last-digit threshold", "status": "soon"},
    {"id": "antiloss", "name": "AntiLoss", "tag": "Recovery engine", "status": "soon"},
]


@app.get("/strategies")
def strategies() -> dict:
    return {"strategies": STRATEGIES}


@app.get("/scan")
async def scan_proxy() -> dict:
    """Proxy the Analyser's live 10-market Even/Odd scan so the client page
    only ever talks to the Bot."""
    import httpx
    url = get_settings().analyser_url.rstrip("/") + "/scan/even_odd?count=120"
    try:
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.get(url)
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        raise HTTPException(502, f"scanner unavailable (is the Analyser running?): {exc!r}")


@app.get("/even_odd/payouts")
async def even_odd_payouts_proxy(stake: float = 1.0, duration: int = 1) -> dict:
    """Proxy the Analyser's live Even/Odd payout comparison across all markets —
    the one genuine EV lever for Even/Odd is trading the highest-payout market."""
    import httpx
    url = (get_settings().analyser_url.rstrip("/") +
           f"/even_odd/payouts?stake={stake}&duration={duration}")
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.get(url)
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        raise HTTPException(502, f"payout scan unavailable: {exc!r}")


@app.get("/mpro/status")
def mpro_status() -> dict:
    from app.mpro import engine
    return engine.status()


# ---------------------------------------------------------------------------
# Assistant view — the simple, client-facing face of the whole tree. The real
# research + analysis happens behind the scenes (Researcher + Analyser); here
# we just surface it in plain language.
# ---------------------------------------------------------------------------


def _friendly_call(decision: dict) -> dict:
    """Turn the Analyser's decision into a plain-language suggestion, hiding all
    the internal strategy machinery."""
    if not decision or not decision.get("is_trade"):
        return {"suggestion": "wait", "label": "No clear signal — sitting out",
                "confidence": round(float(decision.get("confidence", 0)) * 100) if decision else 0}
    tt = decision.get("trade_type")
    conf = round(float(decision.get("confidence", 0)) * 100)
    if tt == "rise_fall":
        d = (decision.get("direction") or "").lower()
        call = "RISE" if d == "up" else "FALL"
    elif tt == "even_odd":
        call = (decision.get("prediction") or "").upper() or "EVEN"
    else:
        call = (tt or "").upper()
    return {"suggestion": call, "label": f"Leaning {call}", "confidence": conf,
            "trade_type": tt, "direction": decision.get("direction"),
            "prediction": decision.get("prediction")}


@app.get("/assistant/read")
async def assistant_read(symbol: str = "R_100") -> dict:
    """What J81 'sees' on a market right now — a simple read powered by the
    Analyser behind the scenes, plus a live spot price + payout so the trade
    terminal feels live."""
    decision = await get_decision(symbol)
    friendly = _friendly_call(decision)
    # One no-auth proposal gives a real current spot + payout (for the buttons).
    spot = payout = None
    try:
        q = await get_proposal(symbol=symbol, trade_type="rise_fall",
                               direction="up", prediction=None,
                               duration=5, duration_unit="t", stake=1.0)
        spot, payout = q.get("spot"), q.get("payout")
    except Exception:
        pass
    return {
        "symbol": symbol,
        "analysing": True,
        "spot": spot,
        "payout": payout,
        **friendly,
        "message": (
            f"J81 studied {symbol}: {friendly['label'].lower()}"
            + (f" ({friendly['confidence']}% confident)." if friendly["suggestion"] != "wait" else ".")
        ) if decision else f"J81 is warming up its read on {symbol}…",
    }


@app.get("/assistant/summary")
def assistant_summary() -> dict:
    """Today's results in plain numbers for the client — wins, trades, profit,
    and progress toward each account's take-profit goal. No internal jargon."""
    store = get_store()
    accts = store.list_accounts_public()
    trades = store.list_trades(limit=500)
    settled = [t for t in trades if t.get("outcome") in ("won", "lost")]
    wins = sum(1 for t in settled if t.get("outcome") == "won")
    profit_today = sum(a.get("profit_today", 0.0) for a in accts)
    goals = [
        {"account": a["deriv_account_id"], "is_demo": a["is_demo"],
         "profit_today": a.get("profit_today", 0.0),
         "take_profit": a.get("take_profit"), "daily_loss_limit": a.get("daily_loss_limit")}
        for a in accts if a["enabled"]
    ]
    return {
        "accounts_connected": len(accts),
        "trades_total": store.stats().get("trades_total", 0),
        "settled": len(settled),
        "wins": wins,
        "win_rate": round(wins / len(settled), 3) if settled else None,
        "profit_today": round(profit_today, 2),
        "goals": goals,
    }


class ManualTradeRequest(BaseModel):
    account_id: str
    trade_type: str                      # rise_fall | even_odd
    symbol: str = "R_100"
    direction: str | None = None         # rise_fall: up | down
    prediction: str | None = None        # even_odd: even | odd
    stake: float = 1.0
    duration: int = 5
    duration_unit: str = "t"


@app.post("/trade/manual")
async def trade_manual(req: ManualTradeRequest, request: Request) -> dict:
    """Place a trade the user picked themselves (Rise/Fall or Even/Odd).
    Honours DRY_RUN + the account's stake/daily caps."""
    _require_own(request, req.account_id)
    acct = get_store().get_internal(req.account_id)
    if acct is None:
        raise HTTPException(404, "account not found")
    return await execute_manual_trade(
        acct, trade_type=req.trade_type, symbol=req.symbol,
        direction=req.direction, prediction=req.prediction,
        stake=req.stake, duration=req.duration, duration_unit=req.duration_unit,
    )


@app.post("/accounts/{account_id}/settle")
async def account_settle(account_id: str, request: Request) -> dict:
    """Settle this account's just-expired contracts on demand, so the site can
    show the win/loss within seconds instead of waiting for the trading loop."""
    _require_own(request, account_id)
    return await settle_pending_for_account(account_id)


# ---------------------------------------------------------------------------
# Priority mode — read/toggle the hub's tree-wide focus from the Bot page
# ---------------------------------------------------------------------------


@app.get("/priority")
async def priority_get() -> dict:
    from app import comms_client
    return await comms_client.get_priority()


class PriorityToggle(BaseModel):
    enabled: bool


@app.post("/priority")
async def priority_set(body: PriorityToggle) -> dict:
    """Proxy to the hub so the Bot page can flip tree-wide priority too."""
    import httpx
    url = get_settings().analyser_url.rstrip("/") + "/priority"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, json={"enabled": body.enabled})
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        raise HTTPException(502, f"could not reach the hub to set priority: {exc!r}")


@app.get("/cycle/status")
async def cycle_status_proxy() -> dict:
    """Proxy the Analyser's 30-min strategy-cycle status so the Bot dashboard can
    show it without the client needing the analyser URL. Soft-fails if unreachable."""
    import httpx
    url = get_settings().analyser_url.rstrip("/") + "/cycle/status"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.json()
    except Exception:
        return {"reachable": False, "tested": 0, "proven_count": 0,
                "next_in_seconds": None, "note": "analyser unreachable"}


@app.get("/trades")
def trades_list(account_id: str | None = None, limit: int = 100) -> list[dict]:
    return get_store().list_trades(account_id=account_id, limit=limit)


class ProvenStrategies(BaseModel):
    strategies: list[dict]


@app.post("/strategies/proven")
def strategies_proven_save(body: ProvenStrategies) -> dict:
    """The Analyser's 30-min cycle pushes its PROVEN strategies here. They
    persist in the bot even when the analyser/researcher auto-clear."""
    store = get_store()
    saved = [store.save_proven_strategy(s) for s in (body.strategies or [])]
    return {"saved": len(saved), "ids": saved}


@app.get("/strategies/proven")
def strategies_proven_list(limit: int = 200) -> list[dict]:
    return get_store().list_proven_strategies(limit=limit)


@app.get("/stats")
def stats() -> dict:
    return get_store().stats()


@app.get("/loop/status")
def loop_status() -> dict:
    return trading_loop.status


@app.get("/preflight")
def preflight() -> dict:
    """Readiness check for going live — what's set, what's missing, and
    which connected accounts are REAL money vs demo."""
    s = get_settings()
    accts = get_store().list_accounts_public()
    real_enabled = [a["deriv_account_id"] for a in accts if a["enabled"] and not a["is_demo"]]
    demo_enabled = [a["deriv_account_id"] for a in accts if a["enabled"] and a["is_demo"]]
    checks = {
        "deriv_app_id_set": bool(s.deriv_app_id),
        "encryption_key_set": bool(s.bot_encryption_key),
        "analyser_reachable_config": bool(s.analyser_url),
        "accounts_connected": len(accts) > 0,
    }
    ready_for_demo = all([
        checks["deriv_app_id_set"],
        checks["encryption_key_set"],
        checks["accounts_connected"],
    ])
    return {
        "dry_run": s.dry_run,
        "checks": checks,
        "ready_to_trade": ready_for_demo,
        "accounts": {
            "total": len(accts),
            "demo_enabled": demo_enabled,
            "real_money_enabled": real_enabled,
        },
        "warnings": (
            (["DRY_RUN is OFF and you have REAL-MONEY accounts enabled — "
              "live trades will place real contracts."] if (not s.dry_run and real_enabled) else [])
            + (["DRY_RUN is ON — no real or demo contracts will be placed; "
                "everything is logged only."] if s.dry_run else [])
        ),
        "recommendation": (
            "Test on a demo account (VRTC…) with DRY_RUN=false first; "
            "enable a real-money account only once you trust the full loop."
        ),
    }


def main() -> None:
    import uvicorn
    s = get_settings()
    uvicorn.run(app, host=s.host, port=s.port)


if __name__ == "__main__":
    main()
