"""Generalized outbound-API plumbing.

Every external HTTP integration J81 makes — analyser pushes, webhooks,
future notifiers — goes through one of these helpers. The Connector model
captures the destination (URL + auth + headers) and `call()` applies the
auth scheme uniformly. Add a new auth scheme here once and every connector
type gets it.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from pydantic import BaseModel

from app.config import get_settings
from app.research_config import AuthKind, Connector


def _apply_auth(
    connector: Connector, headers: dict[str, str], params: dict[str, str]
) -> tuple[str, str] | None:
    """Mutate headers/params for the connector's auth scheme; return a
    (username, password) tuple if HTTP Basic is requested, else None.
    """
    a = connector.auth
    if a.kind == AuthKind.BEARER and a.token:
        headers["Authorization"] = f"Bearer {a.token}"
    elif a.kind == AuthKind.HEADER and a.header_name and a.token:
        headers[a.header_name] = a.token
    elif a.kind == AuthKind.QUERY and a.query_param and a.token:
        params[a.query_param] = a.token
    elif a.kind == AuthKind.BASIC and a.username:
        return (a.username, a.password)
    return None


async def call(
    connector: Connector,
    method: str,
    path: str = "",
    *,
    json: Any = None,
    extra_params: dict[str, str] | None = None,
) -> httpx.Response:
    """Send a single HTTP request through a connector. Caller handles the
    `httpx.HTTPStatusError` if a non-2xx matters to them."""
    base = connector.base_url.rstrip("/")
    url = base + ("/" + path.lstrip("/") if path else "")
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        **connector.extra_headers,
    }
    params: dict[str, str] = dict(extra_params or {})
    auth_tuple = _apply_auth(connector, headers, params)
    timeout = get_settings().request_timeout_seconds
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.request(
            method.upper(),
            url,
            json=json,
            headers=headers,
            params=params,
            auth=auth_tuple,
        )


class ReachabilityResult(BaseModel):
    reachable: bool
    status_code: int | None = None
    latency_ms: int | None = None
    error: str | None = None


async def test_reachability(connector: Connector) -> ReachabilityResult:
    """Light, side-effect-free check. We try OPTIONS first (servers usually
    answer cheap), then fall back to GET on the base URL. Any 2xx/3xx/4xx
    response means we reached the server; a network/timeout/DNS failure is
    the only thing we count as unreachable."""
    started = time.monotonic()
    for method in ("OPTIONS", "GET"):
        try:
            resp = await call(connector, method)
            return ReachabilityResult(
                reachable=True,
                status_code=resp.status_code,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
            continue
        except Exception as exc:
            return ReachabilityResult(reachable=False, error=repr(exc))
    return ReachabilityResult(
        reachable=False, error="connection refused / timed out / DNS failure"
    )


def redact(connector: Connector) -> dict:
    """Connector → dict suitable for an API response: secret-bearing fields
    are masked. Use this in any GET that returns a connector."""
    d = connector.model_dump()
    a = d.get("auth") or {}
    if a.get("token"):
        a["token"] = "***"
    if a.get("password"):
        a["password"] = "***"
    d["auth"] = a
    return d
