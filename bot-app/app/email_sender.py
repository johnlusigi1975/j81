"""Transactional email — sends the buyer's lifetime license code to the email
Stripe collected at checkout. Uses Resend's HTTP API (free tier: 3,000/mo).

Disabled (no-op) when RESEND_API_KEY isn't set — letting the rest of the
payment flow still work. Failures NEVER raise; webhooks must not retry on
mail bounces.
"""
from __future__ import annotations
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)
_RESEND_URL = "https://api.resend.com/emails"


def _settings():
    from app.config import get_settings
    return get_settings()


def _enabled() -> bool:
    s = _settings()
    return bool((s.resend_api_key or "").strip() and (s.email_from or "").strip())


def _license_email_html(code: str, loginids: list[str], stripe_session_id: str | None) -> str:
    bind = ", ".join(loginids or []) or "your Deriv account"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#0a0d12;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#e6e7ea">
  <div style="max-width:560px;margin:40px auto;padding:32px 24px;background:linear-gradient(135deg,#141c28,#08111c);border:1px solid rgba(212,184,122,.35);border-radius:14px">
    <div style="text-align:center;font-size:13px;font-family:monospace;letter-spacing:.22em;color:#d4b87a;font-weight:800;margin-bottom:14px">LIFETIME ACCESS · ACTIVATED 🎉</div>
    <h1 style="text-align:center;margin:0 0 14px;font-size:26px;font-weight:800;color:#fff;letter-spacing:-.01em">Welcome to J81 Trade Desk</h1>
    <p style="font-size:15px;line-height:1.55;color:#aab0bd;margin:0 0 22px;text-align:center">
      Your $100 lifetime purchase is confirmed. Here's your license code — paste it inside J81 to unlock the desk.
    </p>
    <div style="margin:0 0 22px;padding:18px;border:1px dashed rgba(212,184,122,.5);border-radius:11px;background:rgba(212,184,122,.08);text-align:center">
      <div style="font-size:11px;font-family:monospace;letter-spacing:.20em;color:#d4b87a;text-transform:uppercase;font-weight:800;margin-bottom:8px">YOUR LIFETIME LICENSE CODE</div>
      <div style="font-family:monospace;font-size:22px;font-weight:800;color:#fff;letter-spacing:.08em;padding:8px 14px;background:rgba(0,0,0,.30);border-radius:7px;display:inline-block;user-select:all">{code}</div>
      <div style="font-size:11px;font-family:monospace;color:#7f8694;margin-top:10px;letter-spacing:.04em">
        🔒 Locks to <b style="color:#d4b87a">{bind}</b> on first use · only your Deriv login can redeem this
      </div>
    </div>
    <h3 style="font-size:14px;font-weight:800;color:#fff;letter-spacing:.04em;margin:24px 0 10px">How to unlock:</h3>
    <ol style="font-size:14px;line-height:1.65;color:#aab0bd;margin:0 0 22px;padding-left:22px">
      <li>Open <a href="https://j81-trade-desk.onrender.com" style="color:#46cf9e;text-decoration:none">j81-trade-desk.onrender.com</a> and connect your Deriv account</li>
      <li>Click any account (demo or real) → the paywall appears</li>
      <li>Click <b>"Have a code?"</b> → paste the code above → click Unlock</li>
      <li>Lifetime access — every device, every Deriv account you own</li>
    </ol>
    <div style="margin:24px 0 0;padding-top:18px;border-top:1px solid rgba(255,255,255,.06);font-size:11px;line-height:1.6;color:#7f8694;text-align:center">
      Save this email — it's your proof of purchase. Your license code is permanent.<br>
      Payment ref: <code style="font-family:monospace;color:#aab0bd">{stripe_session_id or 'n/a'}</code>
    </div>
    <div style="text-align:center;margin:18px 0 0;font-size:11px;color:#7f8694;line-height:1.6">
      J81 is a set of analysis &amp; risk tools — <b>not</b> financial advice and <b>not</b> a guarantee of profit.
      Deriv synthetic indices are an audited RNG with a house edge; you can lose money. Trade only what you can afford to lose.
    </div>
  </div>
</body></html>"""


def _license_email_text(code: str, loginids: list[str], stripe_session_id: str | None) -> str:
    bind = ", ".join(loginids or []) or "your Deriv account"
    return (
        "Welcome to J81 Trade Desk\n\n"
        "Your $100 lifetime purchase is confirmed.\n\n"
        f"YOUR LIFETIME LICENSE CODE:\n\n    {code}\n\n"
        f"This code locks to {bind} on first use — only your Deriv login can redeem it.\n\n"
        "HOW TO UNLOCK:\n"
        "  1. Open https://j81-trade-desk.onrender.com\n"
        "  2. Connect your Deriv account\n"
        "  3. Click any account → paywall appears\n"
        "  4. Click 'Have a code?' → paste the code above → click Unlock\n"
        "  5. Lifetime access on every device, every Deriv account you own.\n\n"
        f"Save this email as proof of purchase. Payment ref: {stripe_session_id or 'n/a'}\n\n"
        "J81 is a set of analysis & risk tools — not financial advice and not a\n"
        "guarantee of profit. Trade only what you can afford to lose.\n"
    )


def send_license_email(
    to_email: str,
    code: str,
    loginids: list[str] | None = None,
    stripe_session_id: str | None = None,
) -> dict[str, Any]:
    """Send the lifetime-license-code email. Returns a dict with status info.
    NEVER raises — webhooks must remain idempotent even if mail send fails."""
    if not _enabled():
        log.info("email send skipped — RESEND_API_KEY or EMAIL_FROM not configured")
        return {"sent": False, "reason": "not configured"}
    if not to_email or "@" not in to_email:
        return {"sent": False, "reason": "no email address"}
    s = _settings()
    payload = {
        "from":    s.email_from,
        "to":      [to_email],
        "subject": "Your J81 Trade Desk lifetime license code 🎉",
        "html":    _license_email_html(code, loginids or [], stripe_session_id),
        "text":    _license_email_text(code, loginids or [], stripe_session_id),
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.post(_RESEND_URL,
                            headers={"Authorization": f"Bearer {s.resend_api_key}",
                                     "Content-Type": "application/json"},
                            json=payload)
        if r.status_code in (200, 202):
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            log.info("license email sent to %s · id=%s", to_email, data.get("id", "?"))
            return {"sent": True, "id": data.get("id"), "to": to_email}
        log.warning("license email failed status=%s body=%s", r.status_code, r.text[:160])
        return {"sent": False, "reason": f"http {r.status_code}", "detail": r.text[:160]}
    except Exception as exc:
        log.warning("license email exception: %r", exc)
        return {"sent": False, "reason": "exception", "detail": repr(exc)[:160]}
