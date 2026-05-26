"""Researcher productivity self-assessment + peer recommendations.

The Researcher's ~5% peer-watch: it scores its own output, reports that to the
hub, and reads the others' rates to advise them. Kept transparent on purpose.
"""

from __future__ import annotations

from typing import Any

APP = "researcher"


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def compute_self() -> dict[str, Any]:
    """Score the Researcher on whether it's actively producing usable material."""
    from app.scheduler import scheduler  # lazy: avoid import cycle at module load

    st = scheduler.status
    enabled = bool(st.get("enabled"))
    summary = st.get("last_summary") or []
    out_last = sum(
        (s.get("strategies", 0) + s.get("insights", 0))
        for s in summary if isinstance(s, dict)
    )
    has_error = bool(st.get("last_error")) or any(
        isinstance(s, dict) and s.get("error") for s in summary
    )

    if not enabled:
        score = 20.0  # ready but idle — not producing
        text = "autonomous research is OFF — turn it on to produce continuously"
    else:
        score = _clamp(40 + min(50.0, out_last * 2.5) + (10 if not has_error else -10))
        text = f"last cycle produced {out_last} items across {len(summary)} topics"

    return {
        "app": APP,
        "score": round(score, 1),
        "summary": text,
        "metrics": {
            "autonomous_enabled": enabled,
            "cycles": st.get("cycles", 0),
            "output_last_cycle": out_last,
            "topics_last_cycle": len(summary),
            "has_error": has_error,
        },
    }


def recommend_for_peer(app: str, snapshot: dict) -> str | None:
    """The Researcher's one-line suggestion for a peer, from its reported rate."""
    score = snapshot.get("score") or 0
    band = "low" if score < 40 else "moderate" if score < 70 else "strong"
    if app == "analyser":
        if score < 40:
            return (f"Analyser productivity {band} ({score}). Tell me exactly which "
                    f"trade types/indicators you lack so I can target them — and "
                    f"run backtests on what I've already sent.")
        if score < 70:
            return (f"Analyser {band} ({score}). Raise me specific questions via the "
                    f"hub and I'll spend research time answering them.")
        return f"Analyser {band} ({score}). Keep sending balance commands — I'll follow them."
    if app == "bot":
        if score < 40:
            return (f"Bot productivity {band} ({score}). If you're starved of trades, "
                    f"I can prioritise research into higher-frequency setups.")
        return f"Bot {band} ({score}). Tell me which markets win for you and I'll dig there."
    return None


# Dedupe cache — only re-send when the body actually changes per peer.
_LAST_REC_SENT: dict[str, str] = {}


async def peer_watch(write_recommendations: bool = True) -> dict:
    """Self-report to the hub and (optionally) advise peers. Best-effort."""
    from app import comms_client

    me = compute_self()
    await comms_client.report_productivity(me["score"], me["summary"], me["metrics"])
    written = 0
    skipped = 0
    if write_recommendations:
        for snap in await comms_client.all_productivity():
            app = snap.get("app")
            if app == APP:
                continue
            text = recommend_for_peer(app, snap)
            if not text:
                continue
            if _LAST_REC_SENT.get(app) == text:
                skipped += 1
                continue
            await comms_client.send(
                to_app=app, type="recommendation", subject="productivity",
                body=text, data={"their_score": snap.get("score")},
            )
            _LAST_REC_SENT[app] = text
            written += 1
    return {"self_score": me["score"], "recommendations_written": written, "skipped_duplicates": skipped}
