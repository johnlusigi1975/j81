"""Ship the gathered library to other places.

Three destinations, one model:
  * "analyser" — POST to LOGGING_APP_URL (the next system in the chain).
  * "webhook"  — POST to any URL the user supplies (someone else's analyser).
  * "email"    — SMTP attachment, when SMTP is configured.

The payload shape matches the LoggingAppClient contract:
    POST /strategies  {"by_trade_type": {"even_odd": [...], ...}}
    POST /insights    {"by_trade_type": {...}}

After a successful send, the library files can be moved into
out/_archive/<timestamp>/ so the live library is "fresh" for the next cycle.
build_library() reads from out/strategies and out/insights — archived files
sit outside that tree, so they don't show up in /library or /library.json.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import httpx
from pydantic import BaseModel

from app.config import data_path, get_settings
from app.library import build_library
from app.logging_client import out_insights, out_strategies


class SendResult(BaseModel):
    destination: str
    sent: bool
    strategies_sent: int
    insights_sent: int
    archived: bool = False
    sent_at: str | None = None
    error: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _archive_root() -> Path:
    return data_path("out/_archive")


def _ts_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _counts(payload: dict) -> tuple[int, int]:
    s = sum(len(v) for v in payload.get("strategies_by_trade_type", {}).values())
    i = sum(len(v) for v in payload.get("insights_by_trade_type", {}).values())
    return s, i


def _empty_result(destination: str, msg: str) -> SendResult:
    return SendResult(
        destination=destination,
        sent=False,
        strategies_sent=0,
        insights_sent=0,
        error=msg,
    )


# ---------- destinations --------------------------------------------------


async def send_to_analyser(
    *, archive: bool | None = None
) -> list[SendResult]:
    """Fan out the current library to every configured analyser destination:
    the legacy LOGGING_APP_URL env var (if set) AND every enabled connection
    in connections.json. Returns one SendResult per destination tried.

    Archive policy: only clear the library when *every* destination succeeded.
    Otherwise the files are left in place so the next auto-send cycle (or a
    manual retry) can re-attempt the failed ones — duplicate sends to the
    successful destinations are accepted as the cost of resilience.
    """
    from app.connections import active_for_auto_send, send_payload

    payload = build_library()
    s_count, i_count = _counts(payload)
    if s_count == 0 and i_count == 0:
        return [_empty_result("analyser", "library is empty — nothing to send")]

    targets = active_for_auto_send()
    if not targets:
        return [
            _empty_result(
                "analyser",
                "no analyser destinations configured. Set LOGGING_APP_URL or "
                "add an analyser connection from the homepage.",
            )
        ]

    results: list[SendResult] = []
    for c in targets:
        ok, err = await send_payload(c, payload)
        results.append(
            SendResult(
                destination=f"analyser:{c.name}",
                sent=ok,
                strategies_sent=s_count if ok else 0,
                insights_sent=i_count if ok else 0,
                sent_at=_now_iso() if ok else None,
                error=err,
            )
        )

    if archive is None:
        from app.research_config import load_config

        archive = load_config().sharing.archive_after_send
    full_success = all(r.sent for r in results)
    if archive and full_success:
        await asyncio.to_thread(_archive_current)
        for r in results:
            r.archived = True
    return results


async def send_to_webhook(
    url: str, api_key: str = "", *, archive: bool | None = None
) -> SendResult:
    if not url.startswith(("http://", "https://")):
        return _empty_result("webhook", "URL must start with http:// or https://")
    return await _http_send(
        destination="webhook",
        url=url.rstrip("/"),
        api_key=api_key,
        archive=archive,
    )


async def send_via_email(to: str, *, archive: bool | None = None) -> SendResult:
    settings = get_settings()
    if not (settings.smtp_host and settings.smtp_user and settings.smtp_password):
        return _empty_result(
            "email",
            "SMTP not configured on the server. Use the browser's email button "
            "instead (opens your local mail client with the JSON attached).",
        )
    payload = build_library()
    s_count, i_count = _counts(payload)
    if s_count == 0 and i_count == 0:
        return _empty_result("email", "library is empty — nothing to send")

    msg = EmailMessage()
    msg["Subject"] = (
        f"J81 library — {s_count} strategies, {i_count} insights"
    )
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = to
    msg.set_content(
        f"J81 Deriv Researcher library export.\n\n"
        f"Strategies: {s_count}\nInsights:   {i_count}\n\n"
        f"Full grouped JSON is attached."
    )
    msg.add_attachment(
        json.dumps(payload, indent=2).encode("utf-8"),
        maintype="application",
        subtype="json",
        filename="j81-library.json",
    )

    def _send_blocking() -> None:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as s:
            s.starttls()
            s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)

    try:
        await asyncio.to_thread(_send_blocking)
    except Exception as exc:
        return SendResult(
            destination="email",
            sent=False,
            strategies_sent=s_count,
            insights_sent=i_count,
            error=repr(exc),
        )

    if archive is None:
        from app.research_config import load_config

        archive = load_config().sharing.archive_after_send
    if archive:
        await asyncio.to_thread(_archive_current)
    return SendResult(
        destination="email",
        sent=True,
        strategies_sent=s_count,
        insights_sent=i_count,
        archived=bool(archive),
        sent_at=_now_iso(),
    )


# ---------- shared HTTP path ---------------------------------------------


async def _http_send(
    *,
    destination: str,
    url: str,
    api_key: str,
    archive: bool | None,
) -> SendResult:
    payload = build_library()
    s_count, i_count = _counts(payload)
    if s_count == 0 and i_count == 0:
        return _empty_result(destination, "library is empty — nothing to send")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    timeout = get_settings().request_timeout_seconds

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if s_count:
                r = await client.post(
                    f"{url}/strategies",
                    json={"by_trade_type": payload["strategies_by_trade_type"]},
                    headers=headers,
                )
                r.raise_for_status()
            if i_count:
                r = await client.post(
                    f"{url}/insights",
                    json={"by_trade_type": payload["insights_by_trade_type"]},
                    headers=headers,
                )
                r.raise_for_status()
    except Exception as exc:
        return SendResult(
            destination=destination,
            sent=False,
            strategies_sent=s_count,
            insights_sent=i_count,
            error=repr(exc),
        )

    if archive is None:
        from app.research_config import load_config

        archive = load_config().sharing.archive_after_send
    if archive:
        await asyncio.to_thread(_archive_current)
    return SendResult(
        destination=destination,
        sent=True,
        strategies_sent=s_count,
        insights_sent=i_count,
        archived=bool(archive),
        sent_at=_now_iso(),
    )


# ---------- archive (frees the live library for fresh data) --------------


def _archive_current() -> None:
    """Move every file currently in out/strategies/<tt>/ and out/insights/<tt>/
    into out/_archive/<timestamp>/{strategies|insights}/<tt>/. Leaves the
    trade-type dirs empty (the library iterator filters those out)."""
    stamp = _ts_stamp()
    arch = _archive_root() / stamp
    for src_fn, name in (
        (out_strategies, "strategies"),
        (out_insights, "insights"),
    ):
        src = src_fn()
        if not src.exists():
            continue
        for tt_dir in src.iterdir():
            if not tt_dir.is_dir():
                continue
            files = list(tt_dir.glob("*.json"))
            if not files:
                continue
            dst_dir = arch / name / tt_dir.name
            dst_dir.mkdir(parents=True, exist_ok=True)
            for f in files:
                shutil.move(str(f), str(dst_dir / f.name))
