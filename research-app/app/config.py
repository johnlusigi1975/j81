from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "J81 Deriv Researcher"
APP_VERSION = "0.3.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM
    anthropic_api_key: str = ""
    extractor_model: str = "claude-opus-4-7"

    # Downstream Logging/Backtest app
    logging_app_url: str = ""
    logging_app_api_key: str = ""

    # Research limits
    max_results_per_source: int = 5
    request_timeout_seconds: int = 30

    # Optional speech-to-text
    enable_whisper: bool = False
    whisper_model: str = "base"

    # Connected-bot trade journal (SQLite)
    trades_db_path: str = "data/trades.db"

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
