"""Analyser brain self-student.

A dedicated "how do I get better?" loop. It reads the Analyser's own
productivity, names concrete weaknesses, asks the Researcher (over the comms
bus) to fetch articles/tools on those topics, and files an *enhancement
proposal* into the study log — which the homepage exports for the AI (Claude)
to actually implement. This is the system studying itself, not just running.
"""

from __future__ import annotations

from typing import Any

from app import productivity
from app.comms import emit
from app.store import get_store

APP = "analyser"


def diagnose() -> list[dict[str, Any]]:
    """Turn my own metrics into specific, actionable weaknesses. Each entry has
    a `topic` (dedupe key), a `query` (what to study online), and an
    `enhancement` (a concrete proposal for the AI)."""
    me = productivity.compute_self()
    m = me["metrics"]
    out: list[dict[str, Any]] = []

    if m.get("strategies_tested", 0) > 0 and m.get("survivors", 0) == 0:
        out.append({
            "topic": "find-surviving-strategies",
            "query": ("deriv synthetic indices backtested high win-rate strategy "
                      "rules RSI bollinger digit edge filters volatility regime"),
            "enhancement": (
                "Backtests find 0 survivors. Worth implementing: (a) per-symbol "
                "pip precision in digit backtests, (b) ensemble agreeing strategies "
                "to lift confidence, (c) volatility-regime entry filters, "
                "(d) revisit the survivor bar (win_rate>=0.55 + >=5 trades)."),
        })
    if m.get("strategies_received", 0) and m.get("tested_ratio", 0) < 0.9:
        out.append({
            "topic": "test-everything-received",
            "query": "automated backtesting pipeline coverage walk-forward synthetic indices",
            "enhancement": ("Not all received strategies get tested. Consider "
                            "auto-fetching candles for every strategy's symbol so "
                            "none stay 'inconclusive' for lack of data."),
        })
    if m.get("decisions", 0) and m.get("actionability", 0) < 0.3:
        out.append({
            "topic": "improve-decision-actionability",
            "query": "trading decision confidence calibration ensemble voting no-trade threshold",
            "enhancement": ("Most decisions are no-trade. Consider ensembling "
                            "multiple agreeing strategies and per-trade-type "
                            "confidence thresholds instead of one global floor."),
        })
    if not out:
        out.append({
            "topic": "incremental-edge",
            "query": "deriv tick digit stream pattern detection markov edge synthetic indices",
            "enhancement": ("Running healthy. Explore digit-stream pattern "
                            "detection (Markov / run-length) for incremental edge."),
        })
    return out


async def study_once(max_requests: int = 1) -> dict:
    """File enhancement proposals + ask the Researcher to fetch study material
    for the top weakness. Best-effort. Returns a small summary."""
    store = get_store()
    weaknesses = diagnose()
    requests_sent = 0
    for w in weaknesses:
        # The proposal that ends up in front of the AI (Claude).
        store.add_study(APP, "enhancement", w["enhancement"],
                        topic=w["topic"], source="self-diagnosis")
        # Ask the Researcher to go study this online (its q&a budget answers it).
        if requests_sent < max_requests:
            store.add_study(APP, "question", w["query"],
                            topic=w["topic"], source="sent to researcher")
            emit(to_app="researcher", type="request",
                 subject=f"self-study: {w['topic']}",
                 body=f"Find articles/tools to help the brain improve: {w['query']}",
                 data={"query": w["query"]}, from_app=APP)
            requests_sent += 1
    return {"weaknesses": len(weaknesses), "requests_sent": requests_sent}
