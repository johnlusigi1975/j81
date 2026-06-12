#
# Psalm 1:3 — like a tree planted by the rivers of water · fruit in its
# season · leaf that shall not wither · whatsoever it doeth shall prosper.
#
# Jeremiah 17:7-8 — blessed is the man that trusteth in the Lord · roots
# spread by the river · leaf green in the heat · fruit in the drought.
#
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "J81 Trade Desk"
APP_VERSION = "0.1.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 9500
    data_dir: str = "."

    # SAFETY: every "real money" code path is gated on this. Default True.
    dry_run: bool = True

    # Deriv app registration (api.deriv.com/dashboard).
    # deriv_app_id is the OAuth client_id — on the NEW platform this is
    # ALPHANUMERIC (e.g. 33lAFHp17w0VN2MmLCbOp) and is ONLY valid for the OAuth
    # login flow. It is NOT accepted by the legacy v3 WebSocket (it 401s the
    # handshake), so v3 calls use deriv_ws_app_id below instead.
    deriv_app_id: str = ""
    # NUMERIC app_id for the v3 WebSocket (authorize/quote/buy/settle). Set this
    # to your own numeric legacy app_id to earn markup; blank falls back to a
    # numeric deriv_app_id if you have one, else Deriv's public "1089" (which
    # works for connecting/authorizing/quoting but earns no markup).
    deriv_ws_app_id: str = ""
    deriv_oauth_redirect_uri: str = "http://localhost:9500/oauth/callback"
    deriv_markup_percent: float = 2.0  # % of payout; Deriv enforces max 3%

    # Deriv OAuth endpoints. Default = LEGACY flow (returns token1/acct1/cur1).
    # To use the NEW platform's OAuth2 + PKCE, set deriv_oauth_token_url (and
    # point authorize_url at the new endpoint) per your Deriv app registration.
    # PKCE auto-activates when deriv_oauth_token_url is non-empty.
    deriv_oauth_authorize_url: str = "https://oauth.deriv.com/oauth2/authorize"
    deriv_oauth_token_url: str = ""           # set to enable OAuth2 PKCE
    deriv_oauth_scope: str = "trade account_manage"  # new-platform scopes

    # New-platform REST/WS base. Flow: access token → GET
    # {base}/trading/v1/options/accounts/{loginid}/otp → wss …/ws/{real|demo}?otp=…
    deriv_new_api_base: str = "https://api.derivws.com"

    # Your Deriv referral/affiliate link — shown behind "Open a free account".
    # Override via DERIV_REFERRAL_URL in .env if it ever changes.
    deriv_referral_url: str = "https://track.deriv.com/_6uqV-95nzXtZl7VyVw174GNd7ZgqdRLk/1/"

    # Token encryption (Fernet)
    bot_encryption_key: str = ""

    # Where to read decisions from
    analyser_url: str = "http://127.0.0.1:9000"

    # Default risk limits per user (each user can override)
    default_max_stake_per_trade: float = 1.0
    default_max_trades_per_day: int = 20
    default_min_confidence: float = 0.70

    # Trading loop interval
    trade_poll_seconds: int = 120

    # ---- Paid access (membership paywall) ----
    # Secret that protects the owner-only license endpoints (generate/list codes).
    # Set ADMIN_KEY in the dashboard; while blank, those endpoints are disabled.
    admin_key: str = ""
    # 0 ⇒ LIFETIME (default). >0 ⇒ time-bound legacy membership of N days.
    access_days: int = 0
    access_price_label: str = "$100"    # shown on the paywall (legacy single-tier)
    # ── Two-tier pricing: $5 = Even/Odd only (starter) · $50 = all trade types.
    # Tier is detected from the paid AMOUNT in the webhooks (>=50 ⇒ all, >=1 ⇒ eo).
    access_price_eo_label: str = "$5"
    access_price_all_label: str = "$50"
    access_buy_url_eo: str = ""     # $5 Even/Odd checkout link (Selar etc.) — set in dashboard
    access_buy_url_all: str = ""    # $50 all-access checkout link — set in dashboard
    # Your Stripe Payment Link — PUBLIC URL, safe to set here. The webhook
    # auto-mints + binds a lifetime license to the buyer's Deriv loginids,
    # so the moment they're back in the app they're unlocked.
    access_buy_url: str = ""
    # Master switch: when True, REAL accounts are paywalled (demo stays free).
    # Default ON so the paywall ships engaged. Set REQUIRE_ACCESS=false in env
    # if you want the app fully open while testing.
    require_access: bool = True
    # Stripe webhook signing secret (whsec_…) — set in the dashboard to auto-issue
    # a code when a payment completes. Blank disables the webhook.
    stripe_webhook_secret: str = ""
    # MASTER UNLOCK CODE — anyone who pastes this exact string in the "Have a
    # code?" form gets LIFETIME access bound to whichever Deriv account(s)
    # they're connected with. Use it for yourself, comp access for friends, or
    # influencer keys. Blank disables the feature. Pick something you don't
    # want guessed — e.g. "J81-OWNR-LIFE-7Z3K".
    master_unlock_code: str = ""
    # Resend (resend.com) API key for emailing the lifetime license code to
    # the buyer after their Stripe payment succeeds. Free tier: 3,000/mo. Set
    # RESEND_API_KEY in the dashboard. Blank disables emails (paywall still
    # works — buyers just have to find their code via /access/code manually).
    resend_api_key: str = ""
    # "From" address for transactional emails (must be a verified domain in
    # Resend, OR use the default onboarding@resend.dev for testing). Format:
    #   "J81 Trade Desk <noreply@yourdomain.com>"  or just  "noreply@your.com"
    email_from: str = ""

    # ── INTASEND (Kenyan fintech — primary processor) ──────────────
    # When INTASEND_SECRET_KEY is set, the paywall uses IntaSend hosted
    # checkout: native M-Pesa STK push for Kenyan buyers + cards (Visa /
    # Mastercard) for everyone else. Self-serve onboarding accepts solo
    # creators with a Kenyan Business Name Certificate.
    # Get keys from dashboard.intasend.com → API & Webhooks → API Keys.
    # Use TEST keys (ISSecretKey_test_...) for sandbox, LIVE keys for
    # production. The same code path serves both — IntaSend routes by key.
    intasend_secret_key: str = ""         # ISSecretKey_test_... or ISSecretKey_live_...
    intasend_publishable_key: str = ""    # ISPubKey_... (optional, for future client widgets)
    # IntaSend signs webhooks with this challenge string. You set it in the
    # dashboard → API & Webhooks → Webhooks; they echo it back in the
    # `X-IntaSend-Signature` (or `challenge`) field of every delivery.
    # Pick something unguessable.
    intasend_webhook_challenge: str = ""
    # Live vs sandbox switch — set to "true" once IntaSend approves you.
    # Blank/false uses sandbox (sandbox.intasend.com).
    intasend_live: bool = False
    # ── FLUTTERWAVE (kept for fallback; primary is IntaSend now) ──
    flw_secret_key: str = ""
    flw_secret_hash: str = ""
    flw_redirect_url: str = ""

    # ── SELAR (Kenya-friendly creator platform — current live processor) ──
    # Selar handles checkout (cards + M-Pesa via Paystack); we set
    # ACCESS_BUY_URL to the Selar product URL and Selar posts back to
    # /webhooks/selar on successful payment.
    # Configure the webhook URL + secret in Selar dashboard →
    # Settings → Developer / Webhooks. Selar signs the payload with
    # HMAC-SHA256 using this secret and sends the signature in the
    # `x-selar-signature` header. Pick something unguessable.
    selar_webhook_secret: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


def data_path(*parts: str) -> Path:
    p = Path(*parts)
    if p.is_absolute():
        return p
    return Path(get_settings().data_dir) / p
