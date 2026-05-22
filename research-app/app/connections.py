"""Persistent registry of downstream analyser/API connections.

Every entry is an HTTP target that speaks the J81 contract:
    POST <base_url>/strategies  {"by_trade_type": {...}}
    POST <base_url>/insights    {"by_trade_type": {...}}

The auto-send loop fans out to every enabled connection in this registry,
plus the legacy LOGGING_APP_URL (which is preserved as a "system" target
so existing setups keep working). Add/remove/edit connections at runtime
via /connections — no restart needed.

Secrets (bearer tokens, header values) live on disk under DATA_DIR with
0o600 permissions and are NEVER returned verbatim by GET endpoints — only
their presence is reported via `has_credential`. Edit flows treat empty
secret fields as "leave unchanged" so a user editing a connection through
the UI doesn't accidentally clear the token by leaving the box blank.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field

from app.config import data_path, get_settings


AuthType = Literal["none", "bearer", "header"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path() -> Path:
    return data_path("connections.json")


_lock = threading.Lock()


# ---------------- Pydantic shapes -----------------------------------------


class ApiConnection(BaseModel):
    """On-disk representation. Includes secrets."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    name: str
    base_url: str
    auth_type: AuthType = "none"
    auth_token: str = ""              # used when auth_type == "bearer"
    auth_header_name: str | None = None
    auth_header_value: str = ""       # used when auth_type == "header"
    enabled: bool = True
    include_in_auto_send: bool = True
    created_at: str = Field(default_factory=_now_iso)
    last_send_at: str | None = None
    last_status: str | None = None    # "ok" | "error"
    last_error: str | None = None


class ApiConnectionPublic(BaseModel):
    """Returned by GET endpoints. Secrets are masked."""

    id: str
    name: str
    base_url: str
    auth_type: AuthType
    has_credential: bool
    auth_header_name: str | None = None
    enabled: bool
    include_in_auto_send: bool
    created_at: str
    last_send_at: str | None = None
    last_status: str | None = None
    last_error: str | None = None


class ApiConnectionInput(BaseModel):
    """Body for create / update. Empty secret fields on update mean
    "leave existing secret as-is"."""

    name: str
    base_url: str
    auth_type: AuthType = "none"
    auth_token: str | None = None
    auth_header_name: str | None = None
    auth_header_value: str | None = None
    enabled: bool = True
    include_in_auto_send: bool = True


# ---------------- file-backed store ---------------------------------------


def _load_all() -> list[ApiConnection]:
    p = _path()
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text())
        return [ApiConnection(**d) for d in raw]
    except Exception:
        return []


def _save_all(conns: list[ApiConnection]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([c.model_dump() for c in conns], indent=2))
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass  # not fatal (e.g. on Windows file systems)


def _to_public(c: ApiConnection) -> ApiConnectionPublic:
    return ApiConnectionPublic(
        id=c.id,
        name=c.name,
        base_url=c.base_url,
        auth_type=c.auth_type,
        has_credential=bool(c.auth_token or c.auth_header_value),
        auth_header_name=c.auth_header_name,
        enabled=c.enabled,
        include_in_auto_send=c.include_in_auto_send,
        created_at=c.created_at,
        last_send_at=c.last_send_at,
        last_status=c.last_status,
        last_error=c.last_error,
    )


# ---------------- CRUD ----------------------------------------------------


def list_public() -> list[ApiConnectionPublic]:
    return [_to_public(c) for c in _load_all()]


def get_internal(conn_id: str) -> ApiConnection | None:
    for c in _load_all():
        if c.id == conn_id:
            return c
    return None


def create(body: ApiConnectionInput) -> ApiConnectionPublic:
    c = ApiConnection(
        name=body.name.strip(),
        base_url=body.base_url.strip().rstrip("/"),
        auth_type=body.auth_type,
        auth_token=(body.auth_token or "").strip(),
        auth_header_name=(body.auth_header_name or "").strip() or None,
        auth_header_value=(body.auth_header_value or "").strip(),
        enabled=body.enabled,
        include_in_auto_send=body.include_in_auto_send,
    )
    with _lock:
        conns = _load_all()
        conns.append(c)
        _save_all(conns)
    return _to_public(c)


def update(conn_id: str, body: ApiConnectionInput) -> ApiConnectionPublic | None:
    with _lock:
        conns = _load_all()
        for i, c in enumerate(conns):
            if c.id != conn_id:
                continue
            c.name = body.name.strip()
            c.base_url = body.base_url.strip().rstrip("/")
            c.auth_type = body.auth_type
            # "leave as-is" if the secret field is None/empty on edit
            if body.auth_token is not None and body.auth_token.strip():
                c.auth_token = body.auth_token.strip()
            if body.auth_type == "none":
                c.auth_token = ""
                c.auth_header_value = ""
            if body.auth_header_name is not None:
                c.auth_header_name = body.auth_header_name.strip() or None
            if (
                body.auth_header_value is not None
                and body.auth_header_value.strip()
            ):
                c.auth_header_value = body.auth_header_value.strip()
            c.enabled = body.enabled
            c.include_in_auto_send = body.include_in_auto_send
            conns[i] = c
            _save_all(conns)
            return _to_public(c)
    return None


def delete(conn_id: str) -> bool:
    with _lock:
        conns = _load_all()
        new = [c for c in conns if c.id != conn_id]
        if len(new) == len(conns):
            return False
        _save_all(new)
        return True


# ---------------- fan-out helpers -----------------------------------------


def env_connection() -> ApiConnection | None:
    """The legacy LOGGING_APP_URL exposed as a virtual connection so it
    participates in fan-out alongside file-based connections."""
    settings = get_settings()
    if not settings.logging_app_url:
        return None
    return ApiConnection(
        id="__env__",
        name="Default analyser (LOGGING_APP_URL)",
        base_url=settings.logging_app_url.rstrip("/"),
        auth_type="bearer" if settings.logging_app_api_key else "none",
        auth_token=settings.logging_app_api_key,
    )


def active_for_auto_send() -> list[ApiConnection]:
    out: list[ApiConnection] = []
    env = env_connection()
    if env is not None:
        out.append(env)
    out.extend(
        c for c in _load_all() if c.enabled and c.include_in_auto_send
    )
    return out


# ---------------- HTTP send + test ----------------------------------------


def _headers_for(c: ApiConnection) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if c.auth_type == "bearer" and c.auth_token:
        h["Authorization"] = f"Bearer {c.auth_token}"
    elif (
        c.auth_type == "header"
        and c.auth_header_name
        and c.auth_header_value
    ):
        h[c.auth_header_name] = c.auth_header_value
    return h


def _record_status(conn_id: str, ok: bool, error: str | None) -> None:
    if conn_id == "__env__":
        return  # virtual, no persistent record
    now = _now_iso()
    with _lock:
        conns = _load_all()
        for i, c in enumerate(conns):
            if c.id == conn_id:
                c.last_send_at = now
                c.last_status = "ok" if ok else "error"
                c.last_error = error
                conns[i] = c
                _save_all(conns)
                return


async def send_payload(c: ApiConnection, payload: dict) -> tuple[bool, str | None]:
    s_count = sum(
        len(v) for v in payload.get("strategies_by_trade_type", {}).values()
    )
    i_count = sum(
        len(v) for v in payload.get("insights_by_trade_type", {}).values()
    )
    timeout = get_settings().request_timeout_seconds
    url = c.base_url.rstrip("/")
    headers = _headers_for(c)
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
        _record_status(c.id, False, repr(exc))
        return False, repr(exc)
    _record_status(c.id, True, None)
    return True, None


async def test_connection(c: ApiConnection) -> tuple[bool, str | None]:
    """Probe a connection with an empty payload. Anything 2xx or 4xx means
    we reached the server (auth may or may not have passed, but the host is
    alive). 5xx or transport errors are treated as failure."""
    timeout = get_settings().request_timeout_seconds
    headers = _headers_for(c)
    url = c.base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{url}/strategies",
                json={"by_trade_type": {}},
                headers=headers,
            )
    except Exception as exc:
        return False, repr(exc)
    if r.status_code >= 500:
        return False, f"HTTP {r.status_code}"
    return True, None if r.status_code < 400 else f"reachable (HTTP {r.status_code})"
