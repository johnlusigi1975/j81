"""Shared math engine — the fast 'calculator' the J81 systems call when they
need numbers in a hurry (the bot/researcher reach it over /calc/*).

Two parts:
  1. safe_eval(expr) — arbitrary arithmetic via simpleeval (sandboxed; NEVER
     Python eval()), with math functions + the trading helpers exposed as names
     so you can type e.g. `ev(0.485, 1.95)` or `kelly(0.52, 1.94)`.
  2. Trading/stats math — EV, break-even win-rate, Kelly stake, risk-of-ruin,
     compound growth, payout/markup, z-score/confidence.

It is HONEST math: e.g. break_even_winrate(1.95) = 51.3%, which exposes Deriv's
house edge (you must win >51.3% just to break even at a 1.95 payout) instead of
hiding it. The calculator computes truth fast; it does not invent an edge.
"""

from __future__ import annotations

import math

from simpleeval import EvalWithCompoundTypes


# ----------------------------------------------------------------- trading math
def ev(win_prob: float, payout: float, stake: float = 1.0) -> float:
    """Expected value of one contract. `payout` is the MULTIPLIER per unit stake
    (e.g. 1.95 → a win returns 1.95× your stake, i.e. +0.95× profit). Win profit =
    stake*(payout-1); a loss costs the stake."""
    p = max(0.0, min(1.0, win_prob))
    return round(p * stake * (payout - 1.0) - (1 - p) * stake, 6)


def break_even_winrate(payout: float, stake: float = 1.0) -> float:
    """Win-rate (0–1) where EV = 0. With payout as a multiplier: p*(payout-1) =
    (1-p) → p = 1/payout (independent of stake)."""
    return round(1.0 / payout, 6) if payout else 1.0


def edge(win_prob: float, payout: float, stake: float = 1.0) -> float:
    """How far the win-rate sits above break-even (negative = losing bet)."""
    return round(win_prob - break_even_winrate(payout, stake), 6)


def kelly(win_prob: float, payout: float, stake: float = 1.0) -> float:
    """Kelly-optimal fraction of bankroll to stake. 0 when there's no edge
    (which, on RNG synthetics, is the usual answer). b = net odds."""
    b = payout - 1.0   # net odds per unit (payout is a multiplier)
    if b <= 0:
        return 0.0
    p = max(0.0, min(1.0, win_prob))
    f = (b * p - (1 - p)) / b
    return round(max(0.0, f), 6)


def risk_of_ruin(win_prob: float, payout: float, units: int) -> float:
    """Approx probability of losing a bankroll of `units` flat stakes, via the
    gambler's-ruin ratio on the per-bet edge. Returns 0–1. With no edge (the RNG
    default) it returns ~1 — losing the bankroll is effectively certain."""
    p = max(1e-9, min(1 - 1e-9, win_prob))
    q = 1 - p
    b = payout - 1.0                      # net profit per 1 staked on a win
    if b <= 0 or (p * b - q) <= 0:        # no positive edge → ruin is ~certain
        return 1.0
    r = q / (p * b)                       # ruin ratio (<1 when there's an edge)
    return round(min(1.0, r ** max(1, int(units))), 6)


def payout_after_markup(base_payout: float, markup_pct: float) -> float:
    """Net payout the client sees after your app markup is taken off the top."""
    return round(base_payout * (1 - max(0.0, markup_pct) / 100.0), 6)


def compound(start: float, rate_pct: float, periods: int) -> float:
    """Compound growth: start × (1 + rate)^periods."""
    return round(start * (1 + rate_pct / 100.0) ** max(0, int(periods)), 6)


def zscore(successes: int, n: int, p0: float = 0.5) -> float:
    """z-score of an observed success count vs a fair baseline p0."""
    if n <= 0:
        return 0.0
    return round((successes - n * p0) / math.sqrt(n * p0 * (1 - p0)), 6)


def confidence(successes: int, n: int, p0: float = 0.5) -> float:
    """0–99 'confidence' = how far the split sits from fair (|z| mapped, z=3→99).
    NOT a win probability — a strength-of-deviation reading on random data."""
    z = abs(zscore(successes, n, p0))
    return round(max(0.0, min(99.0, z / 3.0 * 100.0)), 2)


def trade_summary(stake: float = 1.0, payout: float = 1.95, win_prob: float = 0.5) -> dict:
    """Everything you'd want before a trade, in one shot."""
    be = break_even_winrate(payout, stake)
    return {
        "stake": stake, "payout": payout, "win_prob": win_prob,
        "win_profit": round(stake * (payout - 1.0), 6),
        "expected_value": ev(win_prob, payout, stake),
        "break_even_winrate": be,
        "break_even_pct": round(be * 100, 2),
        "edge": edge(win_prob, payout, stake),
        "edge_pct": round(edge(win_prob, payout, stake) * 100, 2),
        "kelly_fraction": kelly(win_prob, payout, stake),
        "kelly_stake_pct": round(kelly(win_prob, payout, stake) * 100, 2),
        "verdict": ("positive EV" if ev(win_prob, payout, stake) > 0
                    else "negative EV — house edge"),
    }


# ----------------------------------------------------------- safe expression eval
_FUNCS: dict = {
    "abs": abs, "round": round, "min": min, "max": max, "sum": sum, "pow": pow,
    "sqrt": math.sqrt, "log": math.log, "log10": math.log10, "exp": math.exp,
    "floor": math.floor, "ceil": math.ceil, "factorial": math.factorial,
    "gcd": math.gcd, "hypot": math.hypot, "sin": math.sin, "cos": math.cos,
    "tan": math.tan, "atan": math.atan, "degrees": math.degrees, "radians": math.radians,
    # trading helpers, callable from an expression
    "ev": ev, "kelly": kelly, "breakeven": break_even_winrate, "edge": edge,
    "compound": compound, "zscore": zscore, "confidence": confidence,
    "payout_after_markup": payout_after_markup,
}
_NAMES: dict = {"pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf}

MAX_EXPR_LEN = 500


def safe_eval(expr: str, variables: dict | None = None):
    """Evaluate one arithmetic expression safely (no Python eval). Supports the
    math + trading functions above and your own variables. Raises ValueError on
    a bad/forbidden expression."""
    if not expr or len(expr) > MAX_EXPR_LEN:
        raise ValueError("expression missing or too long")
    names = dict(_NAMES)
    if variables:
        for k, v in variables.items():
            if isinstance(v, (int, float)):
                names[str(k)] = v
    try:
        return EvalWithCompoundTypes(functions=_FUNCS, names=names).eval(expr.strip())
    except Exception as exc:  # surface a clean message, never a 500
        raise ValueError(f"could not evaluate: {exc}")
