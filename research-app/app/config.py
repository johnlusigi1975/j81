from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "J81 Deriv Researcher"
APP_VERSION = "0.3.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM provider switch. "anthropic" (Claude) or "google" (Gemini).
    # Change without touching code — uvicorn --reload picks up .env edits.
    llm_provider: str = "anthropic"

    # Anthropic
    anthropic_api_key: str = ""
    extractor_model: str = "claude-opus-4-7"

    # Google Gemini (https://aistudio.google.com — generous free tier)
    google_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    # Downstream Logging/Backtest app
    logging_app_url: str = ""
    logging_app_api_key: str = ""

    # Research limits
    max_results_per_source: int = 5
    request_timeout_seconds: int = 30

    # --- Optional source-API credentials --------------------------------
    # When set, the matching adapter uses the official API. When empty,
    # it falls back to the keyless scrape/JSON path. Mix and match freely.

    # Reddit "web" or "installed" app type — read-only client_credentials.
    # Create one at https://www.reddit.com/prefs/apps
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "j81-deriv-researcher/0.3"

    # YouTube Data API v3 key (Google Cloud Console -> enable API -> create key)
    youtube_api_key: str = ""

    # Tavily web search API (https://tavily.com — generous free tier)
    tavily_api_key: str = ""

    # --- Optional SMTP (for emailing the library export) -----------------
    # If unset, the homepage falls back to opening the user's local mail
    # client via mailto: (works without any server-side config).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""

    # Optional speech-to-text
    enable_whisper: bool = False
    whisper_model: str = "base"

    # Connected-bot trade journal (SQLite)
    trades_db_path: str = "data/trades.db"

    # Comms hub (the Analyser). The Researcher reports to it and obeys its
    # commands as the brain of the system.
    comms_hub_url: str = "http://127.0.0.1:9000"

    # Persistence root. Relative data paths (DB, config, out/) are rooted
    # here. Locally this is "." (current behaviour); on Render/Railway/Fly
    # point it at the mounted persistent disk, e.g. /var/data.
    data_dir: str = "."

    # Service
    host: str = "0.0.0.0"
    port: int = 8001


@lru_cache
def get_settings() -> Settings:
    return Settings()


def data_path(*parts: str) -> "Path":
    """Resolve a path under DATA_DIR (absolute parts are returned as-is)."""
    from pathlib import Path

    p = Path(*parts)
    if p.is_absolute():
        return p
    return Path(get_settings().data_dir) / p
