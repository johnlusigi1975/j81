"""OAuth token lifecycle — keep an account's Deriv access token fresh.

New-platform access tokens expire in ~1 hour. If we captured a refresh token at
sign-in (scope includes `offline_access`), we can silently renew the access
token so balance reads, settlement and auto-trading survive past the hour
without the user reconnecting.

Two entry points:
  * get_access_token(account_id)  — returns a usable access token, refreshing
    PROACTIVELY if it's within REFRESH_SKEW of expiry.
  * with_fresh_token(account_id, fn) — runs an async Deriv call and, on an AUTH
    error, refreshes ONCE and retries (reactive safety net).

Legacy accounts have no refresh token; we just return their stored token.
Everything is best-effort: if refresh isn't possible we fall back to the current
token, and the caller surfaces `needs_reconnect` rather than crashing.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.deriv import DerivBotError
from app.store import get_store

REFRESH_SKEW = 120  # refresh when within this many seconds of expiry

_AUTH_HINTS = ("auth", "token", "otp", "401", "403", "unauthor", "invalid", "expire")


def is_auth_error(exc: Exception) -> bool:
    return any(h in str(exc).lower() for h in _AUTH_HINTS)


def _expires_soon(acct: dict) -> bool:
    exp = acct.get("token_expires_at")
    if not exp:
        return False  # unknown expiry → don't churn; rely on reactive retry
    try:
        dt = datetime.fromisoformat(exp)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - datetime.now(timezone.utc)).total_seconds() <= REFRESH_SKEW


async def refresh_now(account_id: str) -> str | None:
    """Force an OAuth refresh for one account; persist + return the new access
    token, or None if it couldn't be refreshed (no refresh token / Deriv error)."""
    store = get_store()
    rt = store.decrypted_refresh_for(account_id)
    if not rt:
        return None
    from app import deriv_new
    try:
        tok = await deriv_new.refresh_token(rt)
    except Exception:
        return None
    access = tok.get("access_token")
    if not access:
        return None
    store.update_token(account_id, access=access,
                       refresh=tok.get("refresh_token"), expires_in=tok.get("expires_in"))
    return access


async def get_access_token(account_id: str) -> str | None:
    """A usable access token for this account, proactively refreshed if it's
    about to expire (new platform only). Falls back to the stored token."""
    store = get_store()
    acct = store.get_internal(account_id)
    if not acct:
        return None
    if (acct.get("platform") or "legacy").lower() == "new" and _expires_soon(acct):
        fresh = await refresh_now(account_id)
        if fresh:
            return fresh
    return store.decrypted_token_for(account_id)


async def with_fresh_token(account_id: str, fn):
    """Run `await fn(token)`; on an auth error, refresh once and retry. Raises
    DerivBotError('reconnect required') if there's no usable token at all."""
    token = await get_access_token(account_id)
    if not token:
        raise DerivBotError("reconnect required — no usable token")
    try:
        return await fn(token)
    except Exception as exc:
        if is_auth_error(exc):
            fresh = await refresh_now(account_id)
            if fresh and fresh != token:
                return await fn(fresh)
        raise
