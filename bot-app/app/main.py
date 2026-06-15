#
# ─── A blessing over this code ───────────────────────────────────────
#
#   "He shall be like a tree planted by the rivers of water, that
#    bringeth forth his fruit in his season; his leaf also shall not
#    wither; and whatsoever he doeth shall prosper."
#                                                       — Psalm 1:3
#
#   "Blessed is the man that trusteth in the Lord, and whose hope the
#    Lord is. For he shall be as a tree planted by the waters, and that
#    spreadeth out her roots by the river, and shall not see when heat
#    cometh, but her leaf shall be green; and shall not be careful in
#    the year of drought, neither shall cease from yielding fruit."
#                                                  — Jeremiah 17:7-8
#
# ──────────────────────────────────────────────────────────────────
"""J81 Bot — execution layer (system 3 of 3).

  GET  /                     homepage
  GET  /health               status incl. DRY_RUN flag
  GET  /oauth/start          redirects user to Deriv to authorize
  GET  /oauth/callback       Deriv calls this with the user's account tokens
  GET  /accounts             list connected accounts (no tokens leaked)
  PATCH /accounts/{id}       enable/disable + edit risk limits
  DELETE /accounts/{id}      forget this user's account
  POST /accounts/{id}/trade  manually trigger one decision/trade cycle
  GET  /trades               recent trade log (dry-run + live)
  GET  /stats                accounts, trades, projected markup
  GET  /loop/status          autonomous loop status
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote as urlquote, urlencode

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from app.config import APP_NAME, APP_VERSION, get_settings
from app.deriv import (
    DerivBotError,
    authorize_account,
    estimate_markup,
    get_proposal,
    is_demo_account,
)
from app.executor import (
    MANUAL_TRADE_TYPES,
    execute_decision_for_account,
    execute_manual_trade,
    get_decision,
    settle_pending_for_account,
)
from app.store import get_store
from app.trading_loop import loop as trading_loop


@asynccontextmanager
async def lifespan(_: FastAPI):
    trading_loop.ensure_running()
    from app.mpro import engine as mpro_engine
    mpro_engine.ensure_running()
    # Single-bot edition: observer + library used to live on the analyser; they
    # now run in this same process. Start the 24/7 tick observer and refresh
    # the live payout table once at boot (best-effort).
    try:
        from app.observer import observer as _obs
        _obs.ensure_running()
    except Exception:
        pass
    try:
        import asyncio as _asyncio
        from app import library as _lib
        _asyncio.create_task(_lib.refresh_payouts())   # one-shot best-effort
    except Exception:
        pass
    yield


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)
_HOMEPAGE = Path(__file__).parent / "web" / "index.html"

# ---- per-visitor session: each browser sees only the account(s) IT connected,
# so a client opening the link gets their own Connect→Deriv flow instead of
# landing on whoever connected first. ----
SESSION_COOKIE = "j81_sid"
_SESSION_MAX_AGE = 60 * 60 * 24 * 180  # 180 days


def _new_sid() -> str:
    return _secrets.token_urlsafe(24)


def _set_session_cookie(resp: Response, sid: str) -> None:
    resp.set_cookie(SESSION_COOKIE, sid, max_age=_SESSION_MAX_AGE,
                    httponly=True, samesite="lax", secure=True, path="/")


def _no_cache(resp):
    """Tell browsers to ALWAYS revalidate the HTML with the server (304 when
    unchanged, full content when changed). Without this, every device caches
    the SPA and refreshes can show a stale version of the app — exactly the
    "I updated but it didn't change on my phone" symptom."""
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/", include_in_schema=False)
def home(request: Request) -> FileResponse:
    resp = FileResponse(_HOMEPAGE, media_type="text/html")
    if not request.cookies.get(SESSION_COOKIE):
        _set_session_cookie(resp, _new_sid())  # give every visitor a session up front
    return _no_cache(resp)


def _require_member(request: Request) -> None:
    """Server-side paywall enforcement for trade-placing endpoints.
    Without this, anyone could flip `window.LICENSED = true` in DevTools
    and trade for free — the frontend gate is cosmetic. This is the real
    gate. No-ops if REQUIRE_ACCESS is False (open mode)."""
    s = get_settings()
    if not s.require_access:
        return
    sid = request.cookies.get(SESSION_COOKIE) or ""
    store = get_store()
    # Collect Deriv loginids this session owns — same logic as /access/status.
    loginids: list[str] = []
    try:
        for a in store.list_accounts_public(sid):
            lid = a.get("deriv_account_id")
            if lid: loginids.append(lid)
    except Exception:
        pass
    st = store.access_status(sid, loginids=loginids)
    if not st.get("licensed"):
        # HTTP 402 Payment Required — the only really-meant-for-this-purpose code.
        raise HTTPException(402, "paid membership required — please complete checkout")


@app.get("/owner", include_in_schema=False)
def owner_page() -> FileResponse:
    """Owner console to mint/copy membership codes (guarded by ADMIN_KEY on the
    API calls it makes; the page itself holds no secret)."""
    return _no_cache(FileResponse(Path(__file__).parent / "web" / "owner.html", media_type="text/html"))


# ---------------------------------------------------------------------------
# Paid access (membership paywall) — honest model: pay for the TOOLS.
# ---------------------------------------------------------------------------


@app.get("/access/status")
def access_status(request: Request, response: Response) -> dict:
    """Is the caller a paid member? Also returns the offer (price + buy link) so
    the paywall can render. require_access=false means the app is open to all.

    New anti-sharing model: we look up the caller's Deriv loginids (any account
    they've connected in this browser) and check if ANY of them is bound to an
    active lifetime license. So one purchase covers all of a user's accounts on
    any device, but a different Deriv user gets the paywall."""
    s = get_settings()
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        sid = _new_sid(); _set_session_cookie(response, sid)
    store = get_store()
    # Collect every Deriv loginid this browser session owns. Some sessions
    # have none yet (they haven't OAuthed); those just fall through to the
    # legacy session-bound check inside access_status.
    loginids: list[str] = []
    try:
        for acct in store.list_accounts_public(sid):
            lid = acct.get("deriv_account_id")
            if lid: loginids.append(lid)
    except Exception:
        pass
    st = store.access_status(sid, loginids=loginids)
    # Total paid customers — for the paywall's social-proof counter.
    try:
        st["customers"] = store.count_active_licenses()
    except Exception:
        pass
    # If they're licensed, look up their actual license code so the front-end
    # can show it in the welcome modal ("your lifetime receipt"). Only returned
    # to the rightful owner — license_by_loginid_any only matches their loginids.
    try:
        if st.get("licensed") and loginids:
            lic = store.license_by_loginid_any(loginids)
            if lic: st["license_code"] = lic.get("code")
    except Exception:
        pass
    return {**st, "require_access": s.require_access,
            "price_label": s.access_price_label, "buy_url": s.access_buy_url,
            "tier": (st.get("tier") if st.get("licensed") else None),
            "offers": {
                "eo":  {"price_label": s.access_price_eo_label,  "buy_url": s.access_buy_url_eo  or s.access_buy_url},
                "all": {"price_label": s.access_price_all_label, "buy_url": s.access_buy_url_all or s.access_buy_url},
            },
            "days_per_membership": s.access_days,
            "lifetime": s.access_days == 0}


class RedeemCode(BaseModel):
    code: str


@app.post("/access/redeem")
def access_redeem(body: RedeemCode, request: Request, response: Response) -> dict:
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        sid = _new_sid(); _set_session_cookie(response, sid)
    store = get_store()
    code_in = (body.code or "").strip()
    # ── MASTER UNLOCK PATH (per-tier codes) ──────────────────────────
    # Two master codes (set on each Selar product's delivery message): one for
    # the $5 Even/Odd tier, one for the $50 all-access tier. Legacy
    # master_unlock_code still grants all-access. Constant-time compare
    # (hmac.compare_digest) prevents timing-side-channel brute-force.
    import hmac as _hmac
    _s = get_settings()
    def _match(val: str) -> bool:
        val = (val or "").strip()
        return bool(val and code_in
                    and len(code_in) == len(val)
                    and _hmac.compare_digest(code_in.upper().encode(), val.upper().encode()))
    master_tier = None
    if _match(_s.master_code_all) or _match(_s.master_unlock_code):
        master_tier = "all"
    elif _match(_s.master_code_eo):
        master_tier = "eo"
    if master_tier:
        loginids: list[str] = []
        try:
            for a in store.list_accounts_public(sid):
                lid = a.get("deriv_account_id")
                if lid: loginids.append(lid)
        except Exception:
            pass
        if not loginids:
            raise HTTPException(400, "Connect a Deriv account first — the license binds to your loginid.")
        ref = f"master:{master_tier}:{sid[:16]}"
        code = store.mint_for_ref(ref, 0, loginids=loginids, tier=master_tier)   # days=0 ⇒ lifetime
        return {"ok": True, "lifetime": True, "tier": master_tier, "code": code, "bound": loginids,
                **store.access_status(sid, loginids=loginids)}
    # ── Standard redeem path with ANTI-SHARING enforcement ──
    # Collect the caller's Deriv loginids first — we need them BEFORE we redeem
    # so we can check ownership properly.
    loginids2: list[str] = []
    try:
        for a in store.list_accounts_public(sid):
            lid = a.get("deriv_account_id")
            if lid: loginids2.append(lid)
    except Exception:
        pass
    code_norm = (code_in or "").strip().upper()
    # Check existing bindings BEFORE redeeming — this is the anti-sharing gate.
    existing_bindings = store.loginids_for_license(code_norm)
    if existing_bindings and loginids2:
        # License is already locked to specific Deriv loginids — reject if the
        # caller doesn't own any of them.
        owns_any = any(lid in existing_bindings for lid in loginids2)
        if not owns_any:
            raise HTTPException(403,
                "This code is locked to a different Deriv account. "
                "It belongs to " + existing_bindings[0] + " — only that user can unlock with it.")
    elif existing_bindings and not loginids2:
        # Code is owned by someone, but the caller has no Deriv account on this
        # session. Tell them to connect first so we can verify ownership.
        raise HTTPException(400,
            "Connect your Deriv account first — this code is locked to a specific Deriv login.")
    # Now run the standard redeem (validates code exists + status + session).
    res = store.redeem_license(code_norm, sid)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "could not redeem code"))
    # If the license is an orphan (no bindings yet) and the caller has Deriv
    # accounts, adopt the code to them. This covers two cases:
    #   1. Master-unlock-code path (above) already binds at mint time.
    #   2. Legacy Stripe payments that fired with client_reference_id=null —
    #      the buyer can paste their own code from the Stripe receipt and the
    #      code adopts to their Deriv login. No support ticket needed.
    if loginids2 and not existing_bindings:
        try:
            store.bind_loginids_to_license(code_norm, loginids2)
        except Exception:
            pass
    return {**res, "code": code_norm, "bound": loginids2,
            **store.access_status(sid, loginids=loginids2)}


def _require_admin(request: Request) -> None:
    """Owner-only guard for license management. Disabled until ADMIN_KEY is set."""
    expected = get_settings().admin_key
    if not expected:
        raise HTTPException(403, "admin disabled — set ADMIN_KEY in the dashboard")
    got = request.headers.get("X-Admin-Key") or request.query_params.get("key", "")
    if got != expected:
        raise HTTPException(403, "bad admin key")


class MintLicenses(BaseModel):
    count: int = 1
    note: str | None = None


@app.post("/admin/licenses")
def admin_mint(body: MintLicenses, request: Request) -> dict:
    """Owner: mint membership codes (one per paying customer). Honest model —
    these unlock the TOOLS, not guaranteed wins."""
    _require_admin(request)
    days = get_settings().access_days
    codes = get_store().create_licenses(max(1, min(body.count, 1000)), days, body.note)
    return {"created": len(codes), "days": days, "codes": codes}


@app.get("/admin/licenses")
def admin_list(request: Request) -> list[dict]:
    _require_admin(request)
    return get_store().list_licenses()


def _verify_stripe_sig(payload: bytes, sig_header: str, secret: str, tolerance: int = 300) -> bool:
    """Verify a Stripe webhook signature with stdlib only (no `stripe` dep).
    Header form: 't=<ts>,v1=<hmac>,v1=<hmac>'. Valid if any v1 matches."""
    import hmac, hashlib, time
    if not sig_header or not secret:
        return False
    try:
        ts = None
        sigs = []
        for part in sig_header.split(","):
            k, _, v = part.partition("=")
            if k.strip() == "t":
                ts = v.strip()
            elif k.strip() == "v1":
                sigs.append(v.strip())
        if not ts or not sigs:
            return False
        if tolerance and abs(time.time() - int(ts)) > tolerance:
            return False
        expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload,
                            hashlib.sha256).hexdigest()
        return any(hmac.compare_digest(expected, s) for s in sigs)
    except Exception:
        return False


@app.post("/webhooks/stripe", include_in_schema=False)
async def stripe_webhook(request: Request) -> dict:
    """Stripe calls this on payment events. Verifies signature, then:
      • checkout.session.completed → mint LIFETIME license + bind to the
        buyer's Deriv loginids (passed as client_reference_id from the
        paywall — see /access/checkout_link). Idempotent on retries.
      • charge.refunded            → revoke that license (user goes back
        behind the paywall).
    Requires STRIPE_WEBHOOK_SECRET."""
    secret = get_settings().stripe_webhook_secret
    if not secret:
        raise HTTPException(503, "stripe webhook not configured")
    payload = await request.body()
    if not _verify_stripe_sig(payload, request.headers.get("Stripe-Signature", ""), secret):
        raise HTTPException(400, "bad signature")
    import json as _json
    try:
        event = _json.loads(payload)
    except Exception:
        raise HTTPException(400, "bad payload")
    etype = event.get("type") or ""
    obj = (event.get("data") or {}).get("object") or {}
    store = get_store()
    if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        sid = obj.get("id")
        paid = obj.get("payment_status") in ("paid", "no_payment_required") or obj.get("status") == "complete"
        if sid and paid:
            # `client_reference_id` carries a comma-joined list of the buyer's
            # Deriv loginids when they came through the J81 unlock button.
            # OPTIONAL — if missing, the buyer still gets their code by email
            # and can paste it in J81 to bind it on first use.
            cref = (obj.get("client_reference_id") or "").strip()
            loginids = [x.strip() for x in cref.split(",") if x.strip()] if cref else None
            # days=0 ⇒ LIFETIME (no expiry).
            code = store.mint_for_ref(f"stripe:{sid}", 0, loginids=loginids)
            # Always email the buyer their code so they have it no matter how
            # they got to Stripe (J81 button, shared link, etc.). The "Have a
            # code?" form will adopt it to their Deriv loginids on first use.
            email = ""
            try:
                cd = obj.get("customer_details") or {}
                email = (cd.get("email") or obj.get("customer_email") or "").strip()
            except Exception:
                pass
            mail_result = {"sent": False, "reason": "no email"}
            if email:
                try:
                    from app import email_sender
                    mail_result = email_sender.send_license_email(
                        email, code, loginids or [], sid)
                except Exception as exc:
                    mail_result = {"sent": False, "reason": "exception", "detail": repr(exc)[:160]}
            return {"ok": True, "issued": True, "code": code,
                    "bound": len(loginids or []),
                    "emailed_to": email if mail_result.get("sent") else None,
                    "email_status": mail_result}
        return {"ok": True, "issued": False, "reason": "not paid"}
    if etype in ("charge.refunded", "charge.dispute.closed"):
        # The refund payload has `payment_intent`; the related checkout session
        # carries the payment_intent in `payment_intent`. We stored the license
        # under `stripe:<session_id>`. Easiest path: read the linked session id
        # off the charge if present; otherwise mark any license whose note
        # starts with stripe: AND whose stored payment_intent matches.
        # Stripe puts the session id sometimes in `metadata.checkout_session`
        # — but the safest universal hook is to revoke by payment_intent ref.
        pi = obj.get("payment_intent")
        revoked = False
        if pi:
            # We didn't store payment_intent; fall back: also try by session id
            # if the dashboard rule passes it in metadata.
            meta = obj.get("metadata") or {}
            cs = meta.get("checkout_session")
            if cs:
                revoked = store.revoke_license_by_ref(f"stripe:{cs}") or revoked
        return {"ok": True, "revoked": revoked}
    return {"ok": True, "ignored": etype}


# ─────────────────────────────────────────────────────────────────────
# INTASEND WEBHOOK — Kenyan fintech, primary processor
# ─────────────────────────────────────────────────────────────────────
@app.post("/webhooks/intasend", include_in_schema=False)
async def intasend_webhook(request: Request) -> dict:
    """IntaSend posts here when a checkout completes.

    Verification: IntaSend sends a `challenge` field in the body (or the
    `X-IntaSend-Signature` header on newer deliveries). We compare it to
    INTASEND_WEBHOOK_CHALLENGE that you configured in the dashboard.

    Successful state="COMPLETE" → mint LIFETIME license keyed to
    `intasend:<api_ref>` (idempotent), bind to the buyer's Deriv loginids
    from meta_data, auto-redeem under their session_id if present, and
    email them the code via Resend.

    Failed/cancelled states → acknowledge and ignore.
    """
    s = get_settings()
    expected = (s.intasend_webhook_challenge or "").strip()
    if not expected:
        raise HTTPException(503, "intasend webhook not configured (set INTASEND_WEBHOOK_CHALLENGE)")
    import json as _json
    try:
        body = _json.loads(await request.body())
    except Exception:
        raise HTTPException(400, "bad payload")
    # IntaSend supports two verification styles — accept either.
    got_challenge = (body.get("challenge") or "").strip()
    got_header = (request.headers.get("X-IntaSend-Signature") or "").strip()
    if got_challenge != expected and got_header != expected:
        raise HTTPException(401, "bad webhook signature")
    state = (body.get("state") or "").upper()
    api_ref = (body.get("api_ref") or "").strip()
    store = get_store()
    if state == "COMPLETE" and api_ref:
        meta = body.get("meta_data") or body.get("metadata") or {}
        cref = (meta.get("loginids") or "").strip()
        loginids = [x.strip() for x in cref.split(",") if x.strip()] if cref else None
        sid_meta = (meta.get("session_id") or "").strip()
        # days=0 ⇒ LIFETIME. Idempotent ref → retries return the same code.
        code = store.mint_for_ref(f"intasend:{api_ref}", 0, loginids=loginids)
        if sid_meta:
            try:
                store.redeem_license(code, sid_meta)
            except Exception:
                pass
        # Email the buyer their code regardless — they may close the tab.
        email = ""
        try:
            customer = body.get("customer") or {}
            email = (customer.get("email") or body.get("email") or "").strip()
        except Exception:
            pass
        mail_result = {"sent": False, "reason": "no email"}
        if email and "@" in email:
            try:
                from app import email_sender
                mail_result = email_sender.send_license_email(
                    email, code, loginids or [], api_ref)
            except Exception as exc:
                mail_result = {"sent": False, "reason": "exception", "detail": repr(exc)[:160]}
        return {"ok": True, "issued": True, "code": code,
                "bound": len(loginids or []),
                "emailed_to": email if mail_result.get("sent") else None,
                "email_status": mail_result}
    if state in ("FAILED", "CANCELLED", "PENDING", "PROCESSING"):
        return {"ok": True, "issued": False, "reason": "state=" + state}
    return {"ok": True, "ignored": True, "state": state}


# ─────────────────────────────────────────────────────────────────────
# SELAR WEBHOOK — current live processor (M-Pesa + cards via Selar)
# ─────────────────────────────────────────────────────────────────────
# Map a paid amount to a license tier: >=50 ⇒ all-access, >=1 ⇒ Even/Odd only.
# Missing/odd amounts default to "all" so a real buyer is never under-granted.
def _tier_from_amount(v) -> str:
    try:
        a = float(str(v).replace(",", "").strip())
    except Exception:
        return "all"
    if a >= 50: return "all"
    if a >= 1:  return "eo"
    return "all"


def _selar_tier(data) -> str:
    """Tier from a Selar order by PRODUCT NAME — currency-proof (the order can
    report KES or USD, so amount alone is ambiguous with dual pricing).
    Check the ALL keywords FIRST so a stray 'Even/Odd' in the all-access
    description can't mis-tier it. Products:
    'J81 Even/Odd Access' (eo) vs 'J81 Lifetime — All Trade Types' (all)."""
    import json as _j
    try:
        hay = _j.dumps(data).lower()
    except Exception:
        hay = str(data).lower()
    if "lifetime" in hay or "all trade" in hay or "all access" in hay:
        return "all"
    if "even" in hay or "odd" in hay:
        return "eo"
    # Rare fallback — amount hint: KES ~6500 or USD ~50 ⇒ all, else eo (least privilege).
    try:
        amt = float(str(data.get("amount") or data.get("total") or data.get("price")
                        or (data.get("order") or {}).get("amount") or 0).replace(",", "").strip())
    except Exception:
        amt = 0.0
    return "all" if (amt >= 6000 or 40 <= amt <= 60) else "eo"


@app.post("/webhooks/selar", include_in_schema=False)
async def selar_webhook(request: Request) -> dict:
    """Selar posts here when an order is completed.

    Verification: Selar signs the body with HMAC-SHA256 using
    SELAR_WEBHOOK_SECRET and sends the hex digest in the `x-selar-signature`
    header. We also accept a `secret` field in the JSON body as a fallback
    (Selar's older webhook style).

    On successful_purchase / paid status, we:
      1. Mint a LIFETIME license keyed to `selar:<order_id>` (idempotent —
         retries return the same code).
      2. Email the buyer the code via Resend so they get it instantly.

    The customer pastes the code back in J81 → it auto-binds to their
    Deriv loginids at redeem time (existing orphan-adoption flow).
    """
    s = get_settings()
    secret = (s.selar_webhook_secret or "").strip()
    if not secret:
        raise HTTPException(503, "selar webhook not configured (set SELAR_WEBHOOK_SECRET)")
    payload = await request.body()
    # ── Signature verification ─────────────────────────────────
    import hmac as _hmac
    import hashlib as _hashlib
    import json as _json
    expected = _hmac.new(secret.encode(), payload, _hashlib.sha256).hexdigest()
    got_header = (request.headers.get("x-selar-signature")
                  or request.headers.get("X-Selar-Signature") or "").strip()
    sig_ok = bool(got_header) and _hmac.compare_digest(got_header, expected)
    # Parse body now (also enables the fallback secret check)
    try:
        body = _json.loads(payload) if payload else {}
    except Exception:
        raise HTTPException(400, "bad payload")
    if not sig_ok:
        # Fallback: shared-secret field in the body (older Selar webhook style)
        body_secret = (body.get("secret") or "").strip() if isinstance(body, dict) else ""
        if not body_secret or body_secret != secret:
            raise HTTPException(401, "bad webhook signature")
    # ── Extract order details ──────────────────────────────────
    event_type = (body.get("event") or body.get("event_type") or "").lower()
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    status = (data.get("status") or data.get("payment_status") or "").lower()
    order_id = (data.get("order_id") or data.get("id")
                or data.get("reference") or data.get("transaction_id") or "")
    order_id = str(order_id).strip()
    if not order_id:
        return {"ok": True, "ignored": True, "reason": "no order id"}
    # Buyer email — Selar nests this in customer{} or buyer{} depending on event
    customer = (data.get("customer") if isinstance(data.get("customer"), dict)
                else data.get("buyer") if isinstance(data.get("buyer"), dict) else {})
    email = (customer.get("email") or data.get("email")
             or data.get("customer_email") or "").strip()
    # Optional: any custom checkout-form fields end up in data.get("custom_fields")
    # or data.get("answers"); preserved for potential future binding hints.
    store = get_store()
    # ── Only act on successful payments ────────────────────────
    is_success = (event_type in ("successful_purchase", "order.paid",
                                  "transaction.success", "payment.successful")
                  or status in ("successful", "completed", "paid", "success"))
    if is_success:
        # days=0 ⇒ LIFETIME. Idempotent ref means retries hit the same code.
        code = store.mint_for_ref(f"selar:{order_id}", 0, loginids=None, tier=_selar_tier(data))
        mail_result = {"sent": False, "reason": "no email"}
        if email and "@" in email:
            try:
                from app import email_sender
                mail_result = email_sender.send_license_email(
                    email, code, [], order_id)
            except Exception as exc:
                mail_result = {"sent": False, "reason": "exception",
                               "detail": repr(exc)[:160]}
        return {"ok": True, "issued": True, "code": code,
                "emailed_to": email if mail_result.get("sent") else None,
                "email_status": mail_result}
    # Failed / cancelled / refunded — acknowledge so Selar stops retrying.
    if event_type in ("refund.completed", "order.refunded") or status == "refunded":
        revoked = store.revoke_license_by_ref(f"selar:{order_id}")
        return {"ok": True, "revoked": bool(revoked), "order_id": order_id}
    return {"ok": True, "ignored": True, "event_type": event_type, "status": status}


# ─────────────────────────────────────────────────────────────────────
# FLUTTERWAVE WEBHOOK — fallback (kept for compatibility)
# ─────────────────────────────────────────────────────────────────────
@app.post("/webhooks/flutterwave", include_in_schema=False)
async def flutterwave_webhook(request: Request) -> dict:
    """Flutterwave posts here when a charge completes (successful or failed).

    Verification:  the `verif-hash` HTTP header must match FLW_SECRET_HASH
    that you configured in the Flutterwave dashboard → Webhooks → Settings.

    On a successful `charge.completed` (status=successful), we:
      1. Mint a LIFETIME license keyed to `flw:<tx_ref>` (idempotent on retry).
      2. Bind it to the buyer's Deriv loginids (carried in meta from the
         /access/checkout_link call).
      3. Email the buyer the license code so they can paste it in J81.

    On a refund (`refund.completed`), we revoke the license tied to that
    tx_ref so the user lands back on the paywall.
    """
    s = get_settings()
    expected = (s.flw_secret_hash or "").strip()
    if not expected:
        raise HTTPException(503, "flutterwave webhook not configured (set FLW_SECRET_HASH)")
    got = (request.headers.get("verif-hash") or "").strip()
    if not got or got != expected:
        raise HTTPException(401, "bad webhook signature")
    import json as _json
    try:
        event = _json.loads(await request.body())
    except Exception:
        raise HTTPException(400, "bad payload")
    etype = (event.get("event") or event.get("event.type") or "").lower()
    data = event.get("data") or {}
    status = (data.get("status") or "").lower()
    tx_ref = (data.get("tx_ref") or "").strip()
    store = get_store()
    # Successful charge → mint + bind + email.
    if etype in ("charge.completed",) and status == "successful" and tx_ref:
        meta = data.get("meta") or {}
        # `meta.loginids` is a comma-joined list (set in /access/checkout_link).
        # `meta.session_id` lets the user be auto-unlocked when they return.
        cref = (meta.get("loginids") or "").strip()
        loginids = [x.strip() for x in cref.split(",") if x.strip()] if cref else None
        sid_meta = (meta.get("session_id") or "").strip()
        # days=0 ⇒ LIFETIME (no expiry). Idempotent ref means a retry returns
        # the same code without re-minting.
        code = store.mint_for_ref(f"flw:{tx_ref}", 0, loginids=loginids)
        # If we have the buyer's J81 session, also redeem the code under that
        # session so they're auto-unlocked when they return from Flutterwave.
        if sid_meta:
            try:
                store.redeem_license(code, sid_meta)
            except Exception:
                pass
        # Email the code regardless — they may close the tab before redirect.
        email = ""
        try:
            cust = data.get("customer") or {}
            email = (cust.get("email") or "").strip()
        except Exception:
            pass
        mail_result = {"sent": False, "reason": "no email"}
        if email and "@" in email:
            try:
                from app import email_sender
                mail_result = email_sender.send_license_email(
                    email, code, loginids or [], tx_ref)
            except Exception as exc:
                mail_result = {"sent": False, "reason": "exception", "detail": repr(exc)[:160]}
        return {"ok": True, "issued": True, "code": code,
                "bound": len(loginids or []),
                "emailed_to": email if mail_result.get("sent") else None,
                "email_status": mail_result}
    # Failed charge — no license to issue, just acknowledge.
    if etype in ("charge.completed",) and status != "successful":
        return {"ok": True, "issued": False, "reason": "charge status=" + status}
    # Refund — revoke the license bound to this tx_ref.
    if etype in ("refund.completed", "transaction.refund.completed") and tx_ref:
        revoked = store.revoke_license_by_ref(f"flw:{tx_ref}")
        return {"ok": True, "revoked": bool(revoked), "tx_ref": tx_ref}
    return {"ok": True, "ignored": etype, "status": status}


@app.get("/access/code")
def access_code(session_id: str) -> dict:
    """The buyer's success page calls this with the processor's session/tx id
    to fetch the code minted for their payment. The id is their one-time
    proof of purchase. Looks up both Stripe (stripe:<sid>) and Flutterwave
    (flw:<tx_ref>) refs since either webhook may have minted it."""
    store = get_store()
    # Try every processor's ref format — Selar (current live), IntaSend,
    # Flutterwave, Stripe (legacy). Whoever minted it, we'll find it.
    lic = (store.license_by_note(f"selar:{session_id}")
           or store.license_by_note(f"intasend:{session_id}")
           or store.license_by_note(f"flw:{session_id}")
           or store.license_by_note(f"stripe:{session_id}"))
    if not lic:
        return {"ready": False}
    return {"ready": True, "code": lic["code"]}


class OwnerUnlockReq(BaseModel):
    key: str | None = None


@app.post("/access/owner_unlock")
def access_owner_unlock(body: OwnerUnlockReq, request: Request, response: Response) -> dict:
    """Owner escape hatch — grants a LIFETIME license to the caller's Deriv
    loginids without going through Stripe. Requires ADMIN_KEY (header
    X-Admin-Key, query ?key=, or body field `key`). Use this to test the
    unlocked version of the app, or to grant yourself + close contacts
    comp access. Disabled until ADMIN_KEY is set in the env."""
    expected = (get_settings().admin_key or "").strip()
    if not expected:
        raise HTTPException(503, "owner unlock disabled — set ADMIN_KEY in the dashboard first")
    got = (request.headers.get("X-Admin-Key")
           or request.query_params.get("key")
           or (body.key if body else None) or "").strip()
    if got != expected:
        raise HTTPException(403, "bad admin key")
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        sid = _new_sid(); _set_session_cookie(response, sid)
    store = get_store()
    loginids: list[str] = []
    try:
        for a in store.list_accounts_public(sid):
            lid = a.get("deriv_account_id")
            if lid: loginids.append(lid)
    except Exception:
        pass
    if not loginids:
        raise HTTPException(400, "connect a Deriv account first — the license binds to your loginids")
    # Lifetime mint + bind. Reusing the same ref idempotently means re-calling
    # this never duplicates the license; it just re-binds new loginids.
    ref = f"owner:{sid[:16]}"
    code = store.mint_for_ref(ref, 0, loginids=loginids)
    return {"ok": True, "code": code, "bound": loginids, "lifetime": True}


@app.get("/access/unlock_self")
def access_unlock_self(request: Request, response: Response) -> dict:
    """Owner-only escape hatch — GRANTS a lifetime license to the caller's
    Deriv loginids. Mirror of /access/owner_unlock but as a GET so you can
    hit it from the browser address bar without any form. Use:
        /access/unlock_self?key=<ADMIN_KEY>
    """
    expected = (get_settings().admin_key or "").strip()
    if not expected:
        raise HTTPException(503, "unlock disabled — set ADMIN_KEY in the dashboard first")
    got = (request.headers.get("X-Admin-Key") or request.query_params.get("key") or "").strip()
    if got != expected:
        raise HTTPException(403, "bad admin key")
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        sid = _new_sid(); _set_session_cookie(response, sid)
    store = get_store()
    loginids: list[str] = []
    try:
        for a in store.list_accounts_public(sid):
            lid = a.get("deriv_account_id")
            if lid: loginids.append(lid)
    except Exception:
        pass
    if not loginids:
        raise HTTPException(400, "connect a Deriv account first — the license binds to your loginids")
    ref = f"owner:{sid[:16]}"
    code = store.mint_for_ref(ref, 0, loginids=loginids)
    return {"ok": True, "code": code, "bound": loginids, "lifetime": True,
            "note": "Refresh the J81 site — you're unlocked."}


@app.get("/admin/revoke_loginid")
def admin_revoke_loginid(request: Request) -> dict:
    """Admin tool — revoke every active license bound to a SPECIFIC Deriv
    loginid, regardless of which session is asking. Useful for refunds,
    testing on a fresh browser, or banning abusers. Pass:
        /admin/revoke_loginid?key=<ADMIN_KEY>&loginid=DOT91992522
    Returns {revoked: n, loginid: "..."}.
    """
    expected = (get_settings().admin_key or "").strip()
    if not expected:
        raise HTTPException(503, "admin tools disabled — set ADMIN_KEY first")
    got = (request.headers.get("X-Admin-Key") or request.query_params.get("key") or "").strip()
    if got != expected:
        raise HTTPException(403, "bad admin key")
    lid = (request.query_params.get("loginid") or "").strip().upper()
    if not lid:
        raise HTTPException(400, "missing ?loginid=DOTxxxx parameter")
    n = get_store().revoke_licenses_for_loginids([lid])
    return {"ok": True, "revoked": n, "loginid": lid,
            "note": f"All active licenses bound to {lid} are now revoked. "
                    "Tombstones cleared too, so a fresh payment can re-bind cleanly."}


@app.get("/access/revoke_self")
def access_revoke_self(request: Request) -> dict:
    """Owner-only escape hatch — revokes every license bound to the caller's
    Deriv loginids so the paywall fires again on the next visit. Used for
    testing the payment flow when you've previously unlocked yourself with
    the master code or owner unlock. Requires ADMIN_KEY (?key=...)."""
    expected = (get_settings().admin_key or "").strip()
    if not expected:
        raise HTTPException(503, "revoke disabled — set ADMIN_KEY in the dashboard first")
    got = (request.headers.get("X-Admin-Key") or request.query_params.get("key") or "").strip()
    if got != expected:
        raise HTTPException(403, "bad admin key")
    sid = request.cookies.get(SESSION_COOKIE) or ""
    store = get_store()
    loginids: list[str] = []
    try:
        for a in store.list_accounts_public(sid):
            lid = a.get("deriv_account_id")
            if lid: loginids.append(lid)
    except Exception:
        pass
    if not loginids:
        raise HTTPException(400, "no Deriv account connected on this session — nothing to revoke")
    n = store.revoke_licenses_for_loginids(loginids)
    return {"ok": True, "revoked": n, "loginids": loginids,
            "note": "Refresh the J81 site; you'll see the paywall again."}


@app.get("/access/checkout_link")
async def access_checkout_link(request: Request) -> dict:
    """Generate a hosted checkout URL for this user's Deriv loginids.

    Provider selection:
      • If FLW_SECRET_KEY is set → Flutterwave (M-Pesa + cards, Kenya-friendly).
        Each call creates a unique transaction reference; meta carries the
        loginids + session_id so the webhook can bind the license correctly.
      • Else if ACCESS_BUY_URL is set → legacy Stripe Payment Link path.
      • Else → returns reason=buy_url not configured.
    The frontend just opens whatever URL we return.
    """
    s = get_settings()
    sid = request.cookies.get(SESSION_COOKIE) or ""
    loginids: list[str] = []
    if sid:
        try:
            for a in get_store().list_accounts_public(sid):
                lid = a.get("deriv_account_id")
                if lid: loginids.append(lid)
        except Exception:
            pass
    if not loginids:
        # No Deriv accounts yet — the buyer would pay but we couldn't bind the
        # license. Send them back to connect first.
        return {"url": None, "reason": "connect_first"}

    # ── INTASEND PATH (primary for Kenya — M-Pesa + cards) ─────────
    if (s.intasend_secret_key or "").strip():
        import time as _time
        import secrets as _secrets
        import httpx
        api_ref = f"j81-{_secrets.token_hex(8)}-{int(_time.time())}"
        host = request.headers.get("host", "")
        scheme = request.headers.get("x-forwarded-proto", "https")
        site_url = f"{scheme}://{host}" if host else "https://j81-trade-desk.onrender.com"
        redirect_url = f"{site_url}/?paid=intasend&ref={api_ref}"
        base = ("https://payment.intasend.com" if s.intasend_live
                else "https://sandbox.intasend.com")
        payload = {
            "public_key": (s.intasend_publishable_key or "").strip() or None,
            "amount": 100,
            "currency": "USD",
            "redirect_url": redirect_url,
            "api_ref": api_ref,
            "comment": "J81 Trade Desk · Lifetime access · " + ", ".join(loginids[:2]),
            "wallet_id": None,
            # Custom metadata round-trips on the webhook.
            "meta_data": {
                "session_id": sid,
                "loginids": ",".join(loginids[:20]),
            },
            # method": let buyer choose at checkout (M-PESA / CARD-PAYMENT / BANK)
        }
        # Drop None values (IntaSend rejects null public_key etc.)
        payload = {k: v for k, v in payload.items() if v is not None}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(
                    f"{base}/api/v1/checkout/",
                    headers={
                        "Authorization": f"Bearer {s.intasend_secret_key.strip()}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        except Exception as exc:
            log.warning("intasend checkout request failed: %r", exc)
            return {"url": None, "reason": "intasend_request_failed",
                    "detail": repr(exc)[:160]}
        # IntaSend returns the hosted checkout URL on success.
        url = data.get("url") or (data.get("checkout") or {}).get("url")
        if not url or r.status_code >= 400:
            log.warning("intasend checkout error: status=%s body=%s",
                        r.status_code, str(data)[:200])
            return {"url": None, "reason": "intasend_error",
                    "status": r.status_code, "detail": str(data)[:200]}
        return {"url": url, "provider": "intasend",
                "bound": loginids[:20], "api_ref": api_ref}

    # ── FLUTTERWAVE PATH (fallback if IntaSend not configured) ──────
    if (s.flw_secret_key or "").strip():
        import time as _time
        import secrets as _secrets
        import httpx
        tx_ref = f"j81-{_secrets.token_hex(8)}-{int(_time.time())}"
        # Build the return URL — Flutterwave appends ?status=…&tx_ref=…&transaction_id=…
        host = request.headers.get("host", "")
        scheme = request.headers.get("x-forwarded-proto", "https")
        default_redirect = f"{scheme}://{host}/?paid=flw" if host else "https://j81-trade-desk.onrender.com/?paid=flw"
        redirect_url = (s.flw_redirect_url or default_redirect)
        payload = {
            "tx_ref": tx_ref,
            "amount": "100",
            "currency": "USD",
            "redirect_url": redirect_url,
            "payment_options": "card,mpesa,banktransfer,ussd",
            "customer": {
                # Flutterwave checkout will prompt for the real email; this is
                # just a placeholder identifier tied to the loginids.
                "email": f"trader+{tx_ref[:8]}@j81.app",
                "name": ", ".join(loginids[:2]),
            },
            "customizations": {
                "title": "J81 Trade Desk",
                "description": "Lifetime access · " + ", ".join(loginids[:2]),
            },
            "meta": {
                "session_id": sid,
                "loginids": ",".join(loginids[:20]),
            },
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(
                    "https://api.flutterwave.com/v3/payments",
                    headers={
                        "Authorization": f"Bearer {s.flw_secret_key.strip()}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        except Exception as exc:
            log.warning("flutterwave checkout request failed: %r", exc)
            return {"url": None, "reason": "flw_request_failed", "detail": repr(exc)[:160]}
        if (data.get("status") or "").lower() != "success":
            log.warning("flutterwave checkout error: %s", data.get("message", "no detail"))
            return {"url": None, "reason": "flw_error",
                    "detail": str(data.get("message") or "")[:200]}
        link = ((data.get("data") or {}).get("link"))
        if not link:
            return {"url": None, "reason": "flw_no_link"}
        return {"url": link, "provider": "flutterwave",
                "bound": loginids[:20], "tx_ref": tx_ref}

    # ── STRIPE FALLBACK ─────────────────────────────────────────────
    base = s.access_buy_url
    if not base:
        return {"url": None, "reason": "buy_url not configured"}
    # Stripe Payment Links accept client_reference_id as a query param. The
    # buyer's checkout will send it back on the completed webhook so we can
    # bind the license to those exact loginids.
    sep = "&" if "?" in base else "?"
    url = base + sep + "client_reference_id=" + ",".join(loginids[:20])
    return {"url": url, "provider": "stripe", "bound": loginids[:20]}


@app.get("/robots.txt", include_in_schema=False)
def robots() -> Response:
    """Let search engines crawl the public app; keep API/OAuth paths out of the
    index (they're not pages)."""
    body = ("User-agent: *\n"
            "Allow: /$\n"
            "Disallow: /oauth/\n"
            "Disallow: /accounts\n"
            "Disallow: /trade/\n"
            "Sitemap: https://j81-trade-desk.onrender.com/sitemap.xml\n")
    return Response(body, media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap() -> Response:
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            '  <url><loc>https://j81-trade-desk.onrender.com/</loc>'
            '<changefreq>weekly</changefreq><priority>1.0</priority></url>\n'
            '</urlset>\n')
    return Response(body, media_type="application/xml")


@app.get("/og-image.svg", include_in_schema=False)
def og_image() -> Response:
    """Branded 1200×630 link-preview card (SVG — self-contained, no binary asset).
    Rendered by Slack/Discord/LinkedIn; X/Facebook may fall back to text."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">'
        '<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#090d16"/><stop offset="1" stop-color="#0d1424"/></linearGradient>'
        '<linearGradient id="gold" x1="0" y1="0" x2="1" y2="0">'
        '<stop offset="0" stop-color="#ffeaa6"/><stop offset="1" stop-color="#d39f2c"/></linearGradient></defs>'
        '<rect width="1200" height="630" fill="url(#bg)"/>'
        '<rect x="64" y="64" width="120" height="120" rx="28" fill="#080709"/>'
        '<text x="124" y="148" font-family="Georgia,serif" font-size="56" font-weight="800" '
        'text-anchor="middle" fill="#f0c64a">J81</text>'
        '<text x="220" y="150" font-family="Georgia,serif" font-size="64" font-weight="800" '
        'fill="url(#gold)">Trade Desk</text>'
        '<text x="66" y="330" font-family="Helvetica,Arial,sans-serif" font-size="52" font-weight="700" '
        'fill="#eef3fb">AI-assisted trading on Deriv</text>'
        '<text x="66" y="398" font-family="Helvetica,Arial,sans-serif" font-size="30" '
        'fill="#9fb6c5">Live charts · one-tap connect · auto-trading · self-testing strategy engine</text>'
        '<rect x="66" y="470" width="320" height="64" rx="14" fill="url(#gold)"/>'
        '<text x="226" y="512" font-family="Helvetica,Arial,sans-serif" font-size="28" font-weight="800" '
        'text-anchor="middle" fill="#2a1f05">Connect with Deriv</text>'
        '<text x="66" y="588" font-family="Helvetica,Arial,sans-serif" font-size="22" '
        'fill="#6f7d8c">Trading carries risk · synthetics are an audited RNG with a house edge</text>'
        '</svg>'
    )
    return Response(svg, media_type="image/svg+xml")


@app.get("/version")
def version() -> dict:
    """Tiny health-pulse endpoint. Front-end pings every 12s to colour the
    nav-bar API status dot. Cheap on purpose — just confirms the server
    is alive and what version is running."""
    return {"app": APP_NAME, "version": APP_VERSION, "ok": True}


@app.get("/health")
def health() -> dict:
    s = get_settings()
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "status": "ok",
        "dry_run": s.dry_run,
        "deriv_app_registered": bool(s.deriv_app_id),
        "encryption_configured": bool(s.bot_encryption_key),
        "markup_percent": s.deriv_markup_percent,
        "referral_url": s.deriv_referral_url,
        "oauth_ready": bool(s.deriv_app_id),
    }


@app.get("/health/tree")
def health_tree() -> dict:
    """Single-bot health check. The 'tree' is now this one process — keeping
    the shape the UI expects so the engines panel keeps rendering."""
    s = get_settings()
    store = get_store()
    accts = store.list_accounts_public()
    enabled = [a for a in accts if a["enabled"]]
    proven = store.list_proven_strategies(limit=1000)
    return {
        "bot": {"ok": True, "version": APP_VERSION, "dry_run": s.dry_run,
                "loop_alive": trading_loop.status.get("loop_alive"),
                "cycles": trading_loop.status.get("cycles"),
                "last_error": trading_loop.status.get("last_error"),
                "accounts": len(accts), "enabled": len(enabled),
                "proven_strategies": len(proven)},
        "analyser": {"configured": True, "ok": True, "ms": 0,
                     "note": "in-process (single-bot edition)"},
        "researcher": {"configured": False,
                       "note": "researcher branch cut from tree"},
        "cycle": {"tested": 0, "proven": 0, "next_in_seconds": None},
    }


def _researcher_url() -> str:
    """Kept for backwards-compat in any caller that still references it; always
    returns an empty string since the researcher is no longer in the tree."""
    return ""


# ---------------------------------------------------------------------------
# OAuth flow — users authorize our app to trade on their Deriv account
# ---------------------------------------------------------------------------


# PKCE state store: state -> (code_verifier, created_at). Server-side because
# J81's OAuth is server-rendered (Twinmil does the equivalent client-side).
import base64 as _b64
import hashlib as _hashlib
import secrets as _secrets
import time as _time

_PKCE_STATES: dict[str, tuple[str, float]] = {}


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for OAuth2 PKCE (S256)."""
    verifier = _secrets.token_urlsafe(64)[:96]
    challenge = _b64.urlsafe_b64encode(
        _hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


def _prune_pkce(ttl: float = 600.0) -> None:
    now = _time.time()
    for k in [k for k, (_, t) in _PKCE_STATES.items() if now - t > ttl]:
        _PKCE_STATES.pop(k, None)


@app.get("/oauth/start", include_in_schema=False)
def oauth_start() -> RedirectResponse:
    """Bounce the user to Deriv's authorize page. Uses OAuth2 + PKCE (the new
    platform) when DERIV_OAUTH_TOKEN_URL is configured, otherwise the legacy
    flow. Either way Deriv redirects back to /oauth/callback, which handles both."""
    s = get_settings()
    if not s.deriv_app_id:
        raise HTTPException(
            500,
            "DERIV_APP_ID is not set — register an app at api.deriv.com/dashboard first",
        )
    if s.deriv_oauth_token_url:  # NEW platform: OAuth2 + PKCE
        verifier, challenge = _pkce_pair()
        state = _secrets.token_urlsafe(24)
        _PKCE_STATES[state] = (verifier, _time.time())
        _prune_pkce()
        # NOTE: this Deriv app is NOT allowed to request `offline_access` (Ory
        # rejects it: "client is not allowed to request scope 'offline_access'"),
        # so we send ONLY the configured scope. Without a refresh token the
        # access token expires (~1h) and the user reconnects via the graceful
        # "Reconnect Deriv ↻" prompt. The refresh machinery stays dormant in case
        # the app's capabilities are later enabled in the Deriv dashboard.
        q = urlencode({
            "response_type": "code",
            "client_id": s.deriv_app_id,
            "redirect_uri": s.deriv_oauth_redirect_uri,
            "scope": s.deriv_oauth_scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        return RedirectResponse(f"{s.deriv_oauth_authorize_url}?{q}")
    # LEGACY platform: app_id only; Deriv returns token1/acct1/cur1.
    q = urlencode({"app_id": s.deriv_app_id})
    return RedirectResponse(f"{s.deriv_oauth_authorize_url}?{q}")


def _auth_ok(n: int) -> RedirectResponse:
    """Bounce the freshly-connected user back into the app UI (loading → home)."""
    return RedirectResponse(f"/?connected={n}", status_code=303)


def _auth_fail(msg: str) -> RedirectResponse:
    """Bounce to the connect screen with a friendly message instead of a raw error."""
    return RedirectResponse(f"/?auth_error={urlquote(msg)}", status_code=303)


@app.get("/oauth/callback", include_in_schema=False)
async def oauth_callback(request: Request) -> RedirectResponse:
    """After Deriv authorizes the user, save their account(s) and redirect them
    back into the app (loading → home). Any failure redirects to the connect
    screen with a readable message rather than dumping JSON or an error page.

    Routing (like Twinmil's getInitialView):
      * ?error=…                 → surface it on the connect screen
      * ?token1=…&acct1=…&cur1=… → LEGACY (tokens straight in the URL)
      * ?code=…&state=…          → NEW OAuth2 PKCE (exchange code for a token)
    """
    params = dict(request.query_params)
    store = get_store()
    # Bind the connected account(s) to THIS browser session.
    sid = request.cookies.get(SESSION_COOKIE) or _new_sid()

    def _ok(n: int) -> RedirectResponse:
        resp = _auth_ok(n); _set_session_cookie(resp, sid); return resp

    if params.get("error"):
        return _auth_fail(params.get("error_description") or params["error"])

    # ---- LEGACY: token1/acct1/cur1 triples ----
    if "token1" in params:
        saved = 0
        loginids: list[str] = []
        i = 1
        while f"token{i}" in params and f"acct{i}" in params:
            try:
                store.upsert_account(
                    deriv_account_id=params[f"acct{i}"],
                    token=params[f"token{i}"],
                    currency=params.get(f"cur{i}"),
                    session_id=sid,
                )
                loginids.append(params[f"acct{i}"])
                saved += 1
            except RuntimeError as exc:
                return _auth_fail(f"Could not store your token: {exc}")
            i += 1
        if not saved:
            return _auth_fail("No account came back from Deriv — the sign-in may have been cancelled.")
        # Anti-sharing: if ANY of these loginids is already on an active
        # license (paid earlier on another device), the new connection adds
        # any new loginids to the SAME license so the user stays unlocked.
        try:
            existing = store.license_by_loginid_any(loginids)
            if existing:
                store.bind_loginids_to_license(existing["code"], loginids)
        except Exception:
            pass
        # CLAIM-PENDING: if the user redeemed a license code BEFORE connecting
        # Deriv (paid via email-only flow), the license is bound to this
        # session but has no loginids yet. Bind them now so /access/status
        # immediately reports licensed=true on the next request.
        try:
            pending = store.license_by_session(sid)
            if pending:
                store.bind_loginids_to_license(pending["code"], loginids)
        except Exception:
            pass
        return _ok(saved)

    # ---- NEW: OAuth2 PKCE code exchange ----
    if "code" in params:
        s = get_settings()
        state = params.get("state")
        entry = _PKCE_STATES.pop(state, None) if state else None
        if not entry:
            return _auth_fail("Your sign-in session expired — please connect again.")
        if not s.deriv_oauth_token_url:
            return _auth_fail("Server is missing DERIV_OAUTH_TOKEN_URL — cannot finish sign-in.")
        verifier = entry[0]
        import httpx
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.post(s.deriv_oauth_token_url, data={
                    "grant_type": "authorization_code",
                    "code": params["code"],
                    "redirect_uri": s.deriv_oauth_redirect_uri,
                    "client_id": s.deriv_app_id,
                    "code_verifier": verifier,
                })
                r.raise_for_status()
                tok = r.json()
        except Exception as exc:
            return _auth_fail(f"Could not finish sign-in with Deriv: {exc}")
        access = tok.get("access_token")
        if not access:
            return _auth_fail("Deriv did not return an access token — please try again.")
        refresh = tok.get("refresh_token")          # present iff offline_access granted
        expires_in = tok.get("expires_in")
        # New-platform token: read the user's accounts (demo + real) from the
        # new Options API, NOT the legacy v3 authorize (which rejects this token).
        from app import deriv_new
        try:
            accounts = await deriv_new.list_accounts(access)
        except Exception as exc:
            return _auth_fail(f"Signed in, but could not read your accounts: {exc}")
        saved = 0
        loginids: list[str] = []
        for a in accounts:
            try:
                store.upsert_account(
                    deriv_account_id=a["loginid"], token=access,
                    currency=a.get("currency"), platform="new", session_id=sid,
                    refresh_token=refresh, expires_in=expires_in)
                loginids.append(a["loginid"])
                saved += 1
            except RuntimeError as exc:
                return _auth_fail(f"Could not store your account: {exc}")
        if not saved:
            return _auth_fail("Signed in, but found no tradable accounts.")
        # Returning paid customer? If ANY loginid is on an active license,
        # adopt that license + bind any newly-added loginids to it.
        try:
            existing = store.license_by_loginid_any(loginids)
            if existing:
                store.bind_loginids_to_license(existing["code"], loginids)
        except Exception:
            pass
        # CLAIM-PENDING: license redeemed by this session BEFORE Deriv was
        # connected → bind the new loginids now.
        try:
            pending = store.license_by_session(sid)
            if pending:
                store.bind_loginids_to_license(pending["code"], loginids)
        except Exception:
            pass
        return _ok(saved)

    return _auth_fail("No account came back from Deriv — the sign-in may have been cancelled.")


# ---------------------------------------------------------------------------
# Account management
# ---------------------------------------------------------------------------


import os as _os
from pathlib import Path as _Path

_BOT_ENV_FILE = _Path(__file__).parent.parent / ".env"


@app.post("/setup/encryption-key")
def setup_encryption_key() -> dict:
    """One-click: generate a Fernet key and save it to bot-app/.env so tokens
    can be stored encrypted. Refuses if one already exists (changing it would
    make existing stored tokens unreadable — that must be a deliberate edit)."""
    if get_settings().bot_encryption_key:
        raise HTTPException(409, "An encryption key is already configured.")
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    _BOT_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = _BOT_ENV_FILE.read_text() if _BOT_ENV_FILE.exists() else ""
    if "BOT_ENCRYPTION_KEY" in existing:
        raise HTTPException(409, "BOT_ENCRYPTION_KEY already present in .env")
    _BOT_ENV_FILE.write_text((existing.rstrip() + "\n" if existing else "") + f"BOT_ENCRYPTION_KEY={key}\n")
    try:
        _os.chmod(_BOT_ENV_FILE, 0o600)
    except OSError:
        pass
    get_settings.cache_clear()
    return {"ok": True, "note": "Encryption key generated and saved locally — you can now connect accounts."}


class ConnectPATRequest(BaseModel):
    token: str


@app.post("/connect/pat")
async def connect_pat(body: ConnectPATRequest, request: Request, response: Response) -> dict:
    """Connect a Deriv account by pasting a Personal Access Token (Read+Trade).
    Simpler than OAuth for local/desktop use — no app registration or redirect
    URLs. The token is authorized, then stored ENCRYPTED; it never returns in
    any response and never goes through chat. Also tells us if the account is
    on the legacy API (if this succeeds, it is)."""
    sid = request.cookies.get(SESSION_COOKIE) or _new_sid()
    _set_session_cookie(response, sid)
    token = body.token.strip()
    if not token:
        raise HTTPException(400, "paste your Deriv API token first")
    if not get_settings().bot_encryption_key:
        raise HTTPException(
            500, "BOT_ENCRYPTION_KEY is not set — generate one and put it in "
            "bot-app/.env so tokens can be stored encrypted.")
    try:
        info = await authorize_account(token)
    except DerivBotError as exc:
        raise HTTPException(
            400, f"Deriv rejected the token: {exc}. Make sure it has the "
            "'Read' and 'Trade' scopes — or your account may be on the new "
            "platform (then we build the new auth).")
    except Exception as exc:
        raise HTTPException(502, f"couldn't reach Deriv: {exc!r}")

    loginid = info.get("loginid")
    if not loginid:
        raise HTTPException(400, "authorized, but Deriv returned no account id")
    internal_id = get_store().upsert_account(
        deriv_account_id=loginid, token=token, currency=info.get("currency"),
        session_id=sid)
    return {
        "connected": loginid,
        "is_demo": is_demo_account(loginid),
        "kind": "demo" if is_demo_account(loginid) else "REAL MONEY",
        "currency": info.get("currency"),
        "internal_id": internal_id,
        "platform": "legacy",  # authorize succeeded → legacy API works
        "all_accounts": [
            {"loginid": a.get("loginid"),
             "is_demo": is_demo_account(a.get("loginid") or ""),
             "currency": a.get("currency")}
            for a in info.get("account_list", [])
        ],
        "next": "Enable autotrade on this account below, or use the Trade panel. "
                "Keep DRY_RUN on until you've tested on the demo (VRTC) account.",
    }


@app.get("/accounts")
def accounts_list(request: Request) -> list[dict]:
    # Only this browser session's accounts (privacy). No cookie → nothing yet.
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return []
    return get_store().list_accounts_public(session_id=sid)


_BALANCE_CACHE: dict[str, tuple[float, dict]] = {}
_BALANCE_TTL = 8.0  # collapse rapid polls into one real Deriv round-trip


@app.get("/accounts/{account_id}/balance")
async def account_balance(account_id: str, request: Request) -> dict:
    """Live Deriv balance for one of the caller's accounts. New-platform
    accounts read it over the OTP socket; legacy via authorize.

    Never 502s: balance is a best-effort display value, so on a Deriv hiccup or
    an EXPIRED TOKEN it returns 200 with balance=null (+ needs_reconnect) so the
    UI shows "—" / a reconnect hint instead of spamming errors. Reads are cached
    briefly and stale cache is served through transient failures."""
    import time
    _require_own(request, account_id)
    store = get_store()
    acct = store.get_internal(account_id)
    if acct is None:
        raise HTTPException(404, "account not found")
    now = time.monotonic()
    hit = _BALANCE_CACHE.get(account_id)
    if hit and (now - hit[0]) < _BALANCE_TTL:
        return hit[1]
    deriv_id = acct["deriv_account_id"]
    cur_fallback = acct.get("currency") or "USD"
    from app import tokens
    token = await tokens.get_access_token(account_id)
    if not token:
        return {"balance": None, "currency": cur_fallback, "deriv_account_id": deriv_id,
                "error": "no stored token", "needs_reconnect": True}
    platform = (acct.get("platform") or "legacy").lower()
    try:
        if platform == "new":
            from app import deriv_new
            async def _read(tk):
                ws_url = await deriv_new.request_otp_ws(tk, deriv_id)
                return await deriv_new.balance(ws_url)
            b = await tokens.with_fresh_token(account_id, _read)
            bal, cur = b.get("balance"), b.get("currency")
        else:
            info = await authorize_account(token)
            bal, cur = info.get("balance"), info.get("currency")
    except Exception as exc:
        if hit:  # serve last good value through a transient blip
            return hit[1]
        msg = str(exc).lower()
        needs = any(k in msg for k in ("auth", "token", "otp", "401", "unauthor",
                                       "invalid", "expire", "403"))
        return {"balance": None, "currency": cur_fallback, "deriv_account_id": deriv_id,
                "error": str(exc)[:140], "needs_reconnect": needs}
    result = {"balance": bal, "currency": cur or cur_fallback, "deriv_account_id": deriv_id}
    _BALANCE_CACHE[account_id] = (now, result)
    return result


@app.post("/accounts/{account_id}/topup_demo")
async def account_topup_demo(account_id: str, request: Request) -> dict:
    """Reset a demo account's virtual balance to 10,000 USD. Server-side gate:
      • Caller must own the account (_require_own).
      • Account must be a DEMO/virtual loginid — refused for real accounts.
    Routes through the new-platform OTP WS or the legacy authorize WS based
    on how the account was originally connected. Returns the new balance so
    the front-end can repaint without an extra /balance call."""
    _require_own(request, account_id)
    store = get_store()
    acct = store.get_internal(account_id)
    if acct is None:
        raise HTTPException(404, "account not found")
    deriv_id = (acct["deriv_account_id"] or "").upper()
    # Demo detection — Deriv synthetic demos start with VR (legacy) or DOT/VRTC
    # in the new platform. Anything else is a real account → refuse the topup.
    if not (deriv_id.startswith("VR") or deriv_id.startswith("DOT")):
        raise HTTPException(400, "topup only allowed on demo (virtual) accounts")
    from app import tokens
    token = await tokens.get_access_token(account_id)
    if not token:
        raise HTTPException(401, "no stored token — please reconnect Deriv")
    platform = (acct.get("platform") or "legacy").lower()
    from app.deriv import topup_virtual as _legacy_topup
    b = None
    new_platform_error: str | None = None
    # Try the new-platform Options WS first (if account was connected that way).
    # If that path errors out — which is expected, since topup_virtual is a v3
    # API call and the Options WS may not expose it — fall back to the legacy
    # v3 WS authorising with the same OAuth token. Deriv issues OAuth tokens
    # that are valid for v3 reads + topup even when scoped for the new platform.
    if platform == "new":
        try:
            from app import deriv_new
            async def _topup_new(tk):
                ws_url = await deriv_new.request_otp_ws(tk, deriv_id)
                return await deriv_new.topup_virtual(ws_url)
            b = await tokens.with_fresh_token(account_id, _topup_new)
        except DerivBotError as exc:
            new_platform_error = str(exc)
        except Exception as exc:
            new_platform_error = repr(exc)[:160]
    # Legacy path — runs when platform=="legacy" OR when the new-platform call
    # failed above. Authorizes with the stored token on the v3 WS and issues
    # the canonical {"topup_virtual":1} request.
    if b is None:
        try:
            fresh = await tokens.get_access_token(account_id)
            b = await _legacy_topup(fresh or token)
        except DerivBotError as exc:
            # Both paths failed — surface a clean message with a fallback URL.
            detail = ("Deriv refused the topup: " + str(exc)
                      + (" · new-platform attempt: " + new_platform_error if new_platform_error else ""))
            return {"ok": False, "error": detail,
                    "fallback_url": "https://app.deriv.com/cashier/reset-balance",
                    "message": "Couldn't reset automatically — open Deriv to top up."}
        except Exception as exc:
            return {"ok": False, "error": repr(exc)[:160],
                    "fallback_url": "https://app.deriv.com/cashier/reset-balance",
                    "message": "Couldn't reset automatically — open Deriv to top up."}
    # Bust the balance cache so the next /balance call returns the new value.
    _BALANCE_CACHE.pop(account_id, None)
    return {"ok": True, "balance": b.get("balance"), "currency": b.get("currency") or "USD",
            "deriv_account_id": deriv_id,
            "message": "Demo balance reset to "+str(int(b.get("balance") or 0))+" "+(b.get("currency") or "USD")}


@app.post("/logout")
def logout(response: Response) -> dict:
    """Disconnect THIS browser: clear its session cookie so it no longer sees
    any account. The stored account is untouched (a fresh sign-in re-binds it)."""
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


class AccountPatch(BaseModel):
    enabled: bool | None = None
    max_stake_per_trade: float | None = None
    max_trades_per_day: int | None = None
    min_confidence: float | None = None
    allowed_trade_types: list[str] | None = None
    allowed_symbols: list[str] | None = None
    label: str | None = None
    take_profit: float | None = None
    daily_loss_limit: float | None = None
    mpro_enabled: bool | None = None
    mpro_config: dict | None = None
    rf_config: dict | None = None
    proven_auto: bool | None = None
    brain_auto: bool | None = None


def _require_own(request: Request, account_id: str) -> None:
    """403/404 unless this account belongs to the caller's session."""
    sid = request.cookies.get(SESSION_COOKIE)
    if not get_store().account_owned_by(account_id, sid):
        raise HTTPException(404, "account not found")


@app.patch("/accounts/{account_id}")
def account_patch(account_id: str, body: AccountPatch, request: Request) -> dict:
    _require_own(request, account_id)
    # Anti-bypass: a non-paying user could otherwise turn ON auto-trade flags
    # (brain_auto / mpro_enabled / proven_auto / rf_config) via this PATCH and
    # the background trading loop would fire trades for them. Block any flag
    # that ENABLES trading unless the caller is a paid member. Disabling those
    # flags is always allowed (so a downgraded user can turn things OFF).
    enabling_trade = any([
        body.enabled is True,
        body.mpro_enabled is True,
        body.proven_auto is True,
        body.brain_auto is True,
        (isinstance(body.rf_config, dict) and body.rf_config.get("enabled") is True),
    ])
    if enabling_trade:
        _require_member(request)
    if not get_store().update_account_settings(
        account_id,
        enabled=body.enabled,
        max_stake_per_trade=body.max_stake_per_trade,
        max_trades_per_day=body.max_trades_per_day,
        min_confidence=body.min_confidence,
        allowed_trade_types=body.allowed_trade_types,
        allowed_symbols=body.allowed_symbols,
        label=body.label,
        take_profit=body.take_profit,
        daily_loss_limit=body.daily_loss_limit,
        mpro_enabled=body.mpro_enabled,
        mpro_config=body.mpro_config,
        rf_config=body.rf_config,
        proven_auto=body.proven_auto,
        brain_auto=body.brain_auto,
    ):
        raise HTTPException(404, "account not found or no changes")
    return {"updated": account_id}


@app.post("/accounts/stop_all_auto")
def stop_all_auto(request: Request) -> dict:
    """🚨 Emergency kill switch. Sets brain_auto=false, proven_auto=false,
    enabled=false, mpro_enabled=false, and clears rf_config.enabled on EVERY
    account this session owns. Stops the server-side trading loop from firing
    any further autonomous trades. Returns the count of accounts touched."""
    store = get_store()
    sid = request.cookies.get(SESSION_COOKIE)
    # list_accounts_public(sid) already filters to the caller's session.
    visible = store.list_accounts_public(sid) if sid else []
    updated_ids: list[str] = []
    for a in visible:
        rf = a.get("rf_config") or {}
        if isinstance(rf, dict):
            rf = {**rf, "enabled": False}
        store.update_account_settings(
            a["id"],
            enabled=False,
            brain_auto=False,
            proven_auto=False,
            mpro_enabled=False,
            rf_config=rf if isinstance(rf, dict) else None,
        )
        updated_ids.append(a["id"])
    return {
        "stopped": True,
        "accounts_touched": len(updated_ids),
        "account_ids": updated_ids,
        "note": "all server-side auto-trading flags set to false; trading loop will not fire new trades on these accounts.",
    }


@app.delete("/accounts/{account_id}")
def account_delete(account_id: str, request: Request) -> dict:
    _require_own(request, account_id)
    if not get_store().delete_account(account_id):
        raise HTTPException(404, "account not found")
    return {"deleted": account_id}


@app.post("/accounts/{account_id}/trade")
async def account_trade_now(account_id: str, request: Request, symbol: str = "R_100") -> dict:
    """One-shot: ask analyser for a decision and execute through the
    risk gates. Useful for testing without waiting for the autonomous loop."""
    _require_member(request)   # ← server-side paywall
    _require_own(request, account_id)
    acct = get_store().get_internal(account_id)
    if acct is None:
        raise HTTPException(404, "account not found")
    decision = await get_decision(symbol)
    if not decision:
        return {"outcome": "skipped", "reason": "analyser unreachable"}
    return await execute_decision_for_account(acct, decision)


def _structural_winprob(trade_type: str | None, direction: str | None,
                        prediction: int | None) -> float:
    """The TRUE win chance of a Deriv synthetic bet (audited RNG):
      rise_fall / even_odd → 0.5
      over N  → digits N+1..9 = (9-N)/10 ; under N → digits 0..N-1 = N/10
    Recent streaks don't move these — that's the honest point."""
    tt = (trade_type or "").lower()
    d = (direction or "").lower()
    if tt in ("rise_fall", "even_odd"):
        return 0.5
    if tt == "over_under":
        n = int(prediction) if prediction is not None else 5
        n = max(0, min(9, n))
        return (9 - n) / 10.0 if d == "over" else n / 10.0
    return 0.5


_QUOTE_CACHE: dict[tuple, tuple[float, dict]] = {}
_QUOTE_TTL = 6.0  # short cache so the trade-math strip stays snappy across keystrokes


@app.get("/quote")
async def quote(
    symbol: str = "R_100",
    trade_type: str = "rise_fall",
    direction: str | None = "up",
    prediction: int | None = None,
    duration: int = 5,
    duration_unit: str = "t",
    stake: float = 1.0,
) -> dict:
    """Real Deriv price quote — NO account, NO token, NO trade placed.
    Shows what a contract would pay out and the markup you'd earn, so you
    can sanity-check the economics before ever going live. Cached for 6s per
    param-set so the trade-math strip and rapid changes stay instant."""
    import time as _time
    key = (symbol, trade_type, direction, prediction, duration, duration_unit, round(float(stake), 2))
    _now = _time.monotonic()
    hit = _QUOTE_CACHE.get(key)
    if hit and (_now - hit[0]) < _QUOTE_TTL:
        return hit[1]
    try:
        q = await get_proposal(
            symbol=symbol,
            trade_type=trade_type,
            direction=direction,
            prediction=prediction,
            duration=duration,
            duration_unit=duration_unit,
            stake=stake,
        )
    except DerivBotError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"deriv quote failed: {exc!r}")
    payout = q.get("payout")
    out = {
        "symbol": symbol,
        "trade_type": trade_type,
        "stake": stake,
        "payout": payout,
        "ask_price": q.get("ask_price"),
        "spot": q.get("spot"),
        "your_markup_estimate": estimate_markup(payout) if payout else None,
        "markup_percent": get_settings().deriv_markup_percent,
        "longcode": q.get("longcode"),
    }
    # Honest pre-trade math from the contract's STRUCTURAL win probability
    # (RNG, so this is the real chance — recent streaks don't change it).
    if payout:
        wp = _structural_winprob(trade_type, direction, prediction)
        s = float(stake)
        ev = round(wp * (payout - s) - (1 - wp) * s, 4)
        be = round(s / payout, 4) if payout else 1.0
        out.update({
            "win_prob": round(wp, 4),
            "win_prob_pct": round(wp * 100, 1),
            "expected_value": ev,
            "break_even_pct": round(be * 100, 2),
            "edge_pct": round((wp - be) * 100, 2),
            "verdict": "positive EV" if ev > 0 else "negative EV — house edge",
        })
    _QUOTE_CACHE[key] = (_now, out)
    return out


# ---------------------------------------------------------------------------
# Simple manual trading — the grandma "Trade" panel (Rise/Fall + Even/Odd)
# ---------------------------------------------------------------------------

# A short, friendly list of common synthetic indices (always open, 24/7).
# Continuous Volatility indices — 24/7, uniform last digit, so they support
# BOTH Rise/Fall and all digit trades. (2s tick = R_*, 1s tick = 1HZ*V.)
TRADEABLE_SYMBOLS = [
    {"code": "R_10", "name": "Volatility 10"},
    {"code": "R_25", "name": "Volatility 25"},
    {"code": "R_50", "name": "Volatility 50"},
    {"code": "R_75", "name": "Volatility 75"},
    {"code": "R_100", "name": "Volatility 100"},
    {"code": "1HZ10V", "name": "Volatility 10 (1s)"},
    {"code": "1HZ25V", "name": "Volatility 25 (1s)"},
    {"code": "1HZ50V", "name": "Volatility 50 (1s)"},
    {"code": "1HZ75V", "name": "Volatility 75 (1s)"},
    {"code": "1HZ100V", "name": "Volatility 100 (1s)"},
]


@app.get("/symbols")
def symbols() -> dict:
    return {"symbols": TRADEABLE_SYMBOLS, "trade_types": list(MANUAL_TRADE_TYPES)}


# ---------------------------------------------------------------------------
# M Pro — Even/Odd confidence engine (10-market scanner + auto-cycle)
# ---------------------------------------------------------------------------

# The strategy menu (Twinmil-style). Only M Pro is live; the rest are planned.
STRATEGIES = [
    {"id": "m_pro", "name": "M Pro", "tag": "Even/Odd confidence engine · 10-market auto", "status": "live"},
    {"id": "m_digit", "name": "M Digit", "tag": "Adaptive single-digit predictor · EV-optimized", "status": "soon"},
    {"id": "sniper_x", "name": "Sniper X", "tag": "Best over/under pairs · 11-market scan", "status": "soon"},
    {"id": "digit_scanner", "name": "Digit Scanner", "tag": "Probability edge · 10-index scan", "status": "soon"},
    {"id": "r1_match", "name": "Deriv R1 Match", "tag": "Top digit-match auto bot", "status": "soon"},
    {"id": "rise_fall", "name": "Rise & Fall", "tag": "Up/down direction", "status": "soon"},
    {"id": "higher_lower", "name": "Higher / Lower", "tag": "Price vs barrier", "status": "soon"},
    {"id": "over_under", "name": "Over / Under", "tag": "Last-digit threshold", "status": "soon"},
    {"id": "antiloss", "name": "AntiLoss", "tag": "Recovery engine", "status": "soon"},
]


@app.get("/strategies")
def strategies() -> dict:
    return {"strategies": STRATEGIES}


# Small in-memory cache for /scan so multiple in-flight callers (EOAuto +
# RFAuto + OUAuto + scanner UI hitting at ~the same moment) all share one
# computation. Observer ticks come in roughly 1/sec, so 750ms is conservative.
_SCAN_CACHE: dict[str, tuple[float, dict]] = {}
_SCAN_TTL = 0.75


@app.get("/scan")
async def scan_local() -> dict:
    """Live Even/Odd / Rise-Fall / Over-Under scan over the 10 synthetic markets.
    Reads the local observer's rolling snapshot — no analyser call needed
    (single-bot edition). Cached for ~750ms so concurrent calls share work."""
    import time as _time
    now = _time.monotonic()
    cached = _SCAN_CACHE.get("v1")
    if cached and (now - cached[0]) < _SCAN_TTL:
        return cached[1]
    # Never 502 the scanner — the auto-traders poll this every cycle and a
    # transient observer or library hiccup would otherwise flood the brain
    # console with "scanner unreachable" toasts. Degrade gracefully: empty
    # markets + warming note. The next cache window picks up real data once
    # the observer recovers.
    try:
        from app import library as _lib
        lib = _lib.library()
    except Exception:
        lib = {}
    try:
        from app import observer as _obs
        obs = _obs.observer_snapshot_safe()
    except Exception:
        obs = {"markets": [], "flagged": [], "ticks_seen": 0, "markets_ready": 0}
    try:
        from app import observer as _obs   # re-import for SCAN_SYMBOLS access below
    except Exception:
        _obs = None
    payouts_by_sym = ((lib or {}).get("live") or {}).get("even_odd_payouts_by_market") or {}
    # Pretty-name lookup from the observer's symbol list (R_100 → "Vol 100").
    SYMBOL_NAMES = dict(getattr(_obs, "SCAN_SYMBOLS", []) if _obs else [])
    rows: list[dict] = []
    for m in obs.get("markets") or []:
        sym = m.get("symbol")
        if not sym:
            continue
        po = payouts_by_sym.get(sym) or {}
        rows.append({
            "symbol": sym,
            "name": po.get("name") or SYMBOL_NAMES.get(sym, sym),
            "even_pct": m.get("even_pct"),
            "odd_pct": m.get("odd_pct"),
            "even_pct_20": m.get("even_pct_20"),  # multi-window confirmation
            "even_pct_50": m.get("even_pct_50"),
            "eo_z": m.get("eo_z"),
            "up_pct": m.get("up_pct"),            # rise/fall direction signal
            "up_pct_20": m.get("up_pct_20"),
            "up_pct_50": m.get("up_pct_50"),
            "up_z": m.get("up_z"),
            "freq": m.get("freq"),                # OU client-side barrier math
            "freq_20": m.get("freq_20"),
            "freq_50": m.get("freq_50"),
            # Brain-v3 signals: statistical anomaly + run momentum + digit heat.
            "chi_square": m.get("chi_square"),       # uniformity test (higher = more skew)
            "chi_anomalous_p01": m.get("chi_anomalous_p01"),
            "current_streak": m.get("current_streak"),  # consecutive same-parity digits
            "streak_side": m.get("streak_side"),         # "even" / "odd"
            "hot_digit": m.get("hot_digit"),
            "cold_digit": m.get("cold_digit"),
            "ticks": m.get("ticks") or 0,
            "ready": bool(m.get("ready")),
            "payout_pct": po.get("payout_pct"),
        })
    def _lean(r):
        ep = r.get("even_pct")
        return abs(ep - 50) if ep is not None else -1
    ready_rows = [r for r in rows if r["ready"]]
    best_lean = max(ready_rows, key=_lean, default=None) if ready_rows else None
    payout_rows = [r for r in rows if r.get("payout_pct")]
    best_payout = max(payout_rows, key=lambda r: r["payout_pct"] or 0, default=None) if payout_rows else None
    out = {
        "markets": rows,
        "best_payout": best_payout,
        "best_lean": best_lean,
        "best": best_payout,           # legacy field — older UI code still expected `best`
    }
    _SCAN_CACHE["v1"] = (now, out)
    return out


@app.get("/deriv/library")
def deriv_library_local() -> dict:
    """Local Deriv trade-type library (single-bot edition — used to live on the
    analyser, now built in-process)."""
    from app import library as _lib
    return _lib.library()


@app.post("/deriv/library/refresh")
async def deriv_library_refresh_local() -> dict:
    """Re-pull live payouts for the 10 synthetic markets and persist to disk."""
    from app import library as _lib
    return await _lib.refresh_payouts()


@app.get("/even_odd/payouts")
async def even_odd_payouts_local(stake: float = 1.0, duration: int = 1) -> dict:
    """Live Even/Odd payout comparison across all 10 markets. Reads the local
    library's cached payout table (refreshed in the background)."""
    from app import library as _lib
    lib = _lib.library()
    payouts = ((lib or {}).get("live") or {}).get("even_odd_payouts_by_market") or {}
    rows = [{"symbol": s, **info} for s, info in payouts.items()]
    rows.sort(key=lambda r: (r.get("payout_pct") or 0), reverse=True)
    return {"stake": float(stake), "duration": int(duration), "markets": rows,
            "best": (rows[0] if rows else None)}


@app.get("/mpro/status")
def mpro_status() -> dict:
    from app.mpro import engine
    return engine.status()


# ---------------------------------------------------------------------------
# Assistant view — the simple, client-facing face of the whole tree. The real
# research + analysis happens behind the scenes (Researcher + Analyser); here
# we just surface it in plain language.
# ---------------------------------------------------------------------------


def _friendly_call(decision: dict) -> dict:
    """Turn the Analyser's decision into a plain-language suggestion, hiding all
    the internal strategy machinery."""
    if not decision or not decision.get("is_trade"):
        return {"suggestion": "wait", "label": "No clear signal — sitting out",
                "confidence": round(float(decision.get("confidence", 0)) * 100) if decision else 0}
    tt = decision.get("trade_type")
    conf = round(float(decision.get("confidence", 0)) * 100)
    if tt == "rise_fall":
        d = (decision.get("direction") or "").lower()
        call = "RISE" if d == "up" else "FALL"
    elif tt == "even_odd":
        call = (decision.get("prediction") or "").upper() or "EVEN"
    else:
        call = (tt or "").upper()
    return {"suggestion": call, "label": f"Leaning {call}", "confidence": conf,
            "trade_type": tt, "direction": decision.get("direction"),
            "prediction": decision.get("prediction")}


@app.get("/assistant/read")
async def assistant_read(symbol: str = "R_100") -> dict:
    """What J81 'sees' on a market right now — a simple read powered by the
    Analyser behind the scenes, plus a live spot price + payout so the trade
    terminal feels live."""
    decision = await get_decision(symbol)
    friendly = _friendly_call(decision)
    # One no-auth proposal gives a real current spot + payout (for the buttons).
    spot = payout = None
    try:
        q = await get_proposal(symbol=symbol, trade_type="rise_fall",
                               direction="up", prediction=None,
                               duration=5, duration_unit="t", stake=1.0)
        spot, payout = q.get("spot"), q.get("payout")
    except Exception:
        pass
    return {
        "symbol": symbol,
        "analysing": True,
        "spot": spot,
        "payout": payout,
        **friendly,
        "message": (
            f"J81 studied {symbol}: {friendly['label'].lower()}"
            + (f" ({friendly['confidence']}% confident)." if friendly["suggestion"] != "wait" else ".")
        ) if decision else f"J81 is warming up its read on {symbol}…",
    }


@app.get("/assistant/summary")
def assistant_summary(account_id: str | None = None,
                      chapter_start: str | None = None) -> dict:
    """Today's results in plain numbers for the client — wins, trades, profit.
    When `account_id` is given, the numbers are filtered to that ONE account
    (so switching from demo → real shows real history only, not aggregated).
    When `chapter_start` (ISO timestamp) is given, results are scoped to
    trades created at-or-after that moment — every login starts a fresh
    chapter on the dashboard. No internal jargon."""
    store = get_store()
    accts_all = store.list_accounts_public()
    accts = [a for a in accts_all if a["id"] == account_id] if account_id else accts_all
    trades = store.list_trades(account_id=account_id, since=chapter_start, limit=500)
    settled = [t for t in trades if t.get("outcome") in ("won", "lost")]
    wins = sum(1 for t in settled if t.get("outcome") == "won")
    # Chapter-scoped profit: sum of settled trade profits since chapter_start.
    # Without a chapter, fall back to the per-account daily roll-up.
    if chapter_start:
        profit_today = round(sum(float(t.get("profit") or 0) for t in settled), 2)
    else:
        profit_today = round(sum(a.get("profit_today", 0.0) for a in accts), 2)
    goals = [
        {"account": a["deriv_account_id"], "is_demo": a["is_demo"],
         "profit_today": a.get("profit_today", 0.0),
         "take_profit": a.get("take_profit"), "daily_loss_limit": a.get("daily_loss_limit")}
        for a in accts if a["enabled"]
    ]
    # trades_total: cheap COUNT(*) — uncapped (previous list-and-len was
    # clamped at 1000 by list_trades' SQL LIMIT cap, leaving the dashboard
    # stuck at "1000 trades" forever once the user passed that threshold).
    if account_id:
        trades_total = store.count_trades(account_id=account_id, since=chapter_start)
    elif chapter_start:
        trades_total = store.count_trades(since=chapter_start)
    else:
        trades_total = store.stats().get("trades_total", 0)
    return {
        "accounts_connected": len(accts_all),
        "scope": "account" if account_id else "all",
        "account_id": account_id,
        "trades_total": trades_total,
        "settled": len(settled),
        "wins": wins,
        "win_rate": round(wins / len(settled), 3) if settled else None,
        "profit_today": round(profit_today, 2),
        "goals": goals,
    }


class ManualTradeRequest(BaseModel):
    account_id: str
    trade_type: str                      # rise_fall | even_odd
    symbol: str = "R_100"
    direction: str | None = None         # rise_fall: up | down
    prediction: str | None = None        # even_odd: even | odd
    stake: float = 1.0
    duration: int = 5
    duration_unit: str = "t"


@app.post("/trade/manual")
async def trade_manual(req: ManualTradeRequest, request: Request) -> dict:
    """Place a trade the user picked themselves (Rise/Fall or Even/Odd).
    Honours DRY_RUN + the account's stake/daily caps."""
    _require_member(request)   # ← server-side paywall (no frontend bypass)
    _require_own(request, req.account_id)
    acct = get_store().get_internal(req.account_id)
    if acct is None:
        raise HTTPException(404, "account not found")
    return await execute_manual_trade(
        acct, trade_type=req.trade_type, symbol=req.symbol,
        direction=req.direction, prediction=req.prediction,
        stake=req.stake, duration=req.duration, duration_unit=req.duration_unit,
    )


@app.post("/accounts/{account_id}/settle")
async def account_settle(account_id: str, request: Request) -> dict:
    """Settle this account's just-expired contracts on demand, so the site can
    show the win/loss within seconds instead of waiting for the trading loop."""
    _require_own(request, account_id)
    return await settle_pending_for_account(account_id)


# ---------------------------------------------------------------------------
# Priority mode — read/toggle the hub's tree-wide focus from the Bot page
# ---------------------------------------------------------------------------


@app.get("/priority")
async def priority_get() -> dict:
    from app import comms_client
    return await comms_client.get_priority()


class PriorityToggle(BaseModel):
    enabled: bool


@app.post("/priority")
async def priority_set(body: PriorityToggle) -> dict:
    """Single-bot edition: priority is a local flag (no hub to coordinate)."""
    from app import comms_client
    return await comms_client.set_priority(body.enabled)


@app.get("/observer/status")
def observer_status_local() -> dict:
    """Local 24/7 live observer snapshot (single-bot edition)."""
    from app import observer as _obs
    try:
        return _obs.snapshot()
    except Exception:
        return {"unreachable": False, "markets": [], "flagged": [],
                "ticks_seen": 0, "markets_ready": 0}


@app.get("/observer/patterns")
def observer_patterns_local(limit: int = 30) -> list:
    from app import observer as _obs
    try:
        return _obs.patterns(limit=limit)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Single-bot edition: the researcher + analyser services were retired and the
# library/observer/brain logic now lives in-process. The cycle + maintenance
# endpoints used to coordinate across systems; we keep tiny stubs so the UI
# can call them without breaking, but they always return empty/static state.
# ---------------------------------------------------------------------------


@app.get("/cycle/history")
def cycle_history_stub(limit: int = 20) -> dict:
    """Stub — there is no strategy cycle in the single-bot edition."""
    return {"recent": [], "summary": {"cycles": 0, "any_proven": False},
            "note": "single-bot edition: no analyser cycle"}


@app.get("/cycle/status")
def cycle_status_stub() -> dict:
    """Stub — no analyser cycle in this edition."""
    return {"reachable": True, "tested": 0, "proven_count": 0,
            "next_in_seconds": None,
            "note": "single-bot edition: no analyser cycle"}


@app.get("/trades")
def trades_list(account_id: str | None = None, limit: int = 100) -> list[dict]:
    return get_store().list_trades(account_id=account_id, limit=limit)


@app.get("/trade_stats")
def trade_stats(account_id: str | None = None, window: int = 100,
                include_practice: bool = True) -> dict:
    """The J81 goal scoreboard over the last `window` settled trades:
    wins / total / win-rate / net P/L, with goal flags (≥60% AND net>0) and
    a per-type / per-market breakdown."""
    return get_store().trade_stats(account_id=account_id, window=window,
                                   include_practice=include_practice)


@app.get("/brain/advise")
async def brain_advise(account_id: str | None = None) -> dict:
    """J81 Brain — synthesizes the shared library (Deriv specs + discipline +
    risk + scripture) with the live state (scoreboard, balance, account) into
    a ranked list of actionable recommendations, each citing the principle
    behind it. The library becomes trading POWER instead of just stockpile."""
    from app import brain
    store = get_store()
    account = store.get_internal(account_id) if account_id else None
    if account:
        # public-shape view for is_demo flag + currency
        public = next((a for a in store.list_accounts_public()
                       if a["id"] == account_id), None) or {}
        if public:
            account = {**account, "is_demo": public.get("is_demo"), "currency": public.get("currency")}
    # Balance: try the cached endpoint logic; fall back to None.
    balance: dict | None = None
    if account_id:
        try:
            # Reuse the existing balance endpoint logic for caching.
            from app import tokens  # noqa: F401
            import time as _time
            now = _time.monotonic()
            hit = _BALANCE_CACHE.get(account_id)
            if hit and (now - hit[0]) < _BALANCE_TTL:
                balance = hit[1]
            else:
                token = await tokens.get_access_token(account_id)
                if token and account and (account.get("platform") or "legacy").lower() == "new":
                    from app import deriv_new
                    async def _read(tk):
                        ws_url = await deriv_new.request_otp_ws(tk, account["deriv_account_id"])
                        return await deriv_new.balance(ws_url)
                    b = await tokens.with_fresh_token(account_id, _read)
                    balance = {"balance": b.get("balance"), "currency": b.get("currency"),
                               "deriv_account_id": account["deriv_account_id"]}
        except Exception:
            balance = None
    scoreboard = store.trade_stats(account_id=account_id, window=100)
    return await brain.advise(account=account, balance=balance, scoreboard=scoreboard)


class ProvenStrategies(BaseModel):
    strategies: list[dict]


@app.post("/strategies/proven")
def strategies_proven_save(body: ProvenStrategies) -> dict:
    """The Analyser's 30-min cycle pushes its PROVEN strategies here. They
    persist in the bot even when the analyser/researcher auto-clear."""
    store = get_store()
    saved = [store.save_proven_strategy(s) for s in (body.strategies or [])]
    return {"saved": len(saved), "ids": saved}


@app.get("/strategies/proven")
def strategies_proven_list(limit: int = 200) -> list[dict]:
    return get_store().list_proven_strategies(limit=limit)


@app.get("/stats")
def stats() -> dict:
    return get_store().stats()


@app.get("/loop/status")
def loop_status() -> dict:
    return trading_loop.status


@app.get("/preflight")
def preflight() -> dict:
    """Readiness check for going live — what's set, what's missing, and
    which connected accounts are REAL money vs demo."""
    s = get_settings()
    accts = get_store().list_accounts_public()
    real_enabled = [a["deriv_account_id"] for a in accts if a["enabled"] and not a["is_demo"]]
    demo_enabled = [a["deriv_account_id"] for a in accts if a["enabled"] and a["is_demo"]]
    checks = {
        "deriv_app_id_set": bool(s.deriv_app_id),
        "encryption_key_set": bool(s.bot_encryption_key),
        "accounts_connected": len(accts) > 0,
    }
    ready_for_demo = all([
        checks["deriv_app_id_set"],
        checks["encryption_key_set"],
        checks["accounts_connected"],
    ])
    return {
        "dry_run": s.dry_run,
        "checks": checks,
        "ready_to_trade": ready_for_demo,
        "accounts": {
            "total": len(accts),
            "demo_enabled": demo_enabled,
            "real_money_enabled": real_enabled,
        },
        "warnings": (
            (["DRY_RUN is OFF and you have REAL-MONEY accounts enabled — "
              "live trades will place real contracts."] if (not s.dry_run and real_enabled) else [])
            + (["DRY_RUN is ON — no real or demo contracts will be placed; "
                "everything is logged only."] if s.dry_run else [])
        ),
        "recommendation": (
            "Test on a demo account (VRTC…) with DRY_RUN=false first; "
            "enable a real-money account only once you trust the full loop."
        ),
    }


def main() -> None:
    import uvicorn
    s = get_settings()
    uvicorn.run(app, host=s.host, port=s.port)


if __name__ == "__main__":
    main()
