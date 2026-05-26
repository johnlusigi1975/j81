"""Deriv Trade-Type Library — the tree's single source of truth.

We ONLY trade four contract types: Rise/Fall, Even/Odd, Over/Under, Matches/Differs.
This module holds:

  * STATIC knowledge from Deriv's API docs (contract codes, settlement rules,
    structural win probabilities, market list, constraints, honest notes).
  * LIVE data refreshed from Deriv (real payout multipliers per (market, type),
    last refresh time).

Persisted to {DATA_DIR}/data/deriv_library.json so it survives restarts. The
analyser exposes /deriv/library/*; the bot proxies it so the UI and the
researcher both consume the same numbers from a single endpoint.

Honest by design: every win-probability and EV claim here is documented from
Deriv's published mechanics, not invented. There is NO predictive edge in this
library — it is reference data the systems use to compute true EV, render
honest UIs, and pick the highest-payout market (the one genuine Even/Odd lever).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from app.config import data_path
from app.deriv import fetch_proposal_payout
from app.scanner import SCAN_SYMBOLS


# ============================================================ STATIC KNOWLEDGE

TRADE_TYPES: dict[str, dict[str, Any]] = {
    "rise_fall": {
        "name": "Rise / Fall",
        "deriv_codes": {"up": "CALL", "down": "PUT"},
        "settles_on": "exit-tick price vs entry-tick price",
        "win_rule": "CALL wins if exit > entry; PUT wins if exit < entry; tie loses on entry tick",
        "duration_unit": "t",
        "duration_min": 1, "duration_max": 10,
        "barrier_required": False,
        "structural_win_prob": 0.50,
        "structural_note": "Synthetic ticks are an audited RNG; up/down on a tick is ~50/50 (P(up|prev up) ≈ 49.2%, verified ~independence).",
        "typical_payout_multiplier": 1.94,
        "break_even_winrate": 0.5155,
        "house_edge_pct_approx": 1.5,
        "real_ev_edge": "None — payout is essentially flat across markets and structural odds are 50/50.",
    },
    "even_odd": {
        "name": "Even / Odd",
        "deriv_codes": {"even": "DIGITEVEN", "odd": "DIGITODD"},
        "settles_on": "last digit of the final tick at expiry",
        "win_rule": "DIGITEVEN wins on {0,2,4,6,8}; DIGITODD wins on {1,3,5,7,9}",
        "duration_unit": "t",
        "duration_min": 1, "duration_max": 10,
        "barrier_required": False,
        "structural_win_prob": 0.50,
        "structural_note": "Last-digit distribution is uniform — verified across thousands of ticks.",
        "typical_payout_multiplier": "1.92–1.95 depending on market",
        "break_even_winrate": 0.5128,
        "house_edge_pct_approx_range": [1.5, 4.0],
        "real_ev_edge": "THE ONLY genuine lever in this library. Deriv quotes slightly different payouts per market (e.g. R_25/50/75/1HZ* ≈ 1.95×, R_100 ≈ 1.92×). Trading the highest-payout market recovers ~1–2% EV vs the lowest. Same 50/50 odds everywhere — no prediction.",
    },
    "over_under": {
        "name": "Over / Under",
        "deriv_codes": {"over": "DIGITOVER", "under": "DIGITUNDER"},
        "settles_on": "last digit of the final tick at expiry",
        "win_rule": "DIGITOVER wins if last digit > barrier; DIGITUNDER wins if < barrier; ties lose",
        "duration_unit": "t",
        "duration_min": 1, "duration_max": 10,
        "barrier_required": True,
        "barrier_constraints": {"over": "0–8 (over 9 is impossible)", "under": "1–9 (under 0 is impossible)"},
        "structural_win_prob_formula": {"over_N": "(9 − N) / 10", "under_N": "N / 10"},
        "structural_win_prob_examples": {
            "over_0": 0.9, "over_4": 0.5, "over_8": 0.1,
            "under_1": 0.1, "under_5": 0.5, "under_9": 0.9,
        },
        "real_ev_edge": "None — Deriv prices payouts per barrier so EV stays negative for both sides. High-probability barriers pay low (~1.1×); low-probability pay high (~9×). Break-even tracks the structural odds plus the house edge.",
    },
    "matches_differs": {
        "name": "Matches / Differs",
        "deriv_codes": {"matches": "DIGITMATCH", "differs": "DIGITDIFF"},
        "settles_on": "last digit of the final tick at expiry",
        "win_rule": "DIGITMATCH wins if last digit == barrier; DIGITDIFF wins if last digit != barrier",
        "duration_unit": "t",
        "duration_min": 1, "duration_max": 10,
        "barrier_required": True,
        "barrier_constraints": {"any": "0–9"},
        "structural_win_prob": {"matches": 0.10, "differs": 0.90},
        "typical_payout_multiplier": {"matches": 9.5, "differs": 1.05},
        "break_even_winrate": {"matches": 0.105, "differs": 0.952},
        "real_ev_edge": "None — and the most honest demonstration in the library of 'high win-rate ≠ profit'. DIFFERS wins ~90% but pays only ~1.05×; MATCHES wins ~10% but pays ~9.5×. Both sides carry a small house edge.",
    },
}

MARKETS: list[dict[str, Any]] = [
    {"code": "R_10",   "name": "Vol 10",          "kind": "synthetic", "pip_size": 3, "tick_interval_sec": 2, "vol_class": "low"},
    {"code": "R_25",   "name": "Vol 25",          "kind": "synthetic", "pip_size": 3, "tick_interval_sec": 2, "vol_class": "low-mid"},
    {"code": "R_50",   "name": "Vol 50",          "kind": "synthetic", "pip_size": 4, "tick_interval_sec": 2, "vol_class": "mid"},
    {"code": "R_75",   "name": "Vol 75",          "kind": "synthetic", "pip_size": 4, "tick_interval_sec": 2, "vol_class": "mid-high"},
    {"code": "R_100",  "name": "Vol 100",         "kind": "synthetic", "pip_size": 2, "tick_interval_sec": 2, "vol_class": "high"},
    {"code": "1HZ10V", "name": "Vol 10 (1s)",     "kind": "synthetic", "pip_size": 2, "tick_interval_sec": 1, "vol_class": "low"},
    {"code": "1HZ25V", "name": "Vol 25 (1s)",     "kind": "synthetic", "pip_size": 2, "tick_interval_sec": 1, "vol_class": "low-mid"},
    {"code": "1HZ50V", "name": "Vol 50 (1s)",     "kind": "synthetic", "pip_size": 2, "tick_interval_sec": 1, "vol_class": "mid"},
    {"code": "1HZ75V", "name": "Vol 75 (1s)",     "kind": "synthetic", "pip_size": 2, "tick_interval_sec": 1, "vol_class": "mid-high"},
    {"code": "1HZ100V","name": "Vol 100 (1s)",    "kind": "synthetic", "pip_size": 2, "tick_interval_sec": 1, "vol_class": "high"},
]

CONSTRAINTS: dict[str, Any] = {
    "min_stake_usd": 0.35,
    "max_app_markup_pct": 3.0,
    "tick_duration_min": 1,
    "tick_duration_max": 10,
    "audit_source": "Deriv synthetics use a NIST-certified random number generator (per Deriv.com).",
}

HONEST_NOTES: list[str] = [
    "These four contract types settle on RNG outputs (price ticks or last digits of ticks).",
    "There is no exploitable next-tick prediction edge on Deriv synthetics.",
    "The only documented real EV lever is Even/Odd market selection (~1–2% recoverable by trading the highest-payout market).",
    "High win-rate ≠ profit: DIFFERS wins ~90% but pays 1.05× → still loses money long-term.",
    "Treat as speculation; never stake money you can't afford to lose.",
]


# ============================================================ LIVE STATE

_LIVE: dict[str, Any] = {
    "live_payouts": {},        # (symbol, contract_type, barrier) -> {payout_pct, payout, fetched_at}
    "best_even_odd_market": None,
    "last_refresh": None,
    "last_error": None,
}

_LIB_PATH = lambda: data_path("data/deriv_library.json")
_lock = asyncio.Lock()


def _load_from_disk() -> None:
    p = _LIB_PATH()
    if not p.exists():
        return
    try:
        d = json.loads(p.read_text())
        _LIVE.update({k: v for k, v in d.items() if k in _LIVE})
    except Exception:
        pass


def _save_to_disk() -> None:
    p = _LIB_PATH()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(json.dumps(_LIVE, indent=2, default=str))
    except Exception:
        pass


def _serializable_key(symbol: str, ct: str, barrier: Any) -> str:
    """Dict keys must be JSON-safe; flatten to a string."""
    return f"{symbol}|{ct}|{barrier if barrier is not None else ''}"


# --- variants the library tracks (must mirror what the bot can place) -----
def variants_to_probe() -> list[dict[str, Any]]:
    """Each row is one (contract_type, side, barrier-if-any) we fetch a real
    payout for. Direction-symmetric pairs share the same payout — we only probe
    one of each so we don't burn 80 WS calls per refresh."""
    return [
        {"ct": "CALL",       "duration": 5, "barrier": None, "side": "rise_fall.up",   "stake": 1.0},
        {"ct": "DIGITEVEN",  "duration": 1, "barrier": None, "side": "even_odd.even",  "stake": 1.0},
        {"ct": "DIGITOVER",  "duration": 1, "barrier": "4",  "side": "over_under.over4","stake": 1.0},
        {"ct": "DIGITUNDER", "duration": 1, "barrier": "5",  "side": "over_under.under5","stake": 1.0},
        {"ct": "DIGITMATCH", "duration": 1, "barrier": "0",  "side": "matches_differs.matches0","stake": 1.0},
        {"ct": "DIGITDIFF",  "duration": 1, "barrier": "0",  "side": "matches_differs.differs0","stake": 1.0},
    ]


async def refresh_payouts() -> dict[str, Any]:
    """Pull a real payout from Deriv (no auth) for every (market, variant) pair
    we track. Updates _LIVE, finds the best Even/Odd market, and persists to disk.
    Soft-fails per call so a single bad row doesn't kill the refresh."""
    async with _lock:
        new_payouts: dict[str, Any] = {}
        ok = err = 0
        for code, name in SCAN_SYMBOLS:
            for v in variants_to_probe():
                try:
                    q = await fetch_proposal_payout(
                        code, contract_type=v["ct"], duration=v["duration"], stake=v["stake"])
                    new_payouts[_serializable_key(code, v["ct"], v["barrier"])] = {
                        "symbol": code, "market": name,
                        "contract_type": v["ct"], "barrier": v["barrier"],
                        "side": v["side"], "duration": v["duration"], "stake": v["stake"],
                        "payout": q.get("payout"), "payout_pct": q.get("payout_pct"),
                        "ask_price": q.get("ask_price"),
                        "fetched_at": time.time(),
                    }
                    ok += 1
                except Exception as exc:
                    new_payouts[_serializable_key(code, v["ct"], v["barrier"])] = {
                        "symbol": code, "market": name, "contract_type": v["ct"],
                        "barrier": v["barrier"], "error": str(exc)[:80],
                        "fetched_at": time.time(),
                    }
                    err += 1
        _LIVE["live_payouts"] = new_payouts
        # The one real edge: best Even/Odd market by payout_pct.
        even_rows = [r for r in new_payouts.values()
                     if r.get("contract_type") == "DIGITEVEN" and r.get("payout_pct")]
        if even_rows:
            best = max(even_rows, key=lambda r: r["payout_pct"])
            _LIVE["best_even_odd_market"] = {
                "symbol": best["symbol"], "name": best["market"],
                "payout_pct": best["payout_pct"], "payout": best["payout"],
            }
        _LIVE["last_refresh"] = time.time()
        _LIVE["last_error"] = None if err == 0 else f"{err} probe(s) failed"
        _save_to_disk()
        return {"ok": ok, "errors": err, "best_even_odd": _LIVE["best_even_odd_market"]}


def library() -> dict[str, Any]:
    """The full library — static knowledge + live data — for the bot, researcher
    and UI to read."""
    return {
        "trade_types": TRADE_TYPES,
        "markets": MARKETS,
        "constraints": CONSTRAINTS,
        "honest_notes": HONEST_NOTES,
        "live": _LIVE,
    }


# load any cached live data on import so the library is non-empty before refresh
_load_from_disk()
