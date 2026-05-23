"""Even/Odd analytics — the shared, honest core of the Even/Odd strategy.

HONESTY FIRST (read this): Deriv's Volatility indices are an RNG whose last
digit is ~uniform, so the next tick's parity is *independent* of the past and
Even/Odd pays ~1.9x (a built-in house edge). Nothing here predicts the next
digit or turns the long-run edge positive — claiming otherwise would mislead
clients. What these functions do, genuinely well, is make the bot
**disciplined and transparent**, which is what actually retains clients:

  1. MEASURE the current window precisely (counts, %, per-digit, streaks).
  2. QUANTIFY how unusual the window is vs a fair coin — z-score, chi-square,
     and a Wald-Wolfowitz runs (randomness) test. These are *descriptive*.
  3. FILTER: emit a signal + confidence ONLY on strong, rare conditions, so
     the bot trades seldom. Fewer trades = less total spread bled and lower
     variance (real, EV-neutral protection — not a magic edge).
  4. SELECT: rank the 10 markets so the bot acts where the skew is largest now.
  5. BACKTEST honestly so expected win-rate (~50% minus spread) is visible.

Used by: the analyser (scanner + /decide), the Bot (M Pro), and the Researcher
(over HTTP via /even_odd/*) to study and tune thresholds.
"""

from __future__ import annotations

import math
from typing import Any

# Even/Odd settles ~1.9x on synthetics; used for honest backtest expectancy.
DEFAULT_PAYOUT = 1.92


def last_digits(prices: list[float], pip_size: int) -> list[int]:
    """The last decimal digit of each tick, at the symbol's quoting precision.
    Must use pip_size (decimals) so e.g. 1234.50 -> 0, not 5."""
    scale = 10 ** int(pip_size)
    return [int(round(p * scale)) % 10 for p in prices]


def _parity(digits: list[int]) -> list[int]:
    """1 = even, 0 = odd."""
    return [1 if d % 2 == 0 else 0 for d in digits]


def _norm_two_sided_p(z: float) -> float:
    """Two-sided p-value for a standard-normal z (probability a fair process
    would look at least this extreme). Small p = unusual sample."""
    return math.erfc(abs(z) / math.sqrt(2.0))


def _runs_z(parity: list[int]) -> float | None:
    """Wald-Wolfowitz runs test z. Detects whether evens/odds CLUSTER
    (z<0, streaky) or ALTERNATE (z>0) more than chance. ~0 = looks random."""
    n = len(parity)
    n1 = sum(parity)        # evens
    n2 = n - n1             # odds
    if n1 == 0 or n2 == 0 or n < 2:
        return None
    runs = 1 + sum(1 for i in range(1, n) if parity[i] != parity[i - 1])
    mu = (2.0 * n1 * n2) / n + 1.0
    var = (2.0 * n1 * n2 * (2.0 * n1 * n2 - n)) / (n * n * (n - 1.0))
    if var <= 0:
        return None
    return (runs - mu) / math.sqrt(var)


def _current_streak(parity: list[int]) -> dict[str, Any]:
    """The run in progress at the end of the window."""
    if not parity:
        return {"side": None, "length": 0}
    last = parity[-1]
    length = 1
    for i in range(len(parity) - 2, -1, -1):
        if parity[i] == last:
            length += 1
        else:
            break
    return {"side": "even" if last == 1 else "odd", "length": length}


def _max_streaks(parity: list[int]) -> dict[str, int]:
    even_max = odd_max = cur = 0
    cur_side = None
    for p in parity:
        if p == cur_side:
            cur += 1
        else:
            cur_side, cur = p, 1
        if p == 1:
            even_max = max(even_max, cur)
        else:
            odd_max = max(odd_max, cur)
    return {"even": even_max, "odd": odd_max}


def _entropy(digits: list[int]) -> float:
    """Shannon entropy of the 0-9 distribution, normalised 0..1 (1 = perfectly
    uniform/random-looking)."""
    n = len(digits)
    if n == 0:
        return 0.0
    h = 0.0
    for d in range(10):
        c = digits.count(d)
        if c:
            p = c / n
            h -= p * math.log2(p)
    return round(h / math.log2(10), 4)


def even_odd_stats(digits: list[int]) -> dict[str, Any]:
    """Full descriptive picture of a digit window."""
    n = len(digits)
    if n == 0:
        return {"samples": 0, "ready": False}
    parity = _parity(digits)
    even = sum(parity)
    odd = n - even
    even_pct = 100.0 * even / n
    # z of the even count vs a fair coin: (2*even - n)/sqrt(n).
    z_even = (2.0 * even - n) / math.sqrt(n)
    runs_z = _runs_z(parity)
    return {
        "samples": n,
        "ready": True,
        "even": even,
        "odd": odd,
        "even_pct": round(even_pct, 2),
        "odd_pct": round(100.0 - even_pct, 2),
        "gap_pct": round(abs(2 * even_pct - 100.0), 2),
        "digit_freq": {str(d): round(100.0 * digits.count(d) / n, 1) for d in range(10)},
        "z_even": round(z_even, 3),                 # + = too many evens, - = too many odds
        "p_value": round(_norm_two_sided_p(z_even), 4),  # how unusual the even/odd split is
        "runs_z": round(runs_z, 3) if runs_z is not None else None,
        "runs_p": round(_norm_two_sided_p(runs_z), 4) if runs_z is not None else None,
        "current_streak": _current_streak(parity),
        "max_streak": _max_streaks(parity),
        "entropy": _entropy(digits),                # ~1.0 = looks fully random
    }


def even_odd_signal(
    digits: list[int],
    *,
    strategy: str = "reversion",
    z_enter: float = 2.0,
    streak_enter: int = 6,
    min_samples: int = 60,
    z_cap: float = 4.0,
) -> dict[str, Any]:
    """Turn a window into a gated trade signal.

    Strategies (all EV-neutral on a true RNG — they FILTER, they don't beat
    the RNG; the win comes from discipline + risk control + market selection):
      * 'reversion'   — only act when the even/odd split is statistically
                        extreme (|z| >= z_enter); lean to the under-represented
                        side. Rare by design.
      * 'streak'      — act when an in-progress run is long (>= streak_enter);
                        lean against the streak.
      * 'momentum'    — opposite of reversion: lean WITH the heavier side.
    Returns {call: 'even'|'odd'|None, confidence: 0..1, reasons, stats}.
    Confidence is a *strength-of-condition* score, NOT a probability of winning.
    """
    stats = even_odd_stats(digits)
    out: dict[str, Any] = {"call": None, "confidence": 0.0, "reasons": [], "stats": stats}
    if not stats.get("ready") or stats["samples"] < min_samples:
        out["reasons"].append(f"need {min_samples}+ ticks (have {stats.get('samples', 0)})")
        return out

    z = stats["z_even"]
    if strategy in ("reversion", "momentum"):
        if abs(z) < z_enter:
            out["reasons"].append(f"split not extreme enough (|z|={abs(z):.2f} < {z_enter})")
            return out
        heavy = "even" if z > 0 else "odd"
        light = "odd" if z > 0 else "even"
        out["call"] = light if strategy == "reversion" else heavy
        out["confidence"] = round(min(1.0, abs(z) / z_cap), 3)
        out["reasons"].append(
            f"{strategy}: even/odd split is {stats['gap_pct']}% off fair (z={z:+.2f}, "
            f"p={stats['p_value']}); leaning {out['call']}"
        )
    elif strategy == "streak":
        cs = stats["current_streak"]
        if cs["length"] < streak_enter:
            out["reasons"].append(f"streak too short ({cs['length']} < {streak_enter})")
            return out
        out["call"] = "odd" if cs["side"] == "even" else "even"
        out["confidence"] = round(min(1.0, cs["length"] / (streak_enter * 2.0)), 3)
        out["reasons"].append(
            f"streak: {cs['length']} {cs['side']} in a row; leaning {out['call']}"
        )
    else:
        out["reasons"].append(f"unknown strategy '{strategy}'")
    return out


def backtest(
    digits: list[int],
    *,
    payout: float = DEFAULT_PAYOUT,
    window: int = 120,
    stake: float = 1.0,
    **cfg: Any,
) -> dict[str, Any]:
    """Honest walk-forward backtest: at each step, build the signal from the
    PRIOR `window` ticks and (if it fires) bet on the NEXT tick's parity.
    Reports the real win-rate and expectancy so nobody is fooled — on a fair
    RNG this lands near 50% and a slightly negative expectancy (the spread).
    """
    trades = wins = 0
    pnl = 0.0
    peak = 0.0
    max_dd = 0.0
    for i in range(window, len(digits)):
        sig = even_odd_signal(digits[i - window:i], **cfg)
        call = sig.get("call")
        if not call:
            continue
        trades += 1
        actual = "even" if digits[i] % 2 == 0 else "odd"
        if call == actual:
            wins += 1
            pnl += stake * (payout - 1.0)
        else:
            pnl -= stake
        peak = max(peak, pnl)
        max_dd = min(max_dd, pnl - peak)
    win_rate = (wins / trades) if trades else 0.0
    return {
        "trades": trades,
        "wins": wins,
        "win_rate": round(win_rate, 4),
        "net": round(pnl, 2),
        "expectancy_per_trade": round(pnl / trades, 4) if trades else 0.0,
        "max_drawdown": round(max_dd, 2),
        "breakeven_win_rate": round(1.0 / payout, 4),  # win-rate you'd NEED to not lose
        "payout": payout,
        "note": "Synthetics are an RNG; ~50% is expected. Edge comes from "
                "discipline + risk control, not prediction.",
    }
