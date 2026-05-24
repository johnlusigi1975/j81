"""Wraps the Deriv trade call with risk limits and the DRY_RUN safety net.

Every code path that could touch real money goes through here. The
DRY_RUN flag in settings is the master switch — when True, this module
records the intent but NEVER calls Deriv. That's what lets us test the
whole flow end-to-end without a real Deriv app being registered yet.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings
from app.deriv import (
    EST_PAYOUT_MULTIPLE,
    DerivBotError,
    estimate_markup,
    get_proposal,
    place_contract,
)
from app.store import get_store


class ExecutorError(Exception):
    pass


# ---------- talk to the Analyser ----------------------------------------


async def get_decision(symbol: str, trade_type: str | None = None) -> dict:
    """Ask the J81 Analyser what it thinks. Returns the decision dict
    (with is_trade, confidence, etc.) or {} on failure."""
    settings = get_settings()
    if not settings.analyser_url:
        return {}
    url = settings.analyser_url.rstrip("/") + "/decide"
    body = {"symbol": symbol}
    if trade_type:
        body["trade_type"] = trade_type
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return {}


async def get_rise_fall_scan() -> dict:
    """Ask the Analyser to scan all markets for Rise/Fall and return the ranked
    list + top {symbol, direction, confidence}. {} on failure."""
    settings = get_settings()
    if not settings.analyser_url:
        return {}
    url = settings.analyser_url.rstrip("/") + "/scan/rise_fall"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return {}


# ---------- risk gates --------------------------------------------------


def goal_status(account: dict) -> tuple[bool, str | None]:
    """Has today's realized profit hit this account's take-profit target or
    loss limit? Returns (blocked, friendly_reason). Profit comes from settled
    live/demo trades (dry-run trades don't settle, so contribute 0)."""
    pnl = get_store().profit_today(account["id"])
    tp = account.get("take_profit")
    ll = account.get("daily_loss_limit")
    if tp and pnl >= float(tp):
        return True, f"🎉 take-profit reached (+{pnl:.2f} ≥ +{float(tp):.2f}) — paused for today"
    if ll and pnl <= -abs(float(ll)):
        return True, f"🛑 loss limit reached ({pnl:.2f}) — paused for today to protect you"
    return False, None


# ---------- platform-aware execution (legacy WS vs new OTP-WS) -----------


async def _live_buy(account: dict, token: str, **p) -> dict:
    """Place ONE real contract on whichever Deriv platform the account is on:
      * legacy → authorize + buy on the classic WS (place_contract)
      * new    → request a one-time OTP socket, then buy on it (deriv_new)
    Returns the `buy` dict (contract_id, payout, buy_price)."""
    if (account.get("platform") or "legacy").lower() == "new":
        from app import deriv_new
        ws_url = await deriv_new.request_otp_ws(token, account["deriv_account_id"])
        return await deriv_new.buy(ws_url, **p)
    return await place_contract(deriv_token=token, **p)


async def _live_check(account: dict, token: str, contract_id) -> dict:
    """Follow a contract to settlement on the right platform."""
    if (account.get("platform") or "legacy").lower() == "new":
        from app import deriv_new
        ws_url = await deriv_new.request_otp_ws(token, account["deriv_account_id"])
        return await deriv_new.check_contract(ws_url, contract_id)
    from app.deriv import check_contract
    return await check_contract(token, contract_id)


def _check_risk_limits(account: dict, decision: dict) -> tuple[bool, str]:
    """Returns (ok, reason). Each check is one short conditional so it's
    obvious which rule blocked a trade."""
    store = get_store()

    if not account.get("enabled"):
        return False, "account is disabled (user hasn't opted in)"

    blocked, why = goal_status(account)
    if blocked:
        return False, why

    if not decision.get("is_trade"):
        return False, f"analyser said no trade: {decision.get('rationale','')}"

    conf = float(decision.get("confidence", 0))
    if conf < float(account.get("min_confidence") or 0):
        return False, f"confidence {conf:.2f} below user threshold {account['min_confidence']:.2f}"

    # Per-day cap
    todays = store.trades_today(account["id"])
    cap = int(account.get("max_trades_per_day") or 0)
    if cap and todays >= cap:
        return False, f"hit daily cap ({todays}/{cap})"

    # Allow-list filters
    import json as _json
    allow_tt = account.get("allowed_trade_types")
    if allow_tt:
        allow_list = _json.loads(allow_tt) if isinstance(allow_tt, str) else allow_tt
        if allow_list and decision.get("trade_type") not in allow_list:
            return False, f"trade_type {decision.get('trade_type')!r} not in user's allow-list"
    allow_sym = account.get("allowed_symbols")
    if allow_sym:
        allow_list = _json.loads(allow_sym) if isinstance(allow_sym, str) else allow_sym
        if allow_list and decision.get("symbol") not in allow_list:
            return False, f"symbol {decision.get('symbol')!r} not in user's allow-list"

    return True, "ok"


# ---------- execute ------------------------------------------------------


async def execute_decision_for_account(account: dict, decision: dict) -> dict:
    """Decide if + how to act on a decision for one account. Always returns
    a record describing what happened, whether dry-run or live."""
    settings = get_settings()
    ok, reason = _check_risk_limits(account, decision)
    if not ok:
        return {
            "account_id": account["id"],
            "outcome": "skipped",
            "reason": reason,
        }

    stake = min(
        float(account.get("max_stake_per_trade") or settings.default_max_stake_per_trade),
        settings.default_max_stake_per_trade,  # global ceiling as defensive cap
    )
    trade_intent = {
        "account_id": account["id"],
        "deriv_account": account["deriv_account_id"],
        "symbol": decision.get("symbol"),
        "trade_type": decision.get("trade_type"),
        "direction": decision.get("direction"),
        "prediction": decision.get("prediction"),
        "duration": decision.get("duration") or 5,
        "duration_unit": decision.get("duration_unit") or "t",
        "stake": stake,
        "confidence": decision.get("confidence"),
        "decision_payload": decision,
        # Markup is % of PAYOUT (per Deriv docs). Filled in below from a real
        # `proposal` quote when possible; falls back to an estimate.
        "markup_earned": estimate_markup(stake * EST_PAYOUT_MULTIPLE),
    }

    # Get a REAL quote (no-auth, no account touched) so both dry-run and live
    # trades know the true payout — and therefore the true markup — instead of
    # the 1.9x guess. Best-effort: a failed quote must never block a trade.
    quote = None
    try:
        quote = await get_proposal(
            symbol=trade_intent["symbol"],
            trade_type=trade_intent["trade_type"],
            direction=trade_intent["direction"],
            prediction=trade_intent["prediction"],
            duration=trade_intent["duration"],
            duration_unit=trade_intent["duration_unit"],
            stake=stake,
        )
    except Exception:
        quote = None
    if quote and quote.get("payout"):
        trade_intent["payout"] = quote["payout"]
        trade_intent["markup_earned"] = estimate_markup(quote["payout"])

    if settings.dry_run:
        trade_intent["is_dry_run"] = True
        trade_intent["outcome"] = "dry_run"
        trade_id = get_store().record_trade(trade_intent)
        return {**trade_intent, "trade_id": trade_id, "outcome": "dry_run"}

    # Live path. We get here only when DRY_RUN=false explicitly.
    token = get_store().decrypted_token_for(account["id"])
    if not token:
        trade_intent["is_dry_run"] = False
        trade_intent["outcome"] = "error"
        trade_intent["error"] = "decryption failed — encryption key may have changed"
        get_store().record_trade(trade_intent)
        return trade_intent

    try:
        buy = await _live_buy(
            account, token,
            symbol=trade_intent["symbol"],
            trade_type=trade_intent["trade_type"],
            direction=trade_intent["direction"],
            prediction=trade_intent["prediction"],
            duration=trade_intent["duration"],
            duration_unit=trade_intent["duration_unit"],
            stake=trade_intent["stake"],
        )
    except DerivBotError as exc:
        trade_intent["is_dry_run"] = False
        trade_intent["outcome"] = "error"
        trade_intent["error"] = str(exc)
        get_store().record_trade(trade_intent)
        return trade_intent

    trade_intent["is_dry_run"] = False
    trade_intent["outcome"] = "pending"  # will be settled by Deriv
    trade_intent["deriv_contract_id"] = buy.get("contract_id")
    # The buy response carries the REAL payout + price paid. Use them to
    # record the accurate markup at buy time; the settler later overwrites
    # markup_earned with Deriv's exact app_markup_amount on settlement.
    from app.deriv import _to_float
    real_payout = _to_float(buy.get("payout"))
    if real_payout:
        trade_intent["payout"] = real_payout
        trade_intent["markup_earned"] = estimate_markup(real_payout)
    buy_price = _to_float(buy.get("buy_price"))
    if buy_price is not None:
        trade_intent["buy_price"] = buy_price
    trade_id = get_store().record_trade(trade_intent)
    return {**trade_intent, "trade_id": trade_id}


# ---------- manual trade (the simple grandma "Trade" panel) -------------

# The two simple sections we support first. Everything else is future work.
MANUAL_TRADE_TYPES = ("rise_fall", "even_odd", "over_under", "matches_differs")


async def execute_manual_trade(
    account: dict,
    *,
    trade_type: str,
    symbol: str,
    direction: str | None = None,   # rise_fall: "up"/"down"
    prediction: str | None = None,  # even_odd: "even"/"odd"
    stake: float,
    duration: int = 5,
    duration_unit: str = "t",
) -> dict:
    """Place a trade the USER chose directly (not the Analyser). Keeps the
    money-safety gates — account enabled, daily cap, stake ceiling, DRY_RUN —
    but skips the 'analyser said no' / confidence / allow-list gates because
    the human is the decider here. Only Rise/Fall + Even/Odd for now."""
    settings = get_settings()
    store = get_store()

    tt = (trade_type or "").lower()
    if tt not in MANUAL_TRADE_TYPES:
        return {"outcome": "error", "error": f"trade_type {trade_type!r} not supported (Rise/Fall, Even/Odd, Over/Under, Matches/Differs)"}
    if not account.get("enabled"):
        return {"outcome": "skipped", "reason": "account is disabled — enable it first"}

    blocked, why = goal_status(account)
    if blocked:
        return {"outcome": "skipped", "reason": why}

    todays = store.trades_today(account["id"])
    cap = int(account.get("max_trades_per_day") or 0)
    if cap and todays >= cap:
        return {"outcome": "skipped", "reason": f"hit daily cap ({todays}/{cap})"}

    stake = min(
        float(stake or 0) or settings.default_max_stake_per_trade,
        float(account.get("max_stake_per_trade") or settings.default_max_stake_per_trade),
        settings.default_max_stake_per_trade,  # global defensive ceiling
    )
    if stake <= 0:
        return {"outcome": "error", "error": "stake must be greater than 0"}

    trade_intent = {
        "account_id": account["id"],
        "deriv_account": account["deriv_account_id"],
        "symbol": symbol,
        "trade_type": tt,
        "direction": direction,
        "prediction": prediction,
        "duration": int(duration) or 5,
        "duration_unit": duration_unit or "t",
        "stake": stake,
        "confidence": 1.0,  # the human chose it
        "decision_payload": {"manual": True},
        "markup_earned": estimate_markup(stake * EST_PAYOUT_MULTIPLE),
    }

    # Real quote (no-auth) for an accurate payout/markup, dry-run or live.
    try:
        quote = await get_proposal(
            symbol=symbol, trade_type=tt, direction=direction, prediction=prediction,
            duration=trade_intent["duration"], duration_unit=trade_intent["duration_unit"],
            stake=stake,
        )
    except Exception:
        quote = None
    if quote and quote.get("payout"):
        trade_intent["payout"] = quote["payout"]
        trade_intent["markup_earned"] = estimate_markup(quote["payout"])

    if settings.dry_run:
        trade_intent["is_dry_run"] = True
        trade_intent["outcome"] = "dry_run"
        trade_id = store.record_trade(trade_intent)
        return {**trade_intent, "trade_id": trade_id, "outcome": "dry_run"}

    token = store.decrypted_token_for(account["id"])
    if not token:
        trade_intent.update(is_dry_run=False, outcome="error",
                            error="decryption failed — encryption key may have changed")
        store.record_trade(trade_intent)
        return trade_intent
    try:
        buy = await _live_buy(
            account, token, symbol=symbol, trade_type=tt, direction=direction,
            prediction=prediction, duration=trade_intent["duration"],
            duration_unit=trade_intent["duration_unit"], stake=stake,
        )
    except DerivBotError as exc:
        trade_intent.update(is_dry_run=False, outcome="error", error=str(exc))
        store.record_trade(trade_intent)
        return trade_intent

    from app.deriv import _to_float
    trade_intent["is_dry_run"] = False
    trade_intent["outcome"] = "pending"
    trade_intent["deriv_contract_id"] = buy.get("contract_id")
    real_payout = _to_float(buy.get("payout"))
    if real_payout:
        trade_intent["payout"] = real_payout
        trade_intent["markup_earned"] = estimate_markup(real_payout)
    bp = _to_float(buy.get("buy_price"))
    if bp is not None:
        trade_intent["buy_price"] = bp
    trade_id = store.record_trade(trade_intent)
    return {**trade_intent, "trade_id": trade_id}
