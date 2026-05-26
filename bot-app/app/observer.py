"""Live Market Observer — 24/7 background WS to Deriv (single-bot edition).

Originally lived in the separate analyser service; merged into the bot when
the tree was collapsed to a single service. Maintains an always-on WebSocket
subscription to ticks for all 10 synthetic markets, computes rolling per-market
stats, and periodically flags statistical anomalies for the brain advisor.

HONEST scope:
  * Deriv synthetics are an audited RNG. There is NO next-tick predictor here.
  * What this DOES find: sample-luck deviations (high z-scores), long streaks,
    distribution anomalies (chi-square). These are descriptive observations —
    they are NOT trading signals. On RNG, treating them as signals is the
    gambler's fallacy.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from typing import Any

import websockets

from app.deriv import DERIV_WS_URL_TEMPLATE, _ws_app_id


# The 10 synthetic markets the bot trades — copied locally so the observer
# doesn't depend on the (removed) analyser scanner module.
SCAN_SYMBOLS: list[tuple[str, str]] = [
    ("R_10", "Vol 10"), ("R_25", "Vol 25"), ("R_50", "Vol 50"),
    ("R_75", "Vol 75"), ("R_100", "Vol 100"),
    ("1HZ10V", "Vol 10 (1s)"), ("1HZ25V", "Vol 25 (1s)"),
    ("1HZ50V", "Vol 50 (1s)"), ("1HZ75V", "Vol 75 (1s)"),
    ("1HZ100V", "Vol 100 (1s)"),
]


_BUFFER = 200                # ticks kept per market
_FLAG_KEEP = 100             # most-recent flagged events kept in memory
_STATE: dict[str, dict[str, Any]] = {}
_FLAGGED: list[dict[str, Any]] = []
_META: dict[str, Any] = {"started_at": None, "ticks_seen": 0, "reconnects": 0, "last_error": None}


def _state(symbol: str) -> dict[str, Any]:
    s = _STATE.get(symbol)
    if s is None:
        s = {"symbol": symbol, "prices": [], "digits": [], "pip_size": 2, "last_ts": None}
        _STATE[symbol] = s
    return s


def _ingest(symbol: str, quote: float, pip_size: int, ts: int) -> None:
    s = _state(symbol)
    s["pip_size"] = int(pip_size or 2)
    s["last_ts"] = ts
    s["prices"].append(float(quote))
    if len(s["prices"]) > _BUFFER:
        s["prices"].pop(0)
    scale = 10 ** s["pip_size"]
    digit = int(round(float(quote) * scale)) % 10
    s["digits"].append(digit)
    if len(s["digits"]) > _BUFFER:
        s["digits"].pop(0)
    _META["ticks_seen"] += 1


def _market_stats(symbol: str) -> dict[str, Any]:
    s = _STATE.get(symbol)
    if not s or len(s["digits"]) < 30:
        return {"symbol": symbol, "ready": False, "ticks": len(s["digits"]) if s else 0}
    digits = s["digits"]; prices = s["prices"]; n = len(digits)
    even = sum(1 for d in digits if d % 2 == 0); odd = n - even
    eo_z = (2 * even - n) / math.sqrt(n)
    ups = sum(1 for i in range(1, len(prices)) if prices[i] > prices[i - 1])
    mv = len(prices) - 1
    up_z = (2 * ups - mv) / math.sqrt(mv) if mv else 0.0
    expected = n / 10.0
    freq = [0] * 10
    for d in digits:
        freq[d] += 1
    chi = sum((freq[d] - expected) ** 2 / expected for d in range(10))
    streak = 1; side = digits[-1] % 2
    for i in range(len(digits) - 2, -1, -1):
        if digits[i] % 2 == side:
            streak += 1
        else:
            break
    hot = max(range(10), key=lambda d: freq[d])
    cold = min(range(10), key=lambda d: freq[d])
    return {
        "symbol": symbol, "ready": True, "ticks": n,
        "even_pct": round(100 * even / n, 1), "odd_pct": round(100 * odd / n, 1),
        "eo_z": round(eo_z, 2),
        "up_pct": round(100 * ups / mv, 1) if mv else 50.0,
        "up_z": round(up_z, 2),
        "chi_square": round(chi, 2),
        "chi_anomalous_p01": chi > 21.67,
        "current_streak": streak, "streak_side": "even" if side == 0 else "odd",
        "freq": freq, "hot_digit": hot, "cold_digit": cold,
        "last_price": prices[-1] if prices else None,
        "last_ts": s["last_ts"],
    }


def snapshot() -> dict[str, Any]:
    return {
        "started_at": _META["started_at"],
        "ticks_seen": _META["ticks_seen"],
        "reconnects": _META["reconnects"],
        "last_error": _META["last_error"],
        "markets_ready": sum(1 for sym, _ in SCAN_SYMBOLS if len(_STATE.get(sym, {}).get("digits", [])) >= 30),
        "markets": [_market_stats(sym) for sym, _ in SCAN_SYMBOLS],
        "flagged": list(_FLAGGED[:30]),
    }


def patterns(limit: int = 30) -> list[dict[str, Any]]:
    return list(_FLAGGED[: max(1, int(limit))])


def _flag(item: dict[str, Any]) -> None:
    item["ts"] = time.time()
    _FLAGGED.insert(0, item)
    while len(_FLAGGED) > _FLAG_KEEP:
        _FLAGGED.pop()


def _detect_once() -> None:
    """Each market: flag z-scores > 2.5, chi-square anomalies, long streaks.
    Every flag carries an honest caveat — descriptive on RNG, not a signal."""
    for sym, _ in SCAN_SYMBOLS:
        st = _market_stats(sym)
        if not st.get("ready"):
            continue
        if abs(st["eo_z"]) > 2.5:
            _flag({"symbol": sym, "kind": "even_odd_deviation",
                   "magnitude_z": st["eo_z"],
                   "note": "Sample-luck range on a fair RNG. NOT a reversal signal."})
        if abs(st["up_z"]) > 2.5:
            _flag({"symbol": sym, "kind": "up_down_deviation",
                   "magnitude_z": st["up_z"],
                   "note": "Random walks do this. NOT a directional signal."})
        if st["chi_anomalous_p01"]:
            _flag({"symbol": sym, "kind": "digit_distribution_anomaly",
                   "chi_square": st["chi_square"],
                   "note": "Likely small-sample noise; uniformity is the long-run truth."})
        if st["current_streak"] >= 8:
            _flag({"symbol": sym, "kind": "long_parity_streak",
                   "streak": st["current_streak"], "side": st["streak_side"],
                   "note": "Streaks happen on RNG. Gambler's fallacy says reverse — DON'T. Each tick is still 50/50."})


class Observer:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def ensure_running(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        _META["started_at"] = time.time()
        await asyncio.sleep(8)  # let the service settle after boot
        await asyncio.gather(self._ws_loop(), self._detect_loop(), return_exceptions=True)

    async def _ws_loop(self) -> None:
        """Persistent WS subscription to all 10 markets, with reconnect."""
        url = DERIV_WS_URL_TEMPLATE.format(app_id=_ws_app_id())
        while True:
            try:
                async with websockets.connect(url, open_timeout=20) as ws:
                    rid = 0
                    for sym, _ in SCAN_SYMBOLS:
                        rid += 1
                        await ws.send(json.dumps({"ticks": sym, "subscribe": 1, "req_id": rid}))
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=180)
                        msg = json.loads(raw)
                        if msg.get("msg_type") == "tick":
                            t = msg.get("tick") or {}
                            sym = t.get("symbol"); q = t.get("quote")
                            if sym and q is not None:
                                _ingest(sym, float(q),
                                        int(t.get("pip_size") or 2),
                                        int(t.get("epoch") or 0))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _META["reconnects"] += 1
                _META["last_error"] = repr(exc)[:120]
                await asyncio.sleep(5)

    async def _detect_loop(self) -> None:
        await asyncio.sleep(60)
        while True:
            try:
                _detect_once()
            except Exception as exc:
                _META["last_error"] = repr(exc)[:120]
            await asyncio.sleep(60)


observer = Observer()


def observer_snapshot_safe() -> dict[str, Any]:
    """Wrapper for callers that just want the snapshot dict, never an exception."""
    try:
        return snapshot()
    except Exception:
        return {"markets": [], "flagged": [], "ticks_seen": 0, "markets_ready": 0}
