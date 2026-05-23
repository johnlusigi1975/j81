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
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
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


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(_HOMEPAGE, media_type="text/html")


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


@app.get("/oauth/callback", include_in_schema=False)
async def oauth_callback(request: Request) -> dict:
    """Dual routing (like Twinmil's getInitialView):
      * ?error=…                 → surface it
      * ?token1=…&acct1=…&cur1=… → LEGACY (tokens straight in the URL)
      * ?code=…&state=…          → NEW OAuth2 PKCE (exchange code for a token)
    """
    params = dict(request.query_params)
    store = get_store()

    if params.get("error"):
        raise HTTPException(
            400, f"Deriv returned an error: {params.get('error_description') or params['error']}")

    # ---- LEGACY: token1/acct1/cur1 triples ----
    if "token1" in params:
        saved: list[dict] = []
        i = 1
        while f"token{i}" in params and f"acct{i}" in params:
            try:
                internal_id = store.upsert_account(
                    deriv_account_id=params[f"acct{i}"],
                    token=params[f"token{i}"],
                    currency=params.get(f"cur{i}"),
                )
            except RuntimeError as exc:
                raise HTTPException(500, f"cannot store token: {exc}")
            saved.append({"internal_id": internal_id,
                          "deriv_account_id": params[f"acct{i}"],
                          "currency": params.get(f"cur{i}"), "enabled": False})
            i += 1
        if not saved:
            raise HTTPException(400, "no tokens in callback — auth may have been cancelled")
        return {"saved": len(saved), "flow": "legacy", "accounts": saved,
                "next": "Visit / to enable autotrading per-account."}

    # ---- NEW: OAuth2 PKCE code exchange ----
    if "code" in params:
        s = get_settings()
        state = params.get("state")
        entry = _PKCE_STATES.pop(state, None) if state else None
        if not entry:
            raise HTTPException(400, "OAuth state missing or expired — start the connection again.")
        if not s.deriv_oauth_token_url:
            raise HTTPException(500, "DERIV_OAUTH_TOKEN_URL is not set — cannot exchange the code.")
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
            raise HTTPException(502, f"token exchange failed: {exc!r}")
        access = tok.get("access_token")
        if not access:
            raise HTTPException(502, "no access_token in Deriv's token response")
        try:
            info = await authorize_account(access)
        except Exception as exc:
            raise HTTPException(400, f"authorized but could not read the account: {exc}")
        loginid = info.get("loginid")
        if not loginid:
            raise HTTPException(400, "token exchanged, but Deriv returned no account id")
        internal_id = store.upsert_account(
            deriv_account_id=loginid, token=access, currency=info.get("currency"),
            platform="new")
        return {"saved": 1, "flow": "oauth2_pkce",
                "accounts": [{"internal_id": internal_id, "deriv_account_id": loginid,
                              "currency": info.get("currency"), "enabled": False}],
                "next": "Visit / to enable autotrading on this account."}

    raise HTTPException(400, "no tokens or code in callback — auth flow may have been cancelled")


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


def _ensure_enc_key() -> None:
    """Generate + persist a Fernet key if none is set (quiet, non-erroring)."""
    if get_settings().bot_encryption_key:
        return
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    existing = _BOT_ENV_FILE.read_text() if _BOT_ENV_FILE.exists() else ""
    if "BOT_ENCRYPTION_KEY" not in existing:
        _BOT_ENV_FILE.write_text((existing.rstrip() + "\n" if existing else "") + f"BOT_ENCRYPTION_KEY={key}\n")
        try:
            _os.chmod(_BOT_ENV_FILE, 0o600)
        except OSError:
            pass
    get_settings.cache_clear()


@app.post("/preview/enter")
def preview_enter() -> dict:
    """TESTING ONLY: create/return a local DEMO account so you can walk the full
    client interface without a real Deriv connection. DRY_RUN keeps every trade
    pretend; the token is a dummy and never touches Deriv. Remove once the app
    is registered on Deriv."""
    _ensure_enc_key()
    store = get_store()
    demo_id = "VRTC0000000"  # VRT prefix → treated as a demo account
    internal = store.upsert_account(deriv_account_id=demo_id, token="preview-demo", currency="USD")
    store.update_account_settings(internal, enabled=True)
    acct = next((a for a in store.list_accounts_public() if a["id"] == internal), None)
    return {"account": acct, "preview": True}


class ConnectPATRequest(BaseModel):
    token: str


@app.post("/connect/pat")
async def connect_pat(body: ConnectPATRequest) -> dict:
    """Connect a Deriv account by pasting a Personal Access Token (Read+Trade).
    Simpler than OAuth for local/desktop use — no app registration or redirect
    URLs. The token is authorized, then stored ENCRYPTED; it never returns in
    any response and never goes through chat. Also tells us if the account is
    on the legacy API (if this succeeds, it is)."""
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
        deriv_account_id=loginid, token=token, currency=info.get("currency"))
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
def accounts_list() -> list[dict]:
    return get_store().list_accounts_public()


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


@app.patch("/accounts/{account_id}")
def account_patch(account_id: str, body: AccountPatch) -> dict:
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
    ):
        raise HTTPException(404, "account not found or no changes")
    return {"updated": account_id}


@app.delete("/accounts/{account_id}")
def account_delete(account_id: str) -> dict:
    if not get_store().delete_account(account_id):
        raise HTTPException(404, "account not found")
    return {"deleted": account_id}


@app.post("/accounts/{account_id}/trade")
async def account_trade_now(account_id: str, symbol: str = "R_100") -> dict:
    """One-shot: ask analyser for a decision and execute through the
    risk gates. Useful for testing without waiting for the autonomous loop."""
    acct = get_store().get_internal(account_id)
    if acct is None:
        raise HTTPException(404, "account not found")
    decision = await get_decision(symbol)
    if not decision:
        return {"outcome": "skipped", "reason": "analyser unreachable"}
    return await execute_decision_for_account(acct, decision)


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
    return {
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


# ---------------------------------------------------------------------------
# Simple manual trading — the grandma "Trade" panel (Rise/Fall + Even/Odd)
# ---------------------------------------------------------------------------

# A short, friendly list of common synthetic indices (always open, 24/7).
TRADEABLE_SYMBOLS = [
    {"code": "R_10", "name": "Volatility 10"},
    {"code": "R_25", "name": "Volatility 25"},
    {"code": "R_50", "name": "Volatility 50"},
    {"code": "R_75", "name": "Volatility 75"},
    {"code": "R_100", "name": "Volatility 100"},
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
async def trade_manual(req: ManualTradeRequest) -> dict:
    """Place a trade the user picked themselves (Rise/Fall or Even/Odd).
    Honours DRY_RUN + the account's stake/daily caps."""
    acct = get_store().get_internal(req.account_id)
    if acct is None:
        raise HTTPException(404, "account not found")
    return await execute_manual_trade(
        acct, trade_type=req.trade_type, symbol=req.symbol,
        direction=req.direction, prediction=req.prediction,
        stake=req.stake, duration=req.duration, duration_unit=req.duration_unit,
    )


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


@app.get("/trades")
def trades_list(account_id: str | None = None, limit: int = 100) -> list[dict]:
    return get_store().list_trades(account_id=account_id, limit=limit)


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
