"""J81 Brain Advisor — turns the shared library into live recommendations.

The library (deriv_lib.py on the analyser) is a stockpile of principles. This
module is the ORCHESTRATOR that, given the current state (scoreboard, account,
balance), pulls the relevant principles and produces a ranked list of
recommended actions — with the section/verse/source cited next to each one.

Rules-based, deterministic, honest. The brain does NOT predict markets; it
applies discipline + risk + library knowledge to whatever state the user is in.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.config import get_settings

# ---- Library cache (the analyser is the authoritative source) ----------------
_LIB_CACHE: dict[str, Any] = {"at": 0.0, "data": None}
_LIB_TTL = 300.0   # 5 min — the static parts barely change; live payouts refresh ~hourly


async def _get_library() -> dict[str, Any]:
    """Fetch /deriv/library from the analyser, cached. Falls back to an empty
    skeleton if the analyser is unreachable so the brain still gives advice."""
    now = time.monotonic()
    if _LIB_CACHE["data"] and (now - _LIB_CACHE["at"]) < _LIB_TTL:
        return _LIB_CACHE["data"]
    url = get_settings().analyser_url.rstrip("/") + "/deriv/library"
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(url); r.raise_for_status()
            _LIB_CACHE["data"] = r.json(); _LIB_CACHE["at"] = now
            return _LIB_CACHE["data"]
    except Exception:
        # Skeleton so the rules engine still works without the live payouts.
        return _LIB_CACHE.get("data") or {
            "live": {}, "j81_goal": {"win_target": {"threshold_pct": 60, "sample_size": 100}},
        }


# ---- The advisor ------------------------------------------------------------
def _rec(action: str, reason: str, principle: str, severity: str = "info",
         verse: str | None = None) -> dict[str, Any]:
    out = {"action": action, "reason": reason, "principle": principle, "severity": severity}
    if verse:
        out["verse_or_source"] = verse
    return out


async def advise(*, account: dict | None, balance: dict | None,
                 scoreboard: dict) -> dict[str, Any]:
    """Given the live state, return a structured list of recommendations.
    Each rec carries the principle that backs it — so the user (or a downstream
    system) can audit the reasoning."""
    lib = await _get_library()
    recs: list[dict[str, Any]] = []
    notes: list[str] = []

    bank = float((balance or {}).get("balance") or 0)
    currency = (balance or {}).get("currency") or "USD"
    is_demo = bool((account or {}).get("is_demo", True))
    state = (scoreboard or {}).get("state", "warming")

    # --- 1. Stake sizing (RISK_MANAGEMENT > Fixed fractional) -----------------
    if bank > 0:
        risk_pct = 0.01 if is_demo else 0.005   # 1% demo, 0.5% real (binary options + RNG)
        stake = round(max(0.35, bank * risk_pct), 2)
        recs.append(_rec(
            f"Set stake to ${stake} per trade ({risk_pct*100:.1f}% of {currency} {bank:.2f})",
            "Fixed-fractional sizing. Binary options lose 100% of stake on loss → keep it small.",
            "RISK_MANAGEMENT → Fixed fractional + binary-options specifics",
            severity="primary",
            verse="Mark Douglas; 1% rule (Investopedia, CFA research)",
        ))
    else:
        recs.append(_rec(
            "Connect an account / wait for balance",
            "Cannot size without bankroll info.",
            "infrastructure", severity="info"))

    # --- 2. Pre-defined exits (TRADING_DISCIPLINE > Predefine exit BEFORE entry) ---
    if bank > 0:
        ll = round(bank * 0.10, 2)
        tp = round(bank * 0.10, 2)
        recs.append(_rec(
            f"Set daily Stop-at-LOSS to ${ll}",
            "10% of bankroll = the discipline standard. Hit it → done for the day.",
            "RISK_MANAGEMENT → Drawdown rules · daily 10–15%",
            verse="Mark Douglas: pre-commit risk before clicking buy"))
        recs.append(_rec(
            f"Set Stop-at-WIN to ${tp}",
            "Locks gains before they revert to the house edge.",
            "TRADING_DISCIPLINE → Predefine exit BEFORE entry"))

    # --- 3. Best-payout market (DERIV_LIBRARY > the one real EV lever) --------
    best = ((lib or {}).get("live") or {}).get("best_even_odd_market")
    if best and best.get("payout_pct"):
        recs.append(_rec(
            f"For Even/Odd, trade {best.get('name') or best.get('symbol')} "
            f"({float(best['payout_pct']):.1f}% payout)",
            "The ONLY documented EV lever on Deriv. Same 50/50 everywhere — higher payout loses less to the house.",
            "DERIV_LIBRARY → trade_types.even_odd.real_ev_edge",
            severity="primary"))
    else:
        notes.append("Best-payout market not yet refreshed — call POST /deriv/library/refresh on the analyser.")

    # --- 4. State-driven recommendation (HUMAN_BRAIN → predictive processing) -
    if state == "winning":
        recs.append(_rec(
            "Stick to the plan — you're beating the bar",
            "Process > outcome. Don't change a winning strategy mid-session.",
            "TRADING_DISCIPLINE → Process over outcome", severity="good"))
    elif state == "below_goal":
        recs.append(_rec(
            "STOP and review — you're below 60% wins AND net negative",
            "Discipline standard: walk away from a losing session. The plan beats the trader.",
            "TRADING_DISCIPLINE → No revenge trading + Daily loss limit",
            severity="danger"))
    elif state == "warming":
        n = scoreboard.get("trades", 0); w = scoreboard.get("window", 100)
        recs.append(_rec(
            f"Building sample — {n}/{w} settled trades so far",
            "Probabilistic thinking: judge over a window, not single trades.",
            "HUMAN_BRAIN → Bayesian updating · evidence-weighted",
            severity="info"))
    elif state == "win_high_pnl_negative":
        recs.append(_rec(
            "Pivot from high-win-rate, low-payout bets (e.g. DIFFERS) to balanced bets",
            "Win-rate is high but you're losing money — payouts are eating you. "
            "Move to Even/Odd on the highest-payout market.",
            "RISK_MANAGEMENT → Edge first, size second",
            severity="warning"))
    elif state == "pnl_positive_winrate_low":
        recs.append(_rec(
            "Profitable but below the 60% bar — keep sample-size discipline",
            "Could be sample-luck. Run more trades before scaling size up.",
            "TRADING_DISCIPLINE → Probabilistic thinking", severity="warning"))

    # --- 5. Hard rules against the dangerous stuff ----------------------------
    recs.append(_rec(
        "Never run a martingale ladder",
        "A 7-loss streak forces a 128× stake just to recover the original. Unbounded streak risk on RNG = blow-up.",
        "RISK_MANAGEMENT → Martingale warning", severity="danger"))

    # --- 6. Scripture-grounded reminder (BIBLE_FINANCIAL_WISDOM) --------------
    recs.append(_rec(
        "Trade only what you can afford to lose",
        "Trading borrowed money turns the trader into the lender's slave.",
        "BIBLE_FINANCIAL_WISDOM → Avoid debt",
        verse="Proverbs 22:7 (GNB)", severity="ground"))
    recs.append(_rec(
        "Count the cost BEFORE you click",
        "Every contract has an EV — check the strip first. The math knows.",
        "BIBLE_FINANCIAL_WISDOM → Count the cost",
        verse="Luke 14:28 (GNB)", severity="ground"))

    # --- 7. Goal summary --------------------------------------------------------
    goal = (lib or {}).get("j81_goal", {}).get("win_target", {})
    target_pct = goal.get("threshold_pct", 60)
    target_n = goal.get("sample_size", 100)
    return {
        "scoreboard_state": state,
        "bankroll": bank, "currency": currency, "is_demo": is_demo,
        "goal": {"target_pct": target_pct, "sample": target_n},
        "best_market": best,
        "recommendations": recs,
        "principles_applied": [r["principle"] for r in recs],
        "notes": notes,
        "honest_note": (
            "These are rules-based recommendations from the J81 library — "
            "they apply discipline + risk + the one real EV lever. The brain "
            "does NOT predict the next tick. On RNG markets, this advice "
            "minimises losses and bias errors; it does not promise profit."
        ),
    }
