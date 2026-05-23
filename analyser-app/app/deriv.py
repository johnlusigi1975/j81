"""Minimal Deriv WebSocket client — historical candle fetching only.

Uses the public app_id (1089) Deriv provides for read-only access. No
account, no token, no trading — just `ticks_history` calls.

Endpoint:    wss://ws.binaryws.com/websockets/v3?app_id=1089
Docs:        https://api.deriv.com/api-explorer/#ticks_history

Protocol notes (so the code below makes sense):
  * One request per WS frame, response correlated by `req_id` echo.
  * `ticks_history` with `style: "candles"` returns OHLC arrays.
  * Hard limit ~5000 candles per response — caller must paginate by
    walking the `end` timestamp backward.

We do NOT keep the connection open: a single connection per fetch is
simple, robust to disconnects, and avoids needing a background task.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

import websockets

_log = logging.getLogger(__name__)

DERIV_WS_URL = "wss://ws.binaryws.com/websockets/v3?app_id=1089"
MAX_CANDLES_PER_REQUEST = 5000

# Per-symbol decimal precision ("pip size"), learned from API responses. This
# is what defines a symbol's "last digit" — R_100 quotes to 2 dp, others differ,
# so digit backtests MUST use the right precision or they score wrong digits.
_PIP_SIZE: dict[str, int] = {}


def pip_size_for(symbol: str) -> int | None:
    return _PIP_SIZE.get(symbol)


class DerivError(Exception):
    pass


@dataclass
class Candle:
    epoch: int
    open: float
    high: float
    low: float
    close: float

    @classmethod
    def from_api(cls, c: dict) -> "Candle":
        return cls(
            epoch=int(c["epoch"]),
            open=float(c["open"]),
            high=float(c["high"]),
            low=float(c["low"]),
            close=float(c["close"]),
        )

    def to_dict(self) -> dict:
        return {
            "epoch": self.epoch,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
        }


async def fetch_candles(
    symbol: str,
    granularity: int,
    *,
    start_epoch: int | None = None,
    end_epoch: int | None = None,
    count: int = MAX_CANDLES_PER_REQUEST,
    timeout: float = 20.0,
) -> list[Candle]:
    """One-shot fetch. Returns up to `count` candles. Caller paginates.

    Either pass both start_epoch/end_epoch, or just count with end_epoch
    (defaults to "latest" if neither is set).
    """
    if granularity not in {60, 120, 180, 300, 600, 900, 1800, 3600, 7200, 14400, 28800, 86400}:
        raise DerivError(f"unsupported granularity: {granularity}s")
    if count > MAX_CANDLES_PER_REQUEST:
        raise DerivError(f"max {MAX_CANDLES_PER_REQUEST} candles per request")

    req: dict = {
        "ticks_history": symbol,
        "style": "candles",
        "granularity": granularity,
        "adjust_start_time": 1,
        "req_id": 1,
    }
    if start_epoch is not None and end_epoch is not None:
        req["start"] = int(start_epoch)
        req["end"] = int(end_epoch)
    else:
        req["count"] = int(count)
        req["end"] = int(end_epoch) if end_epoch is not None else "latest"

    async with websockets.connect(DERIV_WS_URL, open_timeout=timeout) as ws:
        await ws.send(json.dumps(req))
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise DerivError("timed out waiting for Deriv response") from exc

    msg = json.loads(raw)
    if "error" in msg:
        raise DerivError(
            f"deriv api error: {msg['error'].get('message','unknown')}"
        )
    ps = msg.get("pip_size")
    if ps is not None:
        _PIP_SIZE[symbol] = int(ps)
    return [Candle.from_api(c) for c in msg.get("candles", [])]


async def fetch_ticks(
    symbol: str,
    *,
    count: int = 1000,
    end_epoch: int | None = None,
    timeout: float = 20.0,
) -> dict:
    """Fetch raw tick prices (NOT candles) for a symbol.

    Digit contracts (even/odd, over/under, matches/differs) settle on the
    last digit of an actual *tick*, not a 1-minute candle close — so for any
    digit analysis we must look at real ticks. Returns
    {prices: [...], times: [...], pip_size: int}. `pip_size` is how many
    decimal places the symbol quotes to, which is what defines its 'last
    digit'."""
    count = max(1, min(int(count), 5000))
    req = {
        "ticks_history": symbol,
        "style": "ticks",
        "count": count,
        "end": int(end_epoch) if end_epoch is not None else "latest",
        "adjust_start_time": 1,
        "req_id": 1,
    }
    async with websockets.connect(DERIV_WS_URL, open_timeout=timeout) as ws:
        await ws.send(json.dumps(req))
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise DerivError("timed out waiting for Deriv ticks") from exc
    msg = json.loads(raw)
    if "error" in msg:
        raise DerivError(f"deriv api error: {msg['error'].get('message','unknown')}")
    hist = msg.get("history") or {}
    return {
        "prices": [float(p) for p in hist.get("prices", [])],
        "times": [int(t) for t in hist.get("times", [])],
        "pip_size": int(msg.get("pip_size") or 2),
    }


async def fetch_proposal_payout(
    symbol: str,
    *,
    contract_type: str = "DIGITEVEN",
    duration: int = 1,
    stake: float = 1.0,
    timeout: float = 15.0,
) -> dict:
    """Get the live PAYOUT for a digit contract via a no-auth `proposal`
    (works on the public app_id). Used to compare Even/Odd payouts across
    markets — the one genuine EV lever is trading the highest-payout market.
    Returns {payout, ask_price, payout_pct} (payout_pct = payout/stake*100)."""
    req = {
        "proposal": 1, "amount": stake, "basis": "stake",
        "contract_type": contract_type, "currency": "USD",
        "duration": int(duration), "duration_unit": "t",
        "symbol": symbol, "req_id": 1,
    }
    async with websockets.connect(DERIV_WS_URL, open_timeout=timeout) as ws:
        await ws.send(json.dumps(req))
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise DerivError("timed out waiting for Deriv proposal") from exc
    msg = json.loads(raw)
    if "error" in msg:
        raise DerivError(f"deriv api error: {msg['error'].get('message','unknown')}")
    p = msg.get("proposal") or {}
    payout = float(p.get("payout") or 0.0)
    return {
        "payout": round(payout, 4),
        "ask_price": float(p.get("ask_price") or 0.0),
        "payout_pct": round(100.0 * payout / stake, 2) if stake else 0.0,
    }


async def fetch_history_paginated(
    symbol: str,
    granularity: int,
    *,
    end_epoch: int,
    total_candles: int,
    timeout: float = 20.0,
) -> list[Candle]:
    """Walk `end` backward until `total_candles` are collected (or the
    feed runs out). Returns candles oldest-first."""
    remaining = total_candles
    cursor = end_epoch
    collected: list[Candle] = []

    while remaining > 0:
        batch_size = min(remaining, MAX_CANDLES_PER_REQUEST)
        batch = await fetch_candles(
            symbol,
            granularity,
            end_epoch=cursor,
            count=batch_size,
            timeout=timeout,
        )
        if not batch:
            break
        # Deriv returns oldest-first within a batch. We have older batches
        # to pull, so move cursor to just-before-this-batch's first epoch.
        oldest = batch[0].epoch
        collected = batch + collected  # keep oldest-first ordering
        if oldest >= cursor:
            # Defensive: avoid infinite loop if the server doesn't advance.
            break
        cursor = oldest - 1
        remaining -= len(batch)
        # Tiny pause to be polite to Deriv's WS.
        await asyncio.sleep(0.05)

    return collected
