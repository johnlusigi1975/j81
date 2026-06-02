"""Deriv WSS — authorize as a user, place contracts on their behalf.

Endpoints used:
  * `authorize`     — authenticate a user's token
  * `buy`           — place a contract; payload includes the markup our app
                      is registered for, Deriv automatically takes its cut and
                      credits us the markup on settlement
  * `proposal_open_contract` — could be used later to follow contract until
                                expiry; v1 just records the contract id

This module deliberately stays small and synchronous in shape (one connection
per trade). A pool would be faster but trading volume is low.
"""

from __future__ import annotations

import asyncio
import json

import websockets

from app.config import get_settings

# Use the user's registered app_id for WSS — markup is keyed off this.
DERIV_WS_URL_TEMPLATE = "wss://ws.binaryws.com/websockets/v3?app_id={app_id}"

# Deriv's public app_id — always valid on the v3 socket. Connecting, authorizing
# and quoting work under it; only markup (revenue) needs your own numeric app.
PUBLIC_WS_APP_ID = "1089"

# Deriv ENFORCES app_markup_percentage at max 3% (app_register schema: min 0,
# max 3). Marketing says "up to 5% for a limited time" but the API rejects >3%,
# so clamp here defensively.
MAX_MARKUP_PCT = 3.0


def _ws_app_id() -> str:
    """Return a NUMERIC app_id valid for the legacy v3 WebSocket.

    The new-platform OAuth client_id (deriv_app_id) is alphanumeric and 401s the
    v3 handshake, so it must never be used here. Preference order:
      1. deriv_ws_app_id  — your own numeric legacy app (earns markup)
      2. deriv_app_id      — only if it happens to be numeric (legacy app)
      3. "1089"            — Deriv's public app (works, but no markup)
    """
    s = get_settings()
    if s.deriv_ws_app_id and s.deriv_ws_app_id.isdigit():
        return s.deriv_ws_app_id
    if s.deriv_app_id and s.deriv_app_id.isdigit():
        return s.deriv_app_id
    return PUBLIC_WS_APP_ID


class DerivBotError(Exception):
    pass


def _trade_payload(
    symbol: str,
    trade_type: str,
    direction: str | None,
    prediction: int | None,
    duration: int,
    duration_unit: str,
    stake: float,
    currency: str = "USD",
) -> dict:
    """Build the `buy` payload for Deriv's WS API."""
    contract_type = _contract_type(trade_type, direction, prediction)
    # `price` is the MAX cost we'll accept. Per Deriv's markup docs the
    # client is debited stake + markup (markup = % of payout), so the cost
    # can exceed the bare stake. Give 20% headroom so the buy isn't rejected
    # on price — Deriv only ever charges the true cost, never more.
    body: dict = {
        "buy": 1,
        "subscribe": 0,
        "price": round(stake * 1.2, 2),
        "parameters": {
            "amount": stake,
            "basis": "stake",
            "contract_type": contract_type,
            "currency": currency,
            "duration": int(duration),
            "duration_unit": duration_unit or "t",
            "symbol": symbol,
        },
    }
    # Apply our app's markup per-trade. Deriv adds this as a % of the
    # contract PAYOUT and credits it to the app developer on settlement.
    # Without this the buy still works but earns us nothing. Deriv caps it
    # at 3% (MAX_MARKUP_PCT); clamp so a bad config can't get the buy rejected.
    # NOTE: markup is confirmed on the LEGACY buy only; deriv_new strips this
    # field for the new Options API where it isn't a documented buy param.
    markup_pct = get_settings().deriv_markup_percent or 0.0
    if markup_pct > 0:
        body["parameters"]["app_markup_percentage"] = round(min(markup_pct, MAX_MARKUP_PCT), 4)
    # Only digit-target contracts take a barrier (the predicted digit).
    # even_odd is NOT one — its parity is encoded in DIGITEVEN/DIGITODD, so
    # sending a barrier there makes Deriv reject the contract.
    if prediction is not None and trade_type == "over_under":
        body["parameters"]["barrier"] = str(prediction)
    return body


def _contract_type(trade_type: str, direction: str | None, prediction) -> str:
    """Map our Strategy trade_type + direction/prediction to Deriv's
    contract_type field. Reference: api.deriv.com / contracts_for."""
    t = (trade_type or "").lower()
    d = (direction or "").lower()
    if t == "rise_fall":
        return "CALL" if d == "up" else "PUT"
    if t == "higher_lower":
        return "CALL" if d == "up" else "PUT"
    if t == "even_odd":
        return "DIGITEVEN" if prediction in ("even", 0, "0") else "DIGITODD"
    if t == "over_under":
        return "DIGITOVER" if d == "over" else "DIGITUNDER"
    raise DerivBotError(f"unsupported trade_type for execution: {trade_type!r}")


async def place_contract(
    *,
    deriv_token: str,
    symbol: str,
    trade_type: str,
    direction: str | None,
    prediction: int | None,
    duration: int,
    duration_unit: str,
    stake: float,
    currency: str = "USD",
    timeout: float = 30.0,
) -> dict:
    """Open one Deriv contract on the authenticated user's account.

    Returns the parsed `buy` response from Deriv. Caller should check for
    response["error"] and handle errors. This function does NOT respect
    DRY_RUN — that gating happens in the executor; this function only
    runs when we genuinely want to talk to Deriv.
    """
    url = DERIV_WS_URL_TEMPLATE.format(app_id=_ws_app_id())

    async with websockets.connect(url, open_timeout=timeout) as ws:
        # 1. authorize
        await ws.send(json.dumps({"authorize": deriv_token, "req_id": 1}))
        auth_raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        auth_msg = json.loads(auth_raw)
        if "error" in auth_msg:
            raise DerivBotError(
                f"deriv authorize failed: {auth_msg['error'].get('message')}"
            )
        # 2. buy
        body = _trade_payload(
            symbol, trade_type, direction, prediction,
            duration, duration_unit, stake, currency,
        )
        body["req_id"] = 2
        await ws.send(json.dumps(body))
        buy_raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        buy_msg = json.loads(buy_raw)
        if "error" in buy_msg:
            raise DerivBotError(
                f"deriv buy failed: {buy_msg['error'].get('message')}"
            )
        return buy_msg.get("buy") or {}


async def topup_virtual(deriv_token: str, timeout: float = 20.0) -> dict:
    """Reset the virtual (demo) balance to 10,000 USD. Only valid on virtual
    accounts — Deriv returns an error on real accounts. Authorizes first,
    then sends {"topup_virtual":1}. Returns the new balance/currency."""
    url = DERIV_WS_URL_TEMPLATE.format(app_id=_ws_app_id())
    async with websockets.connect(url, open_timeout=timeout) as ws:
        await ws.send(json.dumps({"authorize": deriv_token, "req_id": 1}))
        auth_msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if "error" in auth_msg:
            raise DerivBotError(f"authorize failed: {auth_msg['error'].get('message')}")
        await ws.send(json.dumps({"topup_virtual": 1, "req_id": 2}))
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if "error" in msg:
            raise DerivBotError(msg["error"].get("message", "topup failed"))
        body = msg.get("topup_virtual") or {}
        return {"amount": float(body.get("amount") or 0),
                "currency": body.get("currency") or "USD",
                "balance": float(body.get("amount") or 0)}


async def authorize_account(deriv_token: str, timeout: float = 20.0) -> dict:
    """Authorize a token (a pasted Personal Access Token, or an OAuth token)
    and return the account it controls plus the user's full account list.

    This is how a user connects without the OAuth redirect dance: they paste a
    PAT and we authorize it. It also doubles as the legacy-vs-new platform
    check — if this succeeds, the account speaks the legacy API the Bot uses.
    Returns {loginid, currency, is_virtual, balance, account_list}. The token
    is never returned or logged."""
    url = DERIV_WS_URL_TEMPLATE.format(app_id=_ws_app_id())
    async with websockets.connect(url, open_timeout=timeout) as ws:
        await ws.send(json.dumps({"authorize": deriv_token, "req_id": 1}))
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
    if "error" in msg:
        raise DerivBotError(msg["error"].get("message", "authorize failed"))
    auth = msg.get("authorize") or {}
    return {
        "loginid": auth.get("loginid"),
        "currency": auth.get("currency"),
        "is_virtual": bool(auth.get("is_virtual")),
        "balance": auth.get("balance"),
        "account_list": auth.get("account_list") or [],
    }


async def check_contract(
    deriv_token: str, contract_id: str | int, timeout: float = 30.0
) -> dict:
    """Follow a placed contract to see if it has settled. Returns
    {is_sold, status, profit, sell_price, payout, app_markup_amount}.
    status is 'open' until the contract expires, then 'won' or 'lost'.

    `app_markup_amount` is the REAL commission Deriv credited us for this
    contract (a string in Deriv's payload) — we coerce it to float so the
    settler can store the true earned markup instead of the buy-time estimate."""
    url = DERIV_WS_URL_TEMPLATE.format(app_id=_ws_app_id())
    async with websockets.connect(url, open_timeout=timeout) as ws:
        await ws.send(json.dumps({"authorize": deriv_token, "req_id": 1}))
        auth = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if "error" in auth:
            raise DerivBotError(
                f"authorize failed: {auth['error'].get('message')}"
            )
        await ws.send(
            json.dumps({
                "proposal_open_contract": 1,
                "contract_id": contract_id,
                "req_id": 2,
            })
        )
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
    if "error" in msg:
        raise DerivBotError(
            f"contract lookup failed: {msg['error'].get('message')}"
        )
    poc = msg.get("proposal_open_contract") or {}
    return {
        "is_sold": bool(poc.get("is_sold")),
        "status": poc.get("status"),  # open | won | lost
        "profit": _to_float(poc.get("profit")),
        "sell_price": _to_float(poc.get("sell_price")),
        "payout": _to_float(poc.get("payout")),
        "app_markup_amount": _to_float(poc.get("app_markup_amount")),
    }


async def get_proposal(
    *,
    symbol: str,
    trade_type: str,
    direction: str | None,
    prediction: int | None,
    duration: int,
    duration_unit: str,
    stake: float,
    currency: str = "USD",
    timeout: float = 20.0,
) -> dict:
    """Ask Deriv for a real price quote BEFORE buying. This is a no-auth
    endpoint — it needs no token and touches no account — so we can call it
    even in DRY_RUN to learn the *true* payout (and therefore the true
    markup) instead of guessing with EST_PAYOUT_MULTIPLE.

    Returns {ask_price, payout, spot, app_markup_amount} (floats; missing
    fields come back as None). Caller decides what to do with it."""
    # A proposal is a no-auth quote (no markup involved); _ws_app_id() gives a
    # numeric app_id valid on v3 (your own, or the public 1089).
    url = DERIV_WS_URL_TEMPLATE.format(app_id=_ws_app_id())
    contract_type = _contract_type(trade_type, direction, prediction)
    req: dict = {
        "proposal": 1,
        "amount": stake,
        "basis": "stake",
        "contract_type": contract_type,
        "currency": currency,
        "duration": int(duration),
        "duration_unit": duration_unit or "t",
        "symbol": symbol,
        "req_id": 1,
    }
    if prediction is not None and trade_type == "over_under":
        req["barrier"] = str(prediction)
    async with websockets.connect(url, open_timeout=timeout) as ws:
        await ws.send(json.dumps(req))
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    msg = json.loads(raw)
    if "error" in msg:
        raise DerivBotError(
            f"proposal failed: {msg['error'].get('message')}"
        )
    p = msg.get("proposal") or {}
    details = p.get("contract_details") or {}
    return {
        "ask_price": _to_float(p.get("ask_price")),
        "payout": _to_float(p.get("payout")),
        "spot": _to_float(p.get("spot")),
        "app_markup_amount": _to_float(details.get("app_markup_amount")),
        "longcode": p.get("longcode"),
    }


async def fetch_proposal_payout(
    symbol: str,
    *,
    contract_type: str = "DIGITEVEN",
    duration: int = 1,
    stake: float = 1.0,
    timeout: float = 15.0,
) -> dict:
    """Live PAYOUT for a digit contract via the no-auth `proposal` endpoint.
    Used by the local library to compare Even/Odd payouts across markets —
    the one genuine EV lever is trading the highest-payout market.

    Maps the legacy contract_type back to (trade_type, direction, prediction)
    so we can call the bot's own get_proposal() (which is the same call,
    just shaped for the bot's trade UI)."""
    tt, direction, prediction = "even_odd", None, None
    ct = (contract_type or "").upper()
    if ct == "DIGITEVEN":
        tt, prediction = "even_odd", 0   # 0 == even in our scheme
    elif ct == "DIGITODD":
        tt, prediction = "even_odd", 1
    elif ct == "CALL":
        tt, direction = "rise_fall", "up"
    elif ct == "PUT":
        tt, direction = "rise_fall", "down"
    p = await get_proposal(
        symbol=symbol, trade_type=tt, direction=direction,
        prediction=prediction, duration=int(duration), duration_unit="t",
        stake=float(stake), timeout=timeout,
    )
    payout = float(p.get("payout") or 0.0)
    return {
        "payout": round(payout, 4),
        "ask_price": float(p.get("ask_price") or 0.0),
        "payout_pct": round(100.0 * payout / float(stake), 2) if stake else 0.0,
    }


async def sell_contract(
    deriv_token: str,
    contract_id: str | int,
    *,
    price: float = 0.0,
    timeout: float = 30.0,
) -> dict:
    """Sell an open contract before expiry (risk control / early exit).
    `price` is the MINIMUM acceptable sale price; 0 means 'sell at market'.
    Returns {sold_for, balance_after, transaction_id}."""
    url = DERIV_WS_URL_TEMPLATE.format(app_id=_ws_app_id())
    async with websockets.connect(url, open_timeout=timeout) as ws:
        await ws.send(json.dumps({"authorize": deriv_token, "req_id": 1}))
        auth = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if "error" in auth:
            raise DerivBotError(
                f"authorize failed: {auth['error'].get('message')}"
            )
        await ws.send(
            json.dumps({"sell": contract_id, "price": price, "req_id": 2})
        )
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
    if "error" in msg:
        raise DerivBotError(f"sell failed: {msg['error'].get('message')}")
    sell = msg.get("sell") or {}
    return {
        "sold_for": _to_float(sell.get("sold_for")),
        "balance_after": _to_float(sell.get("balance_after")),
        "transaction_id": sell.get("transaction_id"),
    }


def _to_float(v) -> float | None:
    """Deriv returns many numeric fields as strings (e.g. profit, payout,
    app_markup_amount). Coerce to float; return None if absent/unparseable."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def is_demo_account(deriv_account_id: str) -> bool:
    """Demo/virtual accounts by loginid prefix:
      * legacy demo  → VRT / VRTC
      * new Options demo → DOT  (real is ROT)
    Real-money accounts use CR / MF / MX / MLT / ROT / etc."""
    return (deriv_account_id or "").upper().startswith(("VRT", "DOT"))


# Rough payout multiple for synthetic rise/fall when we don't yet have a
# real quote (DRY_RUN). Deriv synthetics typically pay ~1.8-1.95x stake.
EST_PAYOUT_MULTIPLE = 1.9


def estimate_markup(payout: float) -> float:
    """Markup is a percentage of the contract PAYOUT (per Deriv's markup
    docs — e.g. 2% of a $50 payout = $1), credited to the app developer.
    Pass the contract's payout, NOT the stake. Live trades use the real
    payout from the buy response; DRY_RUN uses an estimated payout."""
    pct = min(get_settings().deriv_markup_percent or 0.0, MAX_MARKUP_PCT)
    return round(payout * (pct / 100.0), 6)
