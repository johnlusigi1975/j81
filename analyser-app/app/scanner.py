"""Even/Odd multi-market scanner — the brain behind the 'M Pro' strategy.

For each synthetic market it pulls REAL recent ticks, measures the last-digit
even/odd bias, how stable that bias is across the window, and a composite
quality score, then ranks the markets by edge. This is the honest core: it
surfaces where a short-term skew exists right now (which can revert) — it does
NOT promise it will hold. The Bot's M Pro engine acts on the top of this list.
"""

from __future__ import annotations

from typing import Any

from app import even_odd
from app.deriv import DerivError, fetch_ticks

# The 10 markets Twinmil-style engines watch: the five Volatility indices and
# their 1-second variants.
SCAN_SYMBOLS: list[tuple[str, str]] = [
    ("R_10", "Vol 10"), ("R_25", "Vol 25"), ("R_50", "Vol 50"),
    ("R_75", "Vol 75"), ("R_100", "Vol 100"),
    ("1HZ10V", "Vol 10 (1s)"), ("1HZ25V", "Vol 25 (1s)"),
    ("1HZ50V", "Vol 50 (1s)"), ("1HZ75V", "Vol 75 (1s)"),
    ("1HZ100V", "Vol 100 (1s)"),
]

MIN_SAMPLES = 20   # need at least this many ticks before a market is "ready"


def _score_one(code: str, name: str, prices: list[float], pip_size: int) -> dict[str, Any]:
    n = len(prices)
    if n < MIN_SAMPLES:
        return {"symbol": code, "name": name, "samples": n, "ready": False,
                "collecting": f"{n}/{MIN_SAMPLES}"}
    digits = even_odd.last_digits(prices, pip_size)
    st = even_odd.even_odd_stats(digits)
    sig = even_odd.even_odd_signal(digits, min_samples=MIN_SAMPLES)
    even_pct, odd_pct = st["even_pct"], st["odd_pct"]
    gap = st["gap_pct"]
    # Stability: does the bias hold across the first vs second half?
    half = n // 2
    e1 = 100.0 * sum(1 for d in digits[:half] if d % 2 == 0) / max(half, 1)
    e2 = 100.0 * sum(1 for d in digits[half:] if d % 2 == 0) / max(n - half, 1)
    stability = max(0.0, 100.0 - 2.0 * abs(e1 - e2))
    sample_factor = min(1.0, n / 120.0)
    quality = round(min(100.0, 0.55 * min(gap * 2, 100) + 0.35 * stability + 0.10 * 100 * sample_factor))
    return {
        "symbol": code, "name": name, "samples": n, "ready": True,
        "even": round(even_pct, 1), "odd": round(odd_pct, 1),
        "gap": round(gap, 1), "stability": round(stability),
        "quality": quality,
        "direction": "even" if even_pct >= odd_pct else "odd",
        "digit_freq": st["digit_freq"],
        # richer, honest stats from the shared even_odd core:
        "z_even": st["z_even"], "p_value": st["p_value"],
        "runs_p": st["runs_p"], "current_streak": st["current_streak"],
        # gated signal the Bot can filter on (only fires on a strong condition):
        "signal": sig["call"], "confidence": sig["confidence"],
        "signal_reason": (sig["reasons"][0] if sig["reasons"] else ""),
    }


def _rf_score(code: str, name: str, prices: list[float]) -> dict[str, Any]:
    """Rise/Fall view of a market: up% of recent moves + a z-score confidence
    (how far the up/down split sits from 50/50). Honest: descriptive, not a
    prediction — ticks are independent ~50/50."""
    n = len(prices)
    if n < MIN_SAMPLES:
        return {"symbol": code, "name": name, "ready": False, "collecting": f"{n}/{MIN_SAMPLES}"}
    moves = [1 if prices[i] > prices[i-1] else (0 if prices[i] < prices[i-1] else -1) for i in range(1, n)]
    moves = [m for m in moves if m >= 0]
    mv = len(moves) or 1
    ups = sum(moves)
    up_pct = 100.0 * ups / mv
    z = abs(2*ups - mv) / (mv ** 0.5) if mv else 0.0
    conf = max(0, min(99, round(z / 3.0 * 100)))
    return {"symbol": code, "name": name, "ready": True, "samples": n,
            "up_pct": round(up_pct, 1), "down_pct": round(100.0 - up_pct, 1),
            "confidence": conf, "direction": "up" if up_pct >= 50 else "down"}


async def scan_rise_fall(count: int = 120) -> dict[str, Any]:
    """Scan all synthetic markets for Rise/Fall and rank by confidence. Used by
    the Bot's server-side (VPS) confidence-gated auto-runner."""
    rows: list[dict[str, Any]] = []
    for code, name in SCAN_SYMBOLS:
        try:
            data = await fetch_ticks(code, count=count)
            rows.append(_rf_score(code, name, data["prices"]))
        except Exception:
            rows.append({"symbol": code, "name": name, "ready": False, "collecting": f"0/{MIN_SAMPLES}"})
    ready = [r for r in rows if r.get("ready")]
    ready.sort(key=lambda r: r["confidence"], reverse=True)
    for i, r in enumerate(ready, 1):
        r["rank"] = i
    return {"ranked": ready + [r for r in rows if not r.get("ready")],
            "top": ready[0] if ready else None,
            "markets_ready": len(ready), "markets_total": len(rows)}


async def scan_even_odd(count: int = 120) -> dict[str, Any]:
    """Scan every market, score + rank by edge (gap, then quality). Returns the
    ranked list plus the current top pick (best ready market)."""
    rows: list[dict[str, Any]] = []
    for code, name in SCAN_SYMBOLS:
        try:
            data = await fetch_ticks(code, count=count)
            rows.append(_score_one(code, name, data["prices"], data["pip_size"]))
        except DerivError:
            rows.append({"symbol": code, "name": name, "samples": 0, "ready": False,
                         "collecting": f"0/{MIN_SAMPLES}"})
        except Exception:
            rows.append({"symbol": code, "name": name, "samples": 0, "ready": False,
                         "collecting": f"0/{MIN_SAMPLES}"})
    ready = [r for r in rows if r.get("ready")]
    # Rank by gated signal strength first (confidence), then raw edge/quality —
    # so the top pick is the market with the strongest, rarest condition now.
    ready.sort(key=lambda r: (r.get("confidence", 0.0), r["gap"], r["quality"]), reverse=True)
    not_ready = [r for r in rows if not r.get("ready")]
    ordered = ready + not_ready
    for i, r in enumerate(ready, 1):
        r["rank"] = i
    return {
        "ranked": ordered,
        "top": ready[0] if ready else None,
        "markets_ready": len(ready),
        "markets_total": len(rows),
    }
