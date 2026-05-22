"""The brain — decision engine.

For a given symbol (and optionally trade_type), the engine:

  1. Gathers every strategy in the local DB whose last backtest survived
     (win_rate >= 0.55, total_pnl > 0, enough trades). Ignores untested
     or rejected ones — they vote with zero weight.

  2. Reads the latest cached candles for that symbol/granularity and
     computes a current "market context" (price, recent volatility,
     last-N digit distribution for digit trades).

  3. For each surviving strategy, evaluates its entry expression against
     the very last bar. If it fires, the strategy "votes" with weight =
     backtest_win_rate * strategy.confidence.

  4. Aggregates the votes. The strongest single vote becomes the decision.
     If no votes / total vote weight below a confidence floor, it emits
     a "no_trade" decision — that's a feature, not a bug. The brain
     should be honest about uncertainty.

This is intentionally a v1: rule-driven aggregation, no Bayesian magic.
Confidence is a derived score, calibrated by backtest history. Future
Phase 2.4 can swap the aggregator for something fancier without changing
the contract.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from app.backtest import _last_digit
from app.expressions import ExpressionError, evaluate_series
from app.indicators import compute as compute_indicator
from app.market_data import get_candles
from app.store import get_store

# Confidence threshold below which we refuse to call it a trade. One global
# floor was too blunt — digit games are ~50/50 so they need a bigger edge
# before we act. Per-trade-type floors, with NO_TRADE_FLOOR as the default.
NO_TRADE_FLOOR = 0.55
NO_TRADE_FLOORS = {
    "rise_fall": 0.55,
    "higher_lower": 0.55,
    "even_odd": 0.60,
    "over_under": 0.60,
    "matches_differs": 0.62,
}
DECISION_TTL_SECONDS = 300  # decision is "fresh" for 5 minutes


def _floor_for(trade_type: str | None) -> float:
    return NO_TRADE_FLOORS.get((trade_type or "").lower(), NO_TRADE_FLOOR)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _market_context(candles: list[dict], lookback: int = 50, pip_size: int = 2) -> dict[str, Any]:
    """Snapshot of the current market state — useful for decision rationale
    and for the brain to inspect what conditions it traded in."""
    if not candles:
        return {}
    last = candles[-1]
    window = candles[-lookback:]
    closes = [c["close"] for c in window]
    digits = [_last_digit(c["close"], pip_size) for c in window]
    vol = statistics.pstdev(closes) if len(closes) > 1 else 0.0
    digit_dist: dict[str, int] = {str(d): 0 for d in range(10)}
    for d in digits:
        digit_dist[str(d)] += 1
    return {
        "last_epoch": last["epoch"],
        "last_close": last["close"],
        "last_digit": _last_digit(last["close"], pip_size),
        "lookback_bars": len(window),
        "volatility_stdev": round(vol, 4),
        "digit_distribution_last_N": digit_dist,
    }


def _evaluate_strategy_at_last_bar(
    payload: dict, candles: list[dict]
) -> tuple[bool, dict[str, Any]]:
    """Returns (fired_on_last_bar, diagnostics)."""
    rules = payload.get("rules") or {}
    entry = rules.get("entry")
    if not entry:
        return False, {"reason": "no entry expression"}
    indicator_values: dict[str, Any] = {}
    for ind in payload.get("indicators") or []:
        ref = ind.get("ref") or ind.get("type", "").lower()
        try:
            indicator_values[ref] = compute_indicator(
                candles, ind.get("type", ""), ind.get("params") or {}
            )
        except ValueError as exc:
            return False, {"reason": f"indicator {ref!r} failed: {exc}"}
    try:
        signals = evaluate_series(entry, candles, indicator_values)
    except ExpressionError as exc:
        return False, {"reason": f"expression error: {exc}"}
    fired = bool(signals[-1]) if signals else False
    return fired, {"entry_expression": entry, "fired_at_last_bar": fired}


def decide(
    symbol: str,
    *,
    trade_type: str | None = None,
    granularity: int = 60,
    min_strategy_winrate: float = 0.55,
) -> dict[str, Any]:
    """Produce one decision for `symbol`. Always returns a dict — either
    `is_trade=True` with confidence/direction/etc., or `is_trade=False`
    with a reason."""
    store = get_store()
    candles = get_candles(symbol=symbol, granularity=granularity, limit=500)
    if len(candles) < 30:
        return {
            "symbol": symbol,
            "is_trade": False,
            "confidence": 0.0,
            "rationale": f"not enough cached candles for {symbol} "
                         f"(have {len(candles)}, need >=30) — pull more via /data/fetch",
            "market_context": {},
            "contributing_strategies": [],
        }

    try:
        pip_size = int(store.get_state(f"pip_size:{symbol}") or 2)
    except (TypeError, ValueError):
        pip_size = 2
    context = _market_context(candles, pip_size=pip_size)

    from app.config import PRIORITY_TRADE_TYPES
    priority_on = store.get_state("priority_mode") == "1"

    backtests = store.latest_backtest_per_strategy()
    strategies = store.list_strategies_raw()
    candidates = []
    for s in strategies:
        if trade_type and s["trade_type"] != trade_type:
            continue
        # Priority mode: only let the two simple trade types vote, so the brain
        # masters them first before expanding to the rest.
        if priority_on and s["trade_type"] not in PRIORITY_TRADE_TYPES:
            continue
        bt = backtests.get(s["id"])
        if not bt:
            continue
        if bt["status"] != "survived":
            continue
        if (bt["win_rate"] or 0) < min_strategy_winrate:
            continue
        payload = json.loads(s["payload"]) if isinstance(s["payload"], str) else s["payload"]
        fired, diag = _evaluate_strategy_at_last_bar(payload, candles)
        if not fired:
            continue
        weight = (bt["win_rate"] or 0) * float(payload.get("confidence", 0.5))
        candidates.append({
            "strategy_id": s["id"],
            "strategy_name": s["name"],
            "trade_type": payload.get("trade_type"),
            "rules": payload.get("rules"),
            "weight": round(weight, 4),
            "win_rate": bt["win_rate"],
            "diag": diag,
        })

    if not candidates:
        rationale = (
            "no surviving strategy fired on the last bar"
            if not any(bt["status"] == "survived" for bt in backtests.values())
            else "no backtest-validated strategies in the DB yet"
        )
        decision = {
            "symbol": symbol,
            "trade_type": trade_type,
            "is_trade": False,
            "confidence": 0.0,
            "rationale": rationale,
            "market_context": context,
            "contributing_strategies": [],
        }
        store.record_decision(decision)
        return decision

    # ENSEMBLE: group candidates that agree on the SAME call (trade_type +
    # direction + prediction) and combine their confidence with a probabilistic
    # OR (1 - ∏(1-wᵢ)). Several agreeing strategies clear the floor that one
    # alone couldn't — which is what was leaving everything as "no trade".
    def _signal_key(c: dict) -> tuple:
        r = c["rules"] or {}
        return (
            c["trade_type"],
            (r.get("direction") or "").lower(),
            str(r.get("prediction")),
        )

    def _combined(members: list[dict]) -> float:
        prod = 1.0
        for m in members:
            prod *= 1.0 - min(max(float(m["weight"]), 0.0), 0.99)
        return 1.0 - prod

    groups: dict[tuple, list[dict]] = {}
    for c in candidates:
        groups.setdefault(_signal_key(c), []).append(c)

    best_key = max(groups, key=lambda k: _combined(groups[k]))
    members = groups[best_key]
    members.sort(key=lambda c: c["weight"], reverse=True)
    top = members[0]
    rules = top["rules"] or {}
    confidence = round(_combined(members), 4)
    floor = _floor_for(top["trade_type"])
    is_trade = confidence >= floor
    agree = len(members)

    decision = {
        "symbol": symbol,
        "trade_type": top["trade_type"],
        "direction": rules.get("direction"),
        "prediction": rules.get("prediction"),
        "duration": rules.get("duration"),
        "duration_unit": rules.get("duration_unit"),
        "confidence": confidence,
        "is_trade": is_trade,
        "agree_count": agree,
        "rationale": (
            f"{agree} strateg{'ies' if agree > 1 else 'y'} agree "
            f"(lead '{top['strategy_name']}', win_rate {top['win_rate']:.0%}); "
            f"ensemble confidence {confidence:.0%} "
            f"{'≥' if is_trade else '<'} {floor:.0%} floor — "
            f"{'TRADE' if is_trade else 'NO TRADE'}"
        ),
        "contributing_strategies": [
            {"id": c["strategy_id"], "name": c["strategy_name"], "weight": c["weight"]}
            for c in members[:5]
        ],
        "market_context": context,
    }
    store.record_decision(decision)
    return decision


def research_gaps() -> list[dict[str, Any]]:
    """Inspect what's been gathered + tested and surface concrete asks the
    Researcher should go fill. The Analyser uses these to drive its
    feedback channel back to the Researcher."""
    store = get_store()
    stats = store.stats()
    by_tt = stats["strategies"]["by_trade_type"]
    backtests = store.latest_backtest_per_strategy()

    gaps: list[dict] = []

    # Gap 1: trade types we have NO strategies for at all.
    for tt in ("rise_fall", "even_odd", "over_under", "matches_differs", "higher_lower"):
        if tt not in by_tt:
            gaps.append({
                "trade_type": tt,
                "kind": "missing_trade_type",
                "query": f"deriv {tt.replace('_', ' ')} strategy with explicit indicator rules",
                "why": f"the Analyser has zero strategies for {tt}",
                "priority": "high",
            })

    # Gap 2: trade types where every tested strategy failed.
    by_tt_status: dict[str, dict[str, int]] = {}
    for s_id, bt in backtests.items():
        tt = (bt.get("strategy_id") and store.list_strategies_raw())
    # Simpler: walk strategies + their backtests directly
    strategies_now = store.list_strategies_raw()
    grouped: dict[str, list[str]] = {}
    for s in strategies_now:
        grouped.setdefault(s["trade_type"], []).append(s["id"])
    for tt, ids in grouped.items():
        ids_tested = [i for i in ids if i in backtests]
        if not ids_tested:
            continue
        survivors = [i for i in ids_tested if backtests[i]["status"] == "survived"]
        if not survivors and len(ids_tested) >= 2:
            gaps.append({
                "trade_type": tt,
                "kind": "all_strategies_rejected",
                "query": f"deriv {tt.replace('_', ' ')} strategy that actually works synthetic indices",
                "why": (
                    f"{len(ids_tested)} {tt} strategies tested, 0 survived. "
                    "Need better entry rules."
                ),
                "priority": "high",
            })

    # Gap 3: low backtest win rate across the survivors — request stronger rules.
    survivor_rates = [bt["win_rate"] for bt in backtests.values()
                      if bt["status"] == "survived" and bt["win_rate"] is not None]
    if survivor_rates and max(survivor_rates) < 0.65:
        gaps.append({
            "trade_type": None,
            "kind": "low_winrate",
            "query": "deriv high accuracy strategy filter market regime volatility",
            "why": (
                f"best surviving strategy is only "
                f"{max(survivor_rates):.0%} win rate. Want >=65%."
            ),
            "priority": "medium",
        })

    return gaps
