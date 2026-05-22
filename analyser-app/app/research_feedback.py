"""Analyser → Researcher feedback channel.

When the Analyser detects a gap (no strategies for trade_type X, every
tested strategy rejected, win rates too low) it can POST a request back
to the Researcher's /research-requests endpoint to drive future cycles
in a useful direction.

This closes the loop: the brain isn't passive any more, it tells the
data-gatherer what to look for next.
"""

from __future__ import annotations

import httpx

from app.config import get_settings


class FeedbackError(Exception):
    pass


async def dispatch_gaps(gaps: list[dict]) -> dict:
    """Post each gap as a research-request to the Researcher. Returns a
    summary of what was sent + accepted."""
    settings = get_settings()
    if not settings.researcher_url:
        raise FeedbackError("RESEARCHER_URL is not set")

    url = settings.researcher_url.rstrip("/") + "/research-requests"
    headers = {"Content-Type": "application/json"}
    if settings.researcher_api_key:
        headers["Authorization"] = f"Bearer {settings.researcher_api_key}"

    accepted = 0
    rejected = 0
    detail = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        for g in gaps:
            try:
                resp = await client.post(
                    url,
                    headers=headers,
                    json={
                        "topic_name": f"analyser-gap-{g.get('kind','?')}-{g.get('trade_type','any')}",
                        "query": g["query"],
                        "trade_type": g.get("trade_type"),
                        "why": g.get("why"),
                        "priority": g.get("priority", "medium"),
                    },
                )
                if 200 <= resp.status_code < 300:
                    accepted += 1
                    detail.append({"gap": g["kind"], "ok": True})
                else:
                    rejected += 1
                    detail.append({"gap": g["kind"], "ok": False, "status": resp.status_code})
            except Exception as exc:
                rejected += 1
                detail.append({"gap": g["kind"], "ok": False, "error": repr(exc)})
    return {"sent": accepted, "failed": rejected, "details": detail}
