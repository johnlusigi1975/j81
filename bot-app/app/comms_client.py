"""Comms client — single-bot edition.

There is no inter-service hub any more (the analyser + researcher were
collapsed into this process). These functions remain so existing call sites
keep working, but they're no-ops or local in-memory equivalents. Keeping the
same surface avoids touching every caller in trading_loop / productivity /
self_study.
"""

from __future__ import annotations

APP = "bot"
_PRIORITY: dict = {"enabled": False, "trade_types": []}


async def send(*, to_app: str, type: str, subject: str, body: str = "",
               grade: float | None = None, data: dict | None = None) -> None:
    """No-op in single-bot edition (no peers to send to)."""
    return None


async def inbox() -> list[dict]:
    return []


async def ack(ids: list[str]) -> None:
    return None


async def report_productivity(score: float, summary: str, metrics: dict) -> None:
    """No-op — there is no peer hub to report to in this edition."""
    return None


async def all_productivity() -> list[dict]:
    """No peers, so no shared productivity board."""
    return []


async def report_issue(summary: str, *, severity: str = "info",
                        area: str | None = None, detail: str | None = None) -> None:
    """No issue store in this edition — silent best-effort."""
    return None


async def get_priority() -> dict:
    """Local in-memory priority flag (used to be served by the analyser hub)."""
    return dict(_PRIORITY)


async def set_priority(enabled: bool) -> dict:
    _PRIORITY["enabled"] = bool(enabled)
    return dict(_PRIORITY)


async def log_study(kind: str, body: str, *, topic: str | None = None,
                     source: str | None = None) -> None:
    """No-op — study log lived on the analyser; this edition has no hub."""
    return None
