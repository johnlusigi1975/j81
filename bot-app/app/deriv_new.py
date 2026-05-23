"""New-platform Deriv client — OAuth2 (PKCE) access token → OTP → OTP-WS.

The new Deriv platform does NOT use the legacy `authorize` call. Instead
(observed from a live Twinmil session):

  1. With the OAuth2 access token, request a one-time WS URL for an account:
       GET {base}/trading/v1/options/accounts/{loginid}/otp   (Bearer token)
     → returns  wss://{host}/trading/v1/options/ws/{real|demo}?otp=…
  2. Connect to that URL. The OTP in the URL authenticates the socket — there is
     NO `authorize` step — then you speak the SAME classic message shapes
     (balance, proposal, buy, proposal_open_contract, ticks, active_symbols).

Live trace this is modelled on:
    OTP req → https://api.derivws.com/trading/v1/options/accounts/ROT…/otp
    OTP URL → wss://api.derivws.com/trading/v1/options/ws/real?otp=gZFDVCWz
    {"balance":1,"subscribe":1} → {"balance":0.15,"currency":"USD",…,"loginid":"ROT…"}

NOTE: coded to that spec but NOT live-tested (needs a real OAuth flow on a public
URL + a new-platform account). Endpoints are configurable. The classic payload
builders are reused from app.deriv, so the trading message layer is shared.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import websockets

from app.config import get_settings
from app.deriv import DerivBotError, _to_float, _trade_payload


def _api_base() -> str:
    return get_settings().deriv_new_api_base.rstrip("/")


def _headers(access_token: str) -> dict:
    """Auth headers for the new Options REST API. Deriv-App-ID identifies the
    app (the alphanumeric OAuth client_id); the Bearer is the user's token."""
    return {
        "Authorization": f"Bearer {access_token}",
        "Deriv-App-ID": get_settings().deriv_app_id,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _is_demo(loginid: str, acct_type: str | None = None) -> bool:
    if acct_type:
        return acct_type.lower() in ("demo", "virtual", "vrt")
    return loginid.upper().startswith("VR")


async def list_accounts(access_token: str, timeout: float = 20.0) -> list[dict]:
    """List every Options trading account the token controls (demo + real), so
    the client can choose which to trade on. Returns
    [{loginid, currency, is_demo, type}]. The token is a Bearer header only."""
    url = f"{_api_base()}/trading/v1/options/accounts"
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, headers=_headers(access_token))
    if r.status_code >= 400:
        raise DerivBotError(f"account list failed: HTTP {r.status_code} {r.text[:200]}")
    body = r.json()
    # Be tolerant of the envelope: list may sit at top level, under "data",
    # or under "accounts".
    rows = body.get("data") if isinstance(body, dict) else body
    if isinstance(rows, dict):
        rows = rows.get("accounts") or rows.get("data") or []
    if not isinstance(rows, list):
        rows = body.get("accounts", []) if isinstance(body, dict) else []
    out: list[dict] = []
    for a in rows:
        if not isinstance(a, dict):
            continue
        loginid = a.get("loginid") or a.get("account_id") or a.get("id")
        if not loginid:
            continue
        acct_type = a.get("account_type") or a.get("type")
        out.append({
            "loginid": loginid,
            "currency": a.get("currency"),
            "is_demo": _is_demo(loginid, acct_type),
            "type": acct_type or ("demo" if _is_demo(loginid) else "real"),
        })
    if not out:
        raise DerivBotError(f"no accounts in response: {str(body)[:200]}")
    return out


async def request_otp_ws(access_token: str, loginid: str, timeout: float = 20.0) -> str:
    """Exchange the OAuth access token for a one-time OTP WebSocket URL bound to
    `loginid`. Returns the wss URL to connect to (it already encodes real/demo).
    The token is sent as a Bearer header — never in the URL/logs."""
    url = f"{_api_base()}/trading/v1/options/accounts/{loginid}/otp"
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, headers=_headers(access_token), json={})
        if r.status_code >= 400:
            raise DerivBotError(f"OTP request failed: HTTP {r.status_code} {r.text[:160]}")
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    # Response envelope may nest under "data". Deriv returns the full ws url
    # (preferred), or an otp we assemble into one.
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    ws_url = inner.get("url") or inner.get("ws_url") or inner.get("otp_url")
    otp = inner.get("otp")
    if not ws_url and otp:
        host = _api_base().split("://", 1)[-1]
        acct_type = "demo" if _is_demo(loginid, inner.get("account_type")) else "real"
        ws_url = f"wss://{host}/trading/v1/options/ws/{acct_type}?otp={otp}"
    if not ws_url:
        raise DerivBotError(f"OTP response had no WebSocket url: {str(data)[:200]}")
    return ws_url


async def _ws_request(ws_url: str, payload: dict, want: str, timeout: float = 30.0) -> dict:
    """Open the OTP-authenticated socket, send one request, return the matching
    response. No `authorize` — the OTP in the URL already authenticated us."""
    async with websockets.connect(ws_url, open_timeout=timeout) as ws:
        await ws.send(json.dumps(payload))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            msg = json.loads(raw)
            if "error" in msg:
                raise DerivBotError(msg["error"].get("message", "deriv error"))
            if msg.get("msg_type") == want or want in msg:
                return msg


async def balance(ws_url: str) -> dict:
    msg = await _ws_request(ws_url, {"balance": 1}, "balance")
    b = msg.get("balance") if isinstance(msg.get("balance"), dict) else msg
    return {"balance": _to_float(b.get("balance")), "currency": b.get("currency"),
            "loginid": b.get("loginid")}


def _new_params(body: dict) -> dict:
    """Convert the legacy buy `parameters` block to the new platform's shape:
      * `symbol` → `underlying_symbol` (the new field name)
      * drop `app_markup_percentage` — it's a documented LEGACY buy param only;
        the new Options buy doesn't list it, and sending an unknown field risks
        an "input validation failed" rejection. (Markup on the new platform is
        unconfirmed; revenue there would come via affiliate/markup-statistics.)
    """
    params = dict(body["parameters"])
    if "symbol" in params:
        params["underlying_symbol"] = params.pop("symbol")
    params.pop("app_markup_percentage", None)
    return params


async def proposal(ws_url: str, *, symbol, trade_type, direction, prediction,
                   duration, duration_unit, stake, currency="USD") -> dict:
    body = _trade_payload(symbol, trade_type, direction, prediction,
                          duration, duration_unit, stake, currency)
    p_params = _new_params(body)
    req = {"proposal": 1, "amount": stake, "basis": "stake",
           "contract_type": p_params["contract_type"],
           "currency": currency, "duration": int(duration),
           "duration_unit": duration_unit or "t",
           "underlying_symbol": p_params["underlying_symbol"]}
    if "barrier" in p_params:
        req["barrier"] = p_params["barrier"]
    msg = await _ws_request(ws_url, req, "proposal")
    p = msg.get("proposal") or {}
    return {"id": p.get("id"), "ask_price": _to_float(p.get("ask_price")),
            "payout": _to_float(p.get("payout")), "spot": _to_float(p.get("spot"))}


async def buy(ws_url: str, *, symbol, trade_type, direction, prediction,
              duration, duration_unit, stake, currency="USD") -> dict:
    """Place one contract via the OTP-authenticated socket (same buy payload as
    legacy, but `underlying_symbol` and including app_markup_percentage)."""
    body = _trade_payload(symbol, trade_type, direction, prediction,
                          duration, duration_unit, stake, currency)
    body["parameters"] = _new_params(body)
    msg = await _ws_request(ws_url, body, "buy")
    return msg.get("buy") or {}


async def check_contract(ws_url: str, contract_id) -> dict:
    msg = await _ws_request(
        ws_url, {"proposal_open_contract": 1, "contract_id": contract_id},
        "proposal_open_contract")
    poc = msg.get("proposal_open_contract") or {}
    return {"is_sold": bool(poc.get("is_sold")), "status": poc.get("status"),
            "profit": _to_float(poc.get("profit")),
            "payout": _to_float(poc.get("payout")),
            "app_markup_amount": _to_float(poc.get("app_markup_amount"))}
