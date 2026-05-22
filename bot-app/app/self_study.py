"""Bot brain self-student.

Reads the Bot's own execution health, names concrete weaknesses, asks the
Researcher (over the comms bus) to fetch articles/tools on those topics, and
files enhancement proposals into the study log for the AI (Claude) to implement.
"""

from __future__ import annotations

from typing import Any

APP = "bot"


def diagnose() -> list[dict[str, Any]]:
    """Turn the Bot's execution metrics into specific improvement topics."""
    from app import productivity

    m = productivity.compute_self()["metrics"]
    out: list[dict[str, Any]] = []

    if m.get("accounts_enabled", 0) == 0:
        out.append({
            "topic": "go-live-safely",
            "query": "deriv api oauth connect account demo testing risk limits best practices",
            "enhancement": ("No account enabled. Smooth the connect+opt-in flow and "
                            "add a guided demo-first checklist before real money."),
        })
    elif m.get("trades_total", 0) == 0:
        out.append({
            "topic": "start-trading",
            "query": "deriv synthetic indices entry timing tick duration selection",
            "enhancement": ("Account ready but no trades. Verify the confidence "
                            "floor isn't filtering everything; surface why each "
                            "cycle skipped."),
        })
    else:
        wr = m.get("win_rate", 0.0)
        if m.get("settled", 0) >= 5 and wr < 0.5:
            out.append({
                "topic": "raise-win-rate",
                "query": "binary options risk management position sizing stop loss take profit synthetic indices",
                "enhancement": ("Win-rate below 50%. Implement take-profit/stop-loss "
                                "via contract_update and only act on the brain's "
                                "highest-confidence decisions."),
            })
        if m.get("errors", 0) > 0:
            out.append({
                "topic": "cut-execution-errors",
                "query": "deriv api buy proposal error handling retry idempotency",
                "enhancement": ("Execution errors seen — add typed handling/retries "
                                "around proposal+buy and clearer surfacing of the cause."),
            })

    if not out:
        out.append({
            "topic": "incremental-edge",
            "query": "deriv markup optimisation contract selection payout efficiency",
            "enhancement": ("Healthy. Explore selling early on adverse moves and "
                            "choosing contracts with the best payout/markup ratio."),
        })
    return out


async def study_once(max_requests: int = 1) -> dict:
    """File enhancement proposals + ask the Researcher to study the top weakness."""
    from app import comms_client

    weaknesses = diagnose()
    requests_sent = 0
    for w in weaknesses:
        await comms_client.log_study(
            "enhancement", w["enhancement"], topic=w["topic"], source="self-diagnosis")
        if w.get("query") and requests_sent < max_requests:
            await comms_client.log_study(
                "question", w["query"], topic=w["topic"], source="sent to researcher")
            await comms_client.send(
                to_app="researcher", type="request",
                subject=f"self-study: {w['topic']}",
                body=f"Find articles/tools to help the bot improve: {w['query']}",
                data={"query": w["query"]},
            )
            requests_sent += 1
    return {"weaknesses": len(weaknesses), "requests_sent": requests_sent}
