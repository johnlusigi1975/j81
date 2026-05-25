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


async def refresh_token(refresh_tok: str, timeout: float = 20.0) -> dict:
    """Exchange an OAuth refresh token for a fresh access token (and possibly a
    rotated refresh token) at the OAuth2 token endpoint. Returns the raw token
    response: {access_token, refresh_token?, expires_in?}. Raises on failure."""
    s = get_settings()
    url = s.deriv_oauth_token_url
    if not url:
        raise DerivBotError("no OAuth token URL configured — cannot refresh")
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_tok,
        "client_id": s.deriv_app_id,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, data=data)
    if r.status_code >= 400:
        raise DerivBotError(f"token refresh failed: HTTP {r.status_code} {r.text[:160]}")
    tok = r.json()
    if not tok.get("access_token"):
        raise DerivBotError("refresh returned no access_token")
    return tok


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
    The token is sent as a Bearer header — never in the URL/logs.

    Deriv rate-limits this endpoint (HTTP 429), so we retry a few times with
    backoff before giving up — smooths over bursts (a trade + its settle polls)."""
    url = f"{_api_base()}/trading/v1/options/accounts/{loginid}/otp"
    data: dict = {}
    delays = [0.0, 1.5, 3.0, 5.0]   # first try immediate, then back off
    last_err = ""
    for i, delay in enumerate(delays):
        if delay:
            await asyncio.sleep(delay)
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, headers=_headers(access_token), json={})
        if r.status_code == 429:           # rate-limited — wait and retry
            last_err = f"HTTP 429 {r.text[:120]}"
            continue
        if r.status_code >= 400:
            raise DerivBotError(f"OTP request failed: HTTP {r.status_code} {r.text[:160]}")
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        break
    else:
        raise DerivBotError(f"OTP request failed after retries: {last_err or 'rate limited'}")
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
              duration, duration_unit, stake, currency="USD", timeout: float = 20.0) -> dict:
    """Place one contract on the new Options API: PROPOSAL then BUY-by-id, on a
    single OTP-authenticated socket (the new API expects a proposal id, and the
    OTP authenticates only this one connection — so both must share it)."""
    body = _trade_payload(symbol, trade_type, direction, prediction,
                          duration, duration_unit, stake, currency)
    params = _new_params(body)
    preq = {"proposal": 1, "amount": stake, "basis": "stake",
            "contract_type": params["contract_type"], "currency": currency,
            "duration": int(duration), "duration_unit": duration_unit or "t",
            "underlying_symbol": params["underlying_symbol"], "req_id": 1}
    if "barrier" in params:
        preq["barrier"] = params["barrier"]
    async with websockets.connect(ws_url, open_timeout=timeout) as ws:
        await ws.send(json.dumps(preq))
        pid = ask = None
        for _ in range(25):
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if "error" in m:
                raise DerivBotError(m["error"].get("message", "proposal failed"))
            if m.get("msg_type") == "proposal":
                p = m.get("proposal") or {}
                pid, ask = p.get("id"), _to_float(p.get("ask_price"))
                break
        if not pid:
            raise DerivBotError("Deriv returned no proposal id")
        await ws.send(json.dumps({"buy": pid, "price": ask if ask is not None else stake, "req_id": 2}))
        for _ in range(25):
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if "error" in m:
                raise DerivBotError(m["error"].get("message", "buy failed"))
            if m.get("msg_type") == "buy":
                return m.get("buy") or {}
    raise DerivBotError("no buy confirmation from Deriv")


async def check_contract(ws_url: str, contract_id) -> dict:
    msg = await _ws_request(
        ws_url, {"proposal_open_contract": 1, "contract_id": contract_id},
        "proposal_open_contract")
    poc = msg.get("proposal_open_contract") or {}
    return {"is_sold": bool(poc.get("is_sold")), "status": poc.get("status"),
            "profit": _to_float(poc.get("profit")),
            "payout": _to_float(poc.get("payout")),
            "app_markup_amount": _to_float(poc.get("app_markup_amount"))}
