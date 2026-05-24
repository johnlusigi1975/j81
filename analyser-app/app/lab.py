"""Live Testing Lab — paper-trades $1 across ALL trade types on live ticks.

No money, no Deriv account: it pulls REAL ticks and SIMULATES a $1 contract
for every trade type, scoring each against the actual next tick(s) using
Deriv's real payouts. This is how you *watch* the analyser try every trade
type at once and see which (if any) hold up — honestly.

Two honest truths this makes visible instead of hiding:
  * Synthetic indices are an audited RNG with a house edge, so over many
    trades every type drifts toward a small LOSS. The lab shows net P/L, not
    just win-rate, so you can see that.
  * You can WIN most trades and still LOSE money: DIFFERS wins ~90% of the
    time but pays ~1.05x; MATCHES wins ~10% but pays ~9.5x. Win-rate alone
    is the wrong target — net P/L (EV) is the real one.

State is in-memory only (a bounded ring buffer + counters) so it NEVER writes
to disk — the lab can run forever without filling the volume.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections import deque
from typing import Any

import websockets

from app.deriv import DERIV_WS_URL, DerivError, fetch_ticks
from app.even_odd import last_digits

# ---- what "all trade types" means here: one $1 contract per variant, per round.
#  (label, contract_type, duration_ticks, barrier-or-None, side-key)
#  barrier "auto:match" / "auto:diff" pick the predicted digit at sim time.
STAKE = 1.0
VARIANTS: list[dict] = [
    {"label": "RISE",    "ct": "CALL",      "dur": 5, "barrier": None,        "fam": "rise_fall"},
    {"label": "FALL",    "ct": "PUT",       "dur": 5, "barrier": None,        "fam": "rise_fall"},
    {"label": "EVEN",    "ct": "DIGITEVEN", "dur": 1, "barrier": None,        "fam": "even_odd"},
    {"label": "ODD",     "ct": "DIGITODD",  "dur": 1, "barrier": None,        "fam": "even_odd"},
    {"label": "OVER 4",  "ct": "DIGITOVER", "dur": 1, "barrier": "4",         "fam": "over_under"},
    {"label": "UNDER 5", "ct": "DIGITUNDER","dur": 1, "barrier": "5",         "fam": "over_under"},
    {"label": "MATCHES", "ct": "DIGITMATCH","dur": 1, "barrier": "auto:match","fam": "matches_differs"},
    {"label": "DIFFERS", "ct": "DIGITDIFF", "dur": 1, "barrier": "auto:diff", "fam": "matches_differs"},
]

# Approximate total payout per $1 stake, used only if a live payout fetch fails.
_FALLBACK_PAYOUT = {
    "CALL": 1.94, "PUT": 1.94,
    "DIGITEVEN": 1.95, "DIGITODD": 1.95,
    "DIGITOVER": 1.95, "DIGITUNDER": 1.95,
    "DIGITMATCH": 9.5, "DIGITDIFF": 1.05,
}

_FEED: deque[dict] = deque(maxlen=120)            # last N simulated trades
_STATS: dict[str, dict] = {}                       # per-label running totals
_PAYOUT: dict[str, tuple[float, float]] = {}       # cache: key -> (payout, ts)
_PAYOUT_TTL = 600.0                                # refresh live payouts every 10 min
_ROUNDS = 0


def _payout_key(symbol: str, ct: str, barrier: str | None) -> str:
    return f"{symbol}|{ct}|{barrier or ''}"


async def _fetch_payout(symbol: str, ct: str, dur: int, barrier: str | None,
                        timeout: float = 12.0) -> float:
    """Live total payout for a $1 contract via a no-auth `proposal`. Cached per
    (symbol, contract, barrier) for _PAYOUT_TTL. Falls back to the table on any
    error so the lab keeps running even if Deriv hiccups."""
    key = _payout_key(symbol, ct, barrier)
    hit = _PAYOUT.get(key)
    now = time.monotonic()
    if hit and (now - hit[1]) < _PAYOUT_TTL:
        return hit[0]
    req: dict[str, Any] = {
        "proposal": 1, "amount": STAKE, "basis": "stake",
        "contract_type": ct, "currency": "USD",
        "duration": int(dur), "duration_unit": "t",
        "symbol": symbol, "req_id": 1,
    }
    if barrier and not barrier.startswith("auto:"):
        req["barrier"] = barrier
    try:
        async with websockets.connect(DERIV_WS_URL, open_timeout=timeout) as ws:
            await ws.send(json.dumps(req))
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        msg = json.loads(raw)
        payout = float((msg.get("proposal") or {}).get("payout") or 0.0)
        if payout <= 0:
            raise DerivError("no payout")
    except Exception:
        payout = _FALLBACK_PAYOUT.get(ct, 1.94)
    _PAYOUT[key] = (payout, now)
    return payout


def _won_at(variant: dict, prices: list[float], digits: list[int],
            idx: int) -> tuple[bool, int | None]:
    """Score one simulated contract that SETTLES at tick `idx`. Returns
    (won, target_digit). For price contracts the entry is `dur` ticks earlier;
    for matches/differs the predicted digit is the tick just before settlement.
    Index-based so both the live lab and the backtester share one scorer."""
    ct = variant["ct"]
    dur = variant["dur"]
    if ct in ("CALL", "PUT"):
        entry, exit_ = prices[idx - dur], prices[idx]
        return ((exit_ > entry) if ct == "CALL" else (exit_ < entry)), None
    d = digits[idx]                                 # settling digit
    if ct == "DIGITEVEN":
        return d % 2 == 0, None
    if ct == "DIGITODD":
        return d % 2 == 1, None
    if ct == "DIGITOVER":
        return d > int(variant["barrier"]), None
    if ct == "DIGITUNDER":
        return d < int(variant["barrier"]), None
    target = digits[idx - 1]
    if ct == "DIGITMATCH":
        return d == target, target
    if ct == "DIGITDIFF":
        return d != target, target
    return False, None


def _won(variant: dict, prices: list[float], digits: list[int]) -> tuple[bool, int | None]:
    """Score against the LATEST tick (live lab convenience)."""
    return _won_at(variant, prices, digits, len(digits) - 1)


async def run_round(symbol: str) -> dict:
    """Place one $1 paper trade for EVERY trade type on the latest ticks of
    `symbol`, score each, and fold the results into the running stats + feed.
    One Deriv tick fetch per round; payouts are cached. Returns this round's
    trades + the fresh snapshot."""
    global _ROUNDS
    data = await fetch_ticks(symbol, count=60)
    prices = data.get("prices") or []
    pip = int(data.get("pip_size") or 2)
    if len(prices) < 8:
        return {"trades": [], "error": "not enough ticks yet", **snapshot()}
    digits = last_digits(prices, pip)
    ts = time.strftime("%H:%M:%S")
    new: list[dict] = []
    for v in VARIANTS:
        won, target = _won(v, prices, digits)
        payout = await _fetch_payout(symbol, v["ct"], v["dur"], v["barrier"])
        pnl = round(payout - STAKE, 4) if won else -STAKE
        label = v["label"] + ("" if target is None else f" {target}")
        rec = {"t": ts, "symbol": symbol, "fam": v["fam"], "label": label,
               "stake": STAKE, "won": won, "pnl": pnl, "payout": round(payout, 3)}
        new.append(rec)
        _FEED.appendleft(rec)
        s = _STATS.setdefault(v["label"], {"label": v["label"], "fam": v["fam"],
                                           "trades": 0, "wins": 0, "pnl": 0.0})
        s["trades"] += 1
        s["wins"] += 1 if won else 0
        s["pnl"] = round(s["pnl"] + pnl, 4)
    _ROUNDS += 1
    return {"trades": new, **snapshot()}


def snapshot() -> dict:
    rows = sorted(_STATS.values(), key=lambda s: s["fam"])
    for s in rows:
        s["win_rate"] = round(100.0 * s["wins"] / s["trades"], 1) if s["trades"] else 0.0
    net = round(sum(s["pnl"] for s in rows), 4)
    trades = sum(s["trades"] for s in rows)
    wins = sum(s["wins"] for s in rows)
    return {
        "rounds": _ROUNDS,
        "trades_total": trades,
        "win_rate": round(100.0 * wins / trades, 1) if trades else 0.0,
        "net_pnl": net,
        "per_type": rows,
        "feed": list(_FEED)[:60],
    }


def reset() -> dict:
    global _ROUNDS
    _FEED.clear()
    _STATS.clear()
    _ROUNDS = 0
    return snapshot()
