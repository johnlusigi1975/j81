"""Researcher's client to the comms hub (the Analyser).

Lets the Researcher: send reports/grades up to the hub, poll its inbox for
commands/advice from the brain, and acknowledge what it has processed.
"""

from __future__ import annotations

import httpx

from app.config import get_settings

APP = "researcher"


def _hub() -> str:
    return get_settings().comms_hub_url.rstrip("/")


async def send(
    *, to_app: str, type: str, subject: str, body: str = "",
    grade: float | None = None, data: dict | None = None,
) -> None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{_hub()}/comms/send",
                json={
                    "from_app": APP, "to_app": to_app, "type": type,
                    "subject": subject, "body": body, "grade": grade,
                    "data": data or {},
                },
            )
    except Exception:
        pass  # comms is best-effort; never break the research loop


async def inbox() -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{_hub()}/comms/inbox", params={"for_app": APP})
            r.raise_for_status()
            return r.json()
    except Exception:
        return []


async def ack(ids: list[str]) -> None:
    if not ids:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(f"{_hub()}/comms/ack", json={"ids": ids})
    except Exception:
        pass


async def report_productivity(score: float, summary: str, metrics: dict) -> None:
    """Push this system's self-assessed productivity to the hub."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{_hub()}/productivity/report",
                json={"app": APP, "score": score, "summary": summary,
                      "metrics": metrics or {}},
            )
    except Exception:
        pass


async def all_productivity() -> list[dict]:
    """Read every system's latest productivity so we can advise peers."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{_hub()}/productivity")
            r.raise_for_status()
            return r.json().get("systems", [])
    except Exception:
        return []


async def report_issue(
    summary: str, *, severity: str = "info", area: str | None = None,
    detail: str | None = None,
) -> None:
    """File a problem with the J81 Maintenance sector (best-effort)."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{_hub()}/maintenance/report",
                json={"from_app": APP, "summary": summary, "severity": severity,
                      "area": area, "detail": detail},
            )
    except Exception:
        pass


async def get_priority() -> dict:
    """Read the tree-wide priority mode from the hub. Returns
    {enabled: bool, trade_types: [...]} — empty/false on any failure."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(f"{_hub()}/priority")
            r.raise_for_status()
            return r.json()
    except Exception:
        return {"enabled": False, "trade_types": []}


async def log_study(
    kind: str, body: str, *, topic: str | None = None, source: str | None = None,
) -> None:
    """Log a self-study entry (question | learning | enhancement) to the hub's
    study log. Best-effort — never breaks the caller's loop."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{_hub()}/study/log",
                json={"app": APP, "kind": kind, "body": body,
                      "topic": topic, "source": source},
            )
    except Exception:
        pass
