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

from app.api_backbone import call as api_call
from app.config import data_path, get_settings
from app.library import build_library
from app.logging_client import out_insights, out_strategies
from app.research_config import (
    AuthKind,
    Connector,
    ConnectorAuth,
    ConnectorKind,
    PayloadMode,
    load_config,
)


class SendResult(BaseModel):
    destination: str
    sent: bool
    strategies_sent: int
    insights_sent: int
    archived: bool = False
    sent_at: str | None = None
    error: str | None = None
    connector_id: str | None = None  # set when a saved connector was used


class FanOutResult(BaseModel):
    """Outcome of sending to every enabled analyser connector at once."""

    results: list[SendResult]
    sent: int       # how many connectors accepted the push
    failed: int
    archived: bool  # whether the local library was archived after sending


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


def _env_analyser_connector() -> Connector | None:
    """If LOGGING_APP_URL is set but no analyser connectors are configured,
    synthesize a single analyser connector from the env vars so existing
    deployments keep working."""
    s = get_settings()
    if not s.logging_app_url:
        return None
    return Connector(
        id="env",
        name="LOGGING_APP_URL (env)",
        kind=ConnectorKind.ANALYSER,
        base_url=s.logging_app_url,
        auth=ConnectorAuth(
            kind=AuthKind.BEARER if s.logging_app_api_key else AuthKind.NONE,
            token=s.logging_app_api_key,
        ),
    )


def _resolve_analyser_connectors(connector_id: str | None = None) -> list[Connector]:
    cfg = load_config()
    analysers = [
        c for c in cfg.connectors
        if c.kind == ConnectorKind.ANALYSER and c.enabled
    ]
    if connector_id:
        analysers = [c for c in analysers if c.id == connector_id]
        return analysers
    if not analysers:
        env_c = _env_analyser_connector()
        if env_c:
            return [env_c]
    return analysers


async def send_to_analyser(
    *, archive: bool | None = None, connector_id: str | None = None
) -> FanOutResult:
    """Fan out the library to every enabled analyser connector (or one
    specific connector when `connector_id` is given). Archives once after
    all pushes, if requested."""
    connectors = _resolve_analyser_connectors(connector_id)
    if not connectors:
        return FanOutResult(
            results=[
                SendResult(
                    destination="analyser",
                    sent=False,
                    strategies_sent=0,
                    insights_sent=0,
                    error="no analyser connector configured (add one in /connectors, or set LOGGING_APP_URL)",
                )
            ],
            sent=0, failed=1, archived=False,
        )

    payload = build_library()
    s_count, i_count = _counts(payload)
    if s_count == 0 and i_count == 0:
        return FanOutResult(
            results=[_empty_result("analyser", "library is empty — nothing to send")],
            sent=0, failed=1, archived=False,
        )

    results: list[SendResult] = []
    for c in connectors:
        results.append(await _send_via_connector(c, payload))

    if archive is None:
        archive = load_config().sharing.archive_after_send
    any_sent = any(r.sent for r in results)
    archived = False
    if archive and any_sent:
        await asyncio.to_thread(_archive_current)
        archived = True
        for r in results:
            if r.sent:
                r.archived = True

    return FanOutResult(
        results=results,
        sent=sum(1 for r in results if r.sent),
        failed=sum(1 for r in results if not r.sent),
        archived=archived,
    )


async def send_to_webhook(
    url: str, api_key: str = "", *, archive: bool | None = None
) -> SendResult:
    """One-off send to a user-supplied URL (handy for "anyone else's
    analyser" without persisting a connector)."""
    if not url.startswith(("http://", "https://")):
        return _empty_result("webhook", "URL must start with http:// or https://")
    adhoc = Connector(
        id="adhoc",
        name="ad-hoc webhook",
        kind=ConnectorKind.WEBHOOK,
        base_url=url,
        auth=ConnectorAuth(
            kind=AuthKind.BEARER if api_key else AuthKind.NONE,
            token=api_key,
        ),
    )
    payload = build_library()
    s_count, i_count = _counts(payload)
    if s_count == 0 and i_count == 0:
        return _empty_result("webhook", "library is empty — nothing to send")

    result = await _send_via_connector(adhoc, payload)

    if archive is None:
        archive = load_config().sharing.archive_after_send
    if archive and result.sent:
        await asyncio.to_thread(_archive_current)
        result.archived = True
    return result


async def send_to_connector(
    connector: Connector, *, archive: bool | None = None
) -> SendResult:
    """Send the current library to one specific saved connector."""
    payload = build_library()
    s_count, i_count = _counts(payload)
    if s_count == 0 and i_count == 0:
        return _empty_result(connector.kind.value, "library is empty — nothing to send")
    result = await _send_via_connector(connector, payload)
    if archive is None:
        archive = load_config().sharing.archive_after_send
    if archive and result.sent:
        await asyncio.to_thread(_archive_current)
        result.archived = True
    return result


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


# ---------- per-connector send (uses the api_backbone) -------------------


async def _send_via_connector(connector: Connector, payload: dict) -> SendResult:
    """Push the grouped library through one connector. `payload_mode` decides
    the wire format: SPLIT posts /strategies and /insights separately (J81
    native contract); SINGLE posts the whole library object to base_url."""
    s_count, i_count = _counts(payload)
    if s_count == 0 and i_count == 0:
        return _empty_result(connector.kind.value, "library is empty — nothing to send")
    try:
        if connector.payload_mode == PayloadMode.SINGLE:
            r = await api_call(connector, "POST", "", json=payload)
            r.raise_for_status()
        else:
            if s_count:
                r = await api_call(
                    connector, "POST", "strategies",
                    json={"by_trade_type": payload["strategies_by_trade_type"]},
                )
                r.raise_for_status()
            if i_count:
                r = await api_call(
                    connector, "POST", "insights",
                    json={"by_trade_type": payload["insights_by_trade_type"]},
                )
                r.raise_for_status()
    except Exception as exc:
        return SendResult(
            destination=connector.kind.value,
            sent=False,
            strategies_sent=s_count,
            insights_sent=i_count,
            error=repr(exc),
            connector_id=connector.id,
        )
    return SendResult(
        destination=connector.kind.value,
        sent=True,
        strategies_sent=s_count,
        insights_sent=i_count,
        sent_at=_now_iso(),
        connector_id=connector.id,
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
