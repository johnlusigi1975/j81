"""Backtest replay engine.

For each strategy:
  1. Load its rules + indicators from the stored payload (the Researcher's
     Pydantic Strategy shape).
  2. Pull cached candles for the target symbol/granularity from /data.
  3. Compute the requested indicators once over the whole series.
  4. Evaluate the entry expression at every bar (safe sandbox; never eval()).
  5. On each True bar, simulate a Deriv contract for the strategy's trade_type
     and duration, look up the outcome `duration` bars later, score it.
  6. Aggregate trades into win-rate, total P&L, profit factor.

Trade-type semantics implemented in this Phase 2.3 cut:
  * rise_fall: win if close moves in the predicted direction by exit bar.
  * even_odd / over_under / matches_differs: win if the last digit of the
    exit-bar close satisfies the prediction.
  * higher_lower: same as rise_fall for our purposes (price direction).
  * touch_no_touch / asian: deferred to Phase 2.4 — too contract-mechanic-
    specific to simulate accurately from candles alone.

Deriv payout assumption: stake 1.0, payout 0.95 on a win (typical for
synthetic indices). The backtest is comparative, not exact P&L — used
for *picking strategies that work*, not predicting next month's revenue.
"""

from __future__ import annotations

import json
from typing import Any

from app.expressions import ExpressionError, evaluate_series
from app.indicators import compute as compute_indicator
from app.market_data import get_candles

# Deriv synthetic indices payout. Adjust later per-symbol if needed.
WIN_PAYOUT = 0.95
LOSS_PAYOUT = -1.0


def _last_digit(price: float, pip_size: int = 2) -> int:
    """Deriv's "last digit of the price" at the symbol's own precision.
    `pip_size` is the symbol's decimal places (R_100 = 2; others differ).
    Using the wrong precision scores the wrong digit — the root cause of
    digit strategies never surviving."""
    return int(round(price * (10 ** pip_size))) % 10


def _simulate_contract(
    entry: dict, exit_: dict, trade_type: str, rules: dict, pip_size: int = 2
) -> float | None:
    """Return PnL for one trade. None means the trade-type isn't supported
    by the candle-based engine yet — those bars are skipped."""
    t = (trade_type or "").lower()
    direction = (rules.get("direction") or "").lower() or None
    prediction = rules.get("prediction")
    exit_close = exit_["close"]
    entry_close = entry["close"]

    if t in ("rise_fall", "higher_lower"):
        if direction not in ("up", "down"):
            return None
        moved_up = exit_close > entry_close
        won = (direction == "up" and moved_up) or (direction == "down" and not moved_up)
        return WIN_PAYOUT if won else LOSS_PAYOUT

    if t == "even_odd":
        if prediction not in ("even", "odd", 0, 1, "0", "1"):
            return None
        target_even = prediction in ("even", 0, "0")
        d = _last_digit(exit_close, pip_size)
        won = (target_even and d % 2 == 0) or (not target_even and d % 2 == 1)
        return WIN_PAYOUT if won else LOSS_PAYOUT

    if t == "over_under":
        if prediction is None:
            return None
        try:
            barrier = int(prediction)
        except (TypeError, ValueError):
            return None
        # Direction here means whether we're betting OVER or UNDER the barrier.
        d = _last_digit(exit_close, pip_size)
        if direction == "over":
            won = d > barrier
        elif direction == "under":
            won = d < barrier
        else:
            return None
        return WIN_PAYOUT if won else LOSS_PAYOUT

    if t == "matches_differs":
        if prediction is None:
            return None
        try:
            target = int(prediction)
        except (TypeError, ValueError):
            return None
        d = _last_digit(exit_close, pip_size)
        # direction is "matches" or "differs"
        matched = d == target
        if direction == "matches":
            won = matched
        elif direction == "differs":
            won = not matched
        else:
            return None
        return WIN_PAYOUT if won else LOSS_PAYOUT

    return None  # unsupported trade type for the candle backtest


def backtest_strategy(
    strategy_payload: dict,
    *,
    symbol: str | None = None,
    granularity: int = 60,
    min_trades: int = 5,
) -> dict[str, Any]:
    """Backtest one strategy. `strategy_payload` is the JSON blob the
    Analyser stored under strategies.payload — i.e. the Researcher's
    full Strategy object as a dict."""
    rules = strategy_payload.get("rules") or {}
    trade_type = (strategy_payload.get("trade_type") or "").lower()
    entry_expr = rules.get("entry")
    if not entry_expr:
        return {"error": "strategy has no entry expression"}

    # Pick a symbol: explicit override > strategy.symbols[0] > R_100 default.
    sym_list = strategy_payload.get("symbols") or []
    chosen_symbol = symbol or (sym_list[0] if sym_list else "R_100")
    # Symbol decimal precision (learned during candle fetches); digit contracts
    # settle on the last digit at THIS precision. Default 2 if unknown.
    from app.store import get_store
    try:
        pip_size = int(get_store().get_state(f"pip_size:{chosen_symbol}") or 2)
    except (TypeError, ValueError):
        pip_size = 2
    candles = get_candles(symbol=chosen_symbol, granularity=granularity, limit=50000)
    if len(candles) < 30:
        return {
            "error": f"not enough cached candles for {chosen_symbol} @ {granularity}s "
            f"(have {len(candles)}, need >=30) — pull more via /data/fetch",
            "symbol": chosen_symbol,
            "granularity": granularity,
        }

    # Compute indicators declared on the strategy.
    indicator_values: dict[str, Any] = {}
    indicator_errors: list[str] = []
    for ind in strategy_payload.get("indicators") or []:
        ref = ind.get("ref") or ind.get("type", "").lower()
        ind_type = ind.get("type")
        if not ind_type or not ref:
            continue
        try:
            indicator_values[ref] = compute_indicator(
                candles, ind_type, ind.get("params") or {}
            )
        except ValueError as exc:
            indicator_errors.append(f"{ref}({ind_type}): {exc}")

    # Evaluate the entry expression at every bar (safe sandbox).
    try:
        signals = evaluate_series(entry_expr, candles, indicator_values)
    except ExpressionError as exc:
        return {"error": f"bad entry expression: {exc}"}

    # Walk signals -> simulated trades.
    duration = max(1, int(rules.get("duration", 5)))
    trades: list[dict] = []
    skipped = 0
    for i, fire in enumerate(signals):
        if not fire:
            continue
        exit_idx = i + duration
        if exit_idx >= len(candles):
            break
        pnl = _simulate_contract(candles[i], candles[exit_idx], trade_type, rules, pip_size)
        if pnl is None:
            skipped += 1
            continue
        trades.append({
            "entry_epoch": candles[i]["epoch"],
            "exit_epoch": candles[exit_idx]["epoch"],
            "pnl": pnl,
        })

    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] < 0)
    total = wins + losses
    win_rate = wins / total if total else 0.0
    total_pnl = sum(t["pnl"] for t in trades)
    gross_win = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    profit_factor = (gross_win / gross_loss) if gross_loss else (float("inf") if gross_win else 0.0)

    enough_trades = total >= min_trades
    status = (
        "survived" if enough_trades and win_rate >= 0.55 and total_pnl > 0
        else "rejected" if enough_trades
        else "inconclusive"
    )

    return {
        "symbol": chosen_symbol,
        "granularity": granularity,
        "trade_type": trade_type,
        "entry_expression": entry_expr,
        "duration": duration,
        "trade_count": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4),
        "total_pnl": round(total_pnl, 4),
        "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else None,
        "skipped_bars": skipped,
        "status": status,
        "indicator_errors": indicator_errors,
        "trades_sample": trades[:5],  # first 5 only; full list could be huge
    }


def backtest_all(
    strategies_rows: list[dict], *, symbol: str | None = None, granularity: int = 60
) -> list[dict]:
    """Backtest every strategy passed in. Used by /backtest/run."""
    out = []
    for row in strategies_rows:
        try:
            payload = (
                json.loads(row["payload"]) if isinstance(row["payload"], str)
                else row["payload"]
            )
            res = backtest_strategy(
                payload, symbol=symbol, granularity=granularity
            )
            res["strategy_id"] = row["id"]
            res["strategy_name"] = row.get("name")
            out.append(res)
        except Exception as exc:
            out.append({
                "strategy_id": row["id"],
                "error": f"backtest crashed: {exc!r}",
            })
    return out
