"""Bot productivity self-assessment + peer recommendations.

The Bot's ~5% peer-watch: it scores its own execution health, reports that to
the hub, and advises the others. Transparent scoring on purpose.
"""

from __future__ import annotations

from typing import Any

from app.store import get_store

APP = "bot"

# Per user policy: the researcher branch has been cut from the productive tree.
# We don't advise it, we don't read its productivity, we don't include it in any
# peer-watch round. Its service may still be deployed for ad-hoc manual research.
_TREE_PEERS = {"analyser"}


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def compute_self() -> dict[str, Any]:
    """Score the Bot on execution health: are accounts enabled, are trades
    happening, and (once settled) are they winning + earning markup?"""
    store = get_store()
    stats = store.stats()
    accts_enabled = stats.get("accounts_enabled", 0)
    trades_total = stats.get("trades_total", 0)
    markup = stats.get("markup_earned_total", 0.0) or 0.0

    # Win rate over settled trades (won vs lost).
    trades = store.list_trades(limit=500)
    settled = [t for t in trades if t.get("outcome") in ("won", "lost")]
    wins = sum(1 for t in settled if t.get("outcome") == "won")
    win_rate = (wins / len(settled)) if settled else 0.0
    errors = sum(1 for t in trades if t.get("outcome") == "error")

    if accts_enabled == 0:
        score = 15.0
        text = "no accounts enabled — connect + opt in an account to start"
    elif trades_total == 0:
        score = 35.0
        text = "accounts ready but no trades placed yet"
    else:
        # Activity 40%, win-rate 45%, low-error bonus 15%.
        activity = min(1.0, trades_total / 20.0)
        err_health = 1.0 if not errors else max(0.0, 1 - errors / max(trades_total, 1))
        wr_component = win_rate if settled else 0.5  # neutral until trades settle
        score = _clamp(100 * (0.40 * activity + 0.45 * wr_component + 0.15 * err_health))
        text = (f"{trades_total} trades, "
                f"{'win-rate %.0f%%' % (win_rate * 100) if settled else 'none settled yet'}, "
                f"markup {markup:.4f}")

    return {
        "app": APP,
        "score": round(score, 1),
        "summary": text,
        "metrics": {
            "accounts_enabled": accts_enabled,
            "trades_total": trades_total,
            "settled": len(settled),
            "wins": wins,
            "win_rate": round(win_rate, 3),
            "errors": errors,
            "markup_earned_total": round(markup, 4),
        },
    }


def recommend_for_peer(app: str, snapshot: dict) -> str | None:
    """The Bot's one-line suggestion for a peer, from its reported rate."""
    score = snapshot.get("score") or 0
    band = "low" if score < 40 else "moderate" if score < 70 else "strong"
    if app == "analyser":
        if score < 40:
            return (f"Analyser productivity {band} ({score}). I need actionable, "
                    f"well-confident decisions — backtest more so fewer come back "
                    f"as no-trade.")
        if score < 70:
            return (f"Analyser {band} ({score}). Lean toward the trade types with "
                    f"the strongest survivors; I'll execute what you're confident in.")
        return f"Analyser {band} ({score}). Decisions are actionable — keep it up."
    if app == "researcher":
        if score < 40:
            return (f"Researcher productivity {band} ({score}). The brain is short on "
                    f"strategies, which leaves me idle — please produce more.")
        return f"Researcher {band} ({score}). Good supply — favour high-frequency setups so I trade more."
    return None


# Dedupe cache — only re-send a peer recommendation when the body changes
# from the last one we sent to that peer. Keeps the maintenance log signal-rich
# instead of repeating "Bot strong (72.8)..." every cycle.
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
            if app == APP or app not in _TREE_PEERS:
                continue   # skip self, and skip anyone outside the active tree (researcher cut)
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
