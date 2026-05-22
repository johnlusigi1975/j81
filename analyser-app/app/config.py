from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "J81 Analyser"
APP_VERSION = "0.1.0"

# When "priority mode" is on, the whole tree focuses ONLY on these two simple
# trade types so the systems can master them first before expanding.
PRIORITY_TRADE_TYPES = ("rise_fall", "even_odd")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 9000

    # All relative data paths are rooted here (SQLite, future caches).
    data_dir: str = "."

    # Bearer token expected on incoming POSTs. Empty = no auth required.
    incoming_api_key: str = ""

    # Where to push research-gap requests when the Analyser asks the
    # Researcher to fetch more on a topic it lacks. Default works locally.
    researcher_url: str = "http://127.0.0.1:8000"
    researcher_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


def data_path(*parts: str) -> Path:
    p = Path(*parts)
    if p.is_absolute():
        return p
    return Path(get_settings().data_dir) / p
