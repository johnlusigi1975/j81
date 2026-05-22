"""Orchestrates Deriv data: fetches, caches in SQLite, reads back.

The backtest engine (Phase 2.3) will read candles via `get_candles`.
For now this module exposes a small API surface the homepage can drive
to populate the cache.
"""

from __future__ import annotations

import time
from typing import Any

from app.deriv import (
    MAX_CANDLES_PER_REQUEST,
    Candle,
    DerivError,
    fetch_history_paginated,
    fetch_ticks,
)
from app.store import get_store


async def ensure_candles(
    symbol: str,
    granularity: int,
    days: float = 7.0,
    *,
    end_epoch: int | None = None,
) -> dict[str, Any]:
    """Fetch ~`days` worth of candles ending at `end_epoch` (default now)
    and store them. Returns a summary of what was fetched + cached.

    Idempotent: re-running with the same arguments adds 0 new candles
    because the (symbol, granularity, epoch) PK dedupes."""
    if days <= 0:
        raise DerivError("days must be positive")
    if days > 60:
        raise DerivError("cap is 60 days per call — make multiple calls if needed")
    end = int(end_epoch if end_epoch is not None else time.time())
    total = int((days * 86400) // granularity)
    total = min(max(total, 1), 50_000)  # sane safety ceiling

    candles = await fetch_history_paginated(
        symbol=symbol,
        granularity=granularity,
        end_epoch=end,
        total_candles=total,
    )
    inserted = get_store().upsert_candles(
        symbol, granularity, [c.to_dict() for c in candles]
    )
    # Persist the symbol's decimal precision so digit backtests use the right
    # "last digit" (learned from the Deriv response during the fetch above).
    from app.deriv import pip_size_for
    ps = pip_size_for(symbol)
    if ps is not None:
        get_store().set_state(f"pip_size:{symbol}", str(ps))

    first = candles[0].epoch if candles else None
    last = candles[-1].epoch if candles else None
    return {
        "symbol": symbol,
        "granularity": granularity,
        "requested_days": days,
        "candles_fetched": len(candles),
        "candles_new": inserted,
        "candles_duplicate": len(candles) - inserted,
        "first_epoch": first,
        "last_epoch": last,
        "max_per_request": MAX_CANDLES_PER_REQUEST,
    }


def get_candles(
    symbol: str,
    granularity: int,
    *,
    start: int | None = None,
    end: int | None = None,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    return get_store().list_candles(
        symbol, granularity, start=start, end=end, limit=limit
    )


def cache_stats() -> dict[str, Any]:
    return get_store().candle_cache_stats()


async def live_digit_stats(symbol: str, count: int = 1000) -> dict[str, Any]:
    """Empirical last-digit distribution from REAL recent ticks.

    This is the honest input for digit trades: it reads the actual
    settlement ticks (at the symbol's true pip precision) rather than the
    last digit of a 1-minute candle close, which is only a rough proxy.
    Returns counts + frequencies per digit 0-9, plus the most/least
    frequent digit and parity split — exactly what even/odd, over/under
    and matches/differs decisions hinge on."""
    data = await fetch_ticks(symbol, count=count)
    prices = data["prices"]
    pip_size = data["pip_size"]
    scale = 10 ** pip_size
    counts = {str(d): 0 for d in range(10)}
    for p in prices:
        counts[str(int(round(p * scale)) % 10)] += 1
    n = len(prices)
    freqs = {d: (c / n if n else 0.0) for d, c in counts.items()}
    even = sum(counts[str(d)] for d in range(0, 10, 2))
    odd = n - even
    most = max(counts, key=counts.get) if n else None
    least = min(counts, key=counts.get) if n else None
    return {
        "symbol": symbol,
        "ticks_analysed": n,
        "pip_size": pip_size,
        "counts": counts,
        "frequencies": {d: round(f, 4) for d, f in freqs.items()},
        "most_frequent_digit": most,
        "least_frequent_digit": least,
        "even_count": even,
        "odd_count": odd,
        "even_frequency": round(even / n, 4) if n else 0.0,
        "odd_frequency": round(odd / n, 4) if n else 0.0,
        "source": "real_ticks",
    }
