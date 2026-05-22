"""Technical indicators — pure Python, no NumPy/pandas dependency.

Designed to align with the Strategy schema's `Indicator` type values:
RSI, SMA, EMA, MACD, BBANDS, STOCH, ATR. Each function returns a list
the same length as the input series with `None` for warmup positions
where the indicator can't be computed yet. Downstream code should treat
None values as "indicator not ready — bar is not eligible".

Why no pandas-ta: backtest volumes here are small (~1,440 candles/day,
~50k candles for a month). Pure Python loops finish in milliseconds and
keep the deploy lean.
"""

from __future__ import annotations

import math
from typing import Any


# --------------------------------------------------------------------- core


def sma(values: list[float], period: int) -> list[float | None]:
    """Simple moving average."""
    out: list[float | None] = [None] * len(values)
    if period <= 0 or period > len(values):
        return out
    running = sum(values[0:period])
    out[period - 1] = running / period
    for i in range(period, len(values)):
        running += values[i] - values[i - period]
        out[i] = running / period
    return out


def ema(values: list[float], period: int) -> list[float | None]:
    """Exponential moving average. Seeded with SMA over the first `period`."""
    out: list[float | None] = [None] * len(values)
    if period <= 0 or period > len(values):
        return out
    alpha = 2.0 / (period + 1)
    seed = sum(values[0:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = (values[i] - prev) * alpha + prev
        out[i] = prev
    return out


def _wilder(values: list[float], period: int) -> list[float | None]:
    """Wilder smoothing — used for RSI and ATR. Seeded with simple average."""
    out: list[float | None] = [None] * len(values)
    if period <= 0 or period > len(values):
        return out
    seed = sum(values[0:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


def rsi(closes: list[float], period: int = 14) -> list[float | None]:
    """Wilder RSI. None for first `period` bars."""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n < period + 1:
        return out
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        gains[i] = d if d > 0 else 0.0
        losses[i] = -d if d < 0 else 0.0
    avg_gain = _wilder(gains[1:], period)
    avg_loss = _wilder(losses[1:], period)
    # _wilder returns same-length list as input; shift back into the closes-aligned `out`.
    for i, (g, l) in enumerate(zip(avg_gain, avg_loss), start=1):
        if g is None or l is None:
            continue
        if l == 0:
            out[i] = 100.0
        else:
            rs = g / l
            out[i] = 100.0 - (100.0 / (1 + rs))
    return out


def macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> dict[str, list[float | None]]:
    """Returns {'macd': ..., 'signal': ..., 'hist': ...}."""
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    macd_line: list[float | None] = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_ema, slow_ema)
    ]
    # signal EMA is computed on the non-None tail of the MACD series
    valid = [v for v in macd_line if v is not None]
    sig_tail = ema(valid, signal)
    signal_line: list[float | None] = [None] * len(macd_line)
    offset = len(macd_line) - len(valid)
    for i, v in enumerate(sig_tail):
        if v is not None:
            signal_line[offset + i] = v
    hist: list[float | None] = [
        (m - s) if (m is not None and s is not None) else None
        for m, s in zip(macd_line, signal_line)
    ]
    return {"macd": macd_line, "signal": signal_line, "hist": hist}


def bbands(
    closes: list[float], period: int = 20, std_dev: float = 2.0
) -> dict[str, list[float | None]]:
    """Bollinger Bands. Returns {'middle': SMA, 'upper': mid + k*std, 'lower': mid - k*std}."""
    mid = sma(closes, period)
    upper: list[float | None] = [None] * len(closes)
    lower: list[float | None] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1 : i + 1]
        m = sum(window) / period
        var = sum((x - m) ** 2 for x in window) / period
        sd = math.sqrt(var)
        upper[i] = m + std_dev * sd
        lower[i] = m - std_dev * sd
    return {"middle": mid, "upper": upper, "lower": lower}


def stoch(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    k_period: int = 14,
    d_period: int = 3,
) -> dict[str, list[float | None]]:
    """Stochastic oscillator %K / %D."""
    n = len(closes)
    k: list[float | None] = [None] * n
    for i in range(k_period - 1, n):
        hh = max(highs[i - k_period + 1 : i + 1])
        ll = min(lows[i - k_period + 1 : i + 1])
        denom = hh - ll
        k[i] = 50.0 if denom == 0 else 100.0 * (closes[i] - ll) / denom
    # %D is SMA of %K over the non-None tail
    k_valid = [v for v in k if v is not None]
    d_tail = sma(k_valid, d_period)
    d: list[float | None] = [None] * n
    offset = n - len(k_valid)
    for i, v in enumerate(d_tail):
        d[offset + i] = v
    return {"k": k, "d": d}


def atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> list[float | None]:
    """Average True Range via Wilder smoothing of TR."""
    n = len(closes)
    if n < 2:
        return [None] * n
    tr = [0.0] * n
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    return _wilder(tr, period)


# ----------------------------------------------------------------- dispatch


def compute(
    candles: list[dict[str, Any]], ind_type: str, params: dict[str, Any] | None = None
) -> dict[str, list[float | None]]:
    """Compute one indicator on a candle series.

    Returns a dict so multi-line indicators (MACD, BBANDS, STOCH) fit the
    same shape as single-line ones. The keys are the standard component
    names; for single-line indicators the only key is 'value'."""
    params = params or {}
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    t = ind_type.strip().upper()
    if t == "RSI":
        return {"value": rsi(closes, int(params.get("period", 14)))}
    if t == "SMA":
        return {"value": sma(closes, int(params.get("period", 20)))}
    if t == "EMA":
        return {"value": ema(closes, int(params.get("period", 20)))}
    if t == "MACD":
        return macd(
            closes,
            fast=int(params.get("fast", 12)),
            slow=int(params.get("slow", 26)),
            signal=int(params.get("signal", 9)),
        )
    if t in ("BBANDS", "BB"):
        return bbands(
            closes,
            period=int(params.get("period", 20)),
            std_dev=float(params.get("std_dev", params.get("std", 2.0))),
        )
    if t in ("STOCH", "STOCHASTIC"):
        return stoch(
            highs,
            lows,
            closes,
            k_period=int(params.get("k_period", params.get("k", 14))),
            d_period=int(params.get("d_period", params.get("d", 3))),
        )
    if t == "ATR":
        return {"value": atr(highs, lows, closes, int(params.get("period", 14)))}
    raise ValueError(
        f"unsupported indicator type: {ind_type!r}. "
        "Supported: RSI, SMA, EMA, MACD, BBANDS, STOCH, ATR"
    )
