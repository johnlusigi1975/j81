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
    default_min_confidence: float = 0.65

    # Trading loop interval
    trade_poll_seconds: int = 120

    # ---- Paid access (membership paywall) ----
    # Secret that protects the owner-only license endpoints (generate/list codes).
    # Set ADMIN_KEY in the dashboard; while blank, those endpoints are disabled.
    admin_key: str = ""
    # 0 ⇒ LIFETIME (default). >0 ⇒ time-bound legacy membership of N days.
    access_days: int = 0
    access_price_label: str = "$100"    # shown on the paywall
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


@lru_cache
def get_settings() -> Settings:
    return Settings()


def data_path(*parts: str) -> Path:
    p = Path(*parts)
    if p.is_absolute():
        return p
    return Path(get_settings().data_dir) / p
