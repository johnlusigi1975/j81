"""Productivity self-assessment + peer recommendations (Analyser side).

Every system spends ~90% of its effort on its own job and ~5% checking how
the others are doing. This module is that 5% for the Analyser: it scores its
own productivity (0-100) from what's in its DB, and — given the productivity
snapshots every system reports to the hub — writes short recommendations
telling each peer how to lift its rate.

Scores are deliberately transparent (a few weighted ratios), not a black box,
so a human can see *why* a system is rated where it is.
"""

from __future__ import annotations

from typing import Any

from app.store import get_store

ANALYSER = "analyser"


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def compute_self() -> dict[str, Any]:
    """Score the Analyser on how well it's doing its job: testing what it
    receives, finding survivors, and producing actionable decisions."""
    store = get_store()
    stats = store.stats()
    s_total = stats["strategies"]["total"]
    backtests = store.latest_backtest_per_strategy()
    tested = len(backtests)
    survivors = sum(1 for b in backtests.values() if b.get("status") == "survived")
    decisions = store.list_decisions(limit=200)
    tradeable = sum(1 for d in decisions if d.get("is_trade"))

    tested_ratio = (tested / s_total) if s_total else 0.0
    survivor_ratio = (survivors / tested) if tested else 0.0
    actionability = (tradeable / len(decisions)) if decisions else 0.0

    # Weighted: testing throughput 35%, survivor quality 35%, decisions 30%.
    score = _clamp(100 * (0.35 * tested_ratio + 0.35 * survivor_ratio + 0.30 * actionability))
    summary = (
        f"tested {tested}/{s_total} strategies, {survivors} survivors, "
        f"{tradeable}/{len(decisions)} decisions tradeable"
    )
    return {
        "app": ANALYSER,
        "score": round(score, 1),
        "summary": summary,
        "metrics": {
            "strategies_received": s_total,
            "strategies_tested": tested,
            "survivors": survivors,
            "tested_ratio": round(tested_ratio, 3),
            "survivor_ratio": round(survivor_ratio, 3),
            "decisions": len(decisions),
            "tradeable": tradeable,
            "actionability": round(actionability, 3),
        },
    }


def recommend_for_peer(app: str, snapshot: dict) -> str | None:
    """One short, specific suggestion for `app` based on its reported
    productivity snapshot. Returns None if there's nothing useful to say."""
    score = snapshot.get("score") or 0
    m = snapshot.get("metrics") or {}
    band = "low" if score < 40 else "moderate" if score < 70 else "strong"

    if app == "researcher":
        spc = m.get("output_last_cycle", m.get("strategies_last_cycle", 0))
        if score < 40:
            return (f"Researcher productivity {band} ({score}). Last cycle yielded "
                    f"little ({spc}). Broaden sources/hashtags and prioritise pages "
                    f"with explicit entry rules so I have more testable strategies.")
        if score < 70:
            return (f"Researcher {band} ({score}). Tilt toward the trade types I "
                    f"lack survivors for; quality of rules matters more than volume.")
        return f"Researcher {band} ({score}). Keep feeding clearly-specified strategies."

    if app == "bot":
        if score < 40:
            return (f"Bot productivity {band} ({score}). Few/again no actionable "
                    f"trades — confirm an account is enabled and the confidence "
                    f"floor isn't filtering everything.")
        if score < 70:
            return (f"Bot {band} ({score}). Watch win-rate vs the survivor bar; "
                    f"feed real settlement outcomes back so I can recalibrate.")
        return f"Bot {band} ({score}). Healthy execution — keep logging real markups."

    if app == "analyser":
        if score < 40:
            return (f"Analyser productivity {band} ({score}). Run more backtests and "
                    f"pull fresh candles so decisions rest on tested strategies.")
        return None
    return None


def write_peer_recommendations() -> int:
    """Read every system's reported productivity and emit a recommendation
    for each peer to the comms bus. Returns how many were written."""
    from app.comms import emit

    store = get_store()
    written = 0
    for snap in store.list_productivity():
        app = snap.get("app")
        if app == ANALYSER:
            continue  # don't advise myself
        text = recommend_for_peer(app, snap)
        if not text:
            continue
        emit(
            to_app=app,
            type="recommendation",
            subject="productivity",
            body=text,
            data={"their_score": snap.get("score")},
            from_app=ANALYSER,
        )
        written += 1
    return written
