"""Persistent runtime control surface.

This is the "section where you can turn search concentration on/off". It is a
JSON file (config/research_config.json) so it survives restarts and can be
edited live — either through the /config API or by hand on the VPS. The
autonomous scheduler re-reads it every cycle, so toggles take effect without
a restart.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from app.config import data_path
from app.models import FocusMode, SourcePlatform, TradeType


def _config_path() -> Path:
    return data_path("config/research_config.json")


class AutonomousConfig(BaseModel):
    enabled: bool = False
    interval_seconds: int = Field(default=3600, ge=60)
    max_results_per_source: int = Field(default=5, ge=1, le=25)


class SourceToggles(BaseModel):
    web: bool = True
    youtube: bool = True
    reddit: bool = True
    tiktok: bool = False
    instagram: bool = False

    def enabled(self) -> list[SourcePlatform]:
        mapping = {
            SourcePlatform.WEB: self.web,
            SourcePlatform.YOUTUBE: self.youtube,
            SourcePlatform.REDDIT: self.reddit,
            SourcePlatform.TIKTOK: self.tiktok,
            SourcePlatform.INSTAGRAM: self.instagram,
        }
        return [p for p, on in mapping.items() if on]


class FocusToggles(BaseModel):
    strategies: bool = True
    updates: bool = True

    def enabled(self) -> list[FocusMode]:
        out: list[FocusMode] = []
        if self.strategies:
            out.append(FocusMode.STRATEGIES)
        if self.updates:
            out.append(FocusMode.UPDATES)
        return out


class ResearchTopic(BaseModel):
    name: str
    query: str
    trade_type: TradeType = TradeType.RISE_FALL
    hashtags: list[str] = Field(default_factory=list)
    enabled: bool = True


def _default_topics() -> list[ResearchTopic]:
    return [
        ResearchTopic(
            name="deriv-even-odd",
            query="deriv even odd digit strategy",
            trade_type=TradeType.EVEN_ODD,
            hashtags=["#deriv", "#derivtrading", "#evenodd"],
        ),
        ResearchTopic(
            name="deriv-rise-fall",
            query="deriv rise fall strategy synthetic indices",
            trade_type=TradeType.RISE_FALL,
            hashtags=["#deriv", "#syntheticindices", "#volatility75"],
        ),
        ResearchTopic(
            name="deriv-updates-sentiment",
            query="deriv platform update news review experience",
            trade_type=TradeType.RISE_FALL,
            hashtags=["#deriv", "#derivbot", "#binarycom"],
        ),
        ResearchTopic(
            name="deriv-rules-contract-specs",
            query=(
                "deriv contract terms conditions payout rules digit range "
                "barrier duration restrictions how it works"
            ),
            trade_type=TradeType.RISE_FALL,
            hashtags=["#deriv", "#derivtrading"],
        ),
        ResearchTopic(
            name="deriv-rules-hidden-behaviour",
            query=(
                "deriv hidden rules undocumented behaviour payout formula "
                "trick traders discovered even odd over under explained"
            ),
            trade_type=TradeType.EVEN_ODD,
            hashtags=["#deriv", "#derivbot", "#syntheticindices"],
        ),
    ]


class LibrarySharing(BaseModel):
    """How and when the gathered library is shipped to the J81 analyser."""

    auto_send_to_analyser: bool = False
    auto_send_interval_seconds: int = Field(default=7200, ge=300)  # default 2h
    auto_send_after_study_link: bool = False
    archive_after_send: bool = True  # frees the library for fresh data


# ---------------------------------------------------------------------------
# Connector framework — generalized outbound-API plumbing
# ---------------------------------------------------------------------------


class AuthKind(str, Enum):
    NONE = "none"          # no auth
    BEARER = "bearer"      # Authorization: Bearer <token>
    BASIC = "basic"        # HTTP Basic (username + password)
    HEADER = "header"      # custom header_name: token
    QUERY = "query"        # ?query_param=token


class ConnectorKind(str, Enum):
    ANALYSER = "analyser"  # consumes the library (by_trade_type payload)
    WEBHOOK = "webhook"    # generic sink, same payload contract


class PayloadMode(str, Enum):
    SPLIT = "split"   # POST {base}/strategies + {base}/insights (J81-native)
    SINGLE = "single" # POST {base} with the full library object in one body


class ConnectorAuth(BaseModel):
    kind: AuthKind = AuthKind.NONE
    token: str = ""          # bearer / header value / query value
    username: str = ""       # basic
    password: str = ""       # basic
    header_name: str = ""    # for kind=header
    query_param: str = ""    # for kind=query


class Connector(BaseModel):
    """A named outbound HTTP integration. Stored in research_config.json so
    you can add/edit/remove analysers and other API endpoints without
    redeploying. Multiple connectors of the same kind are fanned out to in
    parallel when the library is shipped.
    """

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    name: str
    kind: ConnectorKind = ConnectorKind.ANALYSER
    base_url: str
    auth: ConnectorAuth = Field(default_factory=ConnectorAuth)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    payload_mode: PayloadMode = PayloadMode.SPLIT
    enabled: bool = True
    note: str = ""


class ResearchConfig(BaseModel):
    autonomous: AutonomousConfig = Field(default_factory=AutonomousConfig)
    sources: SourceToggles = Field(default_factory=SourceToggles)
    focus: FocusToggles = Field(default_factory=FocusToggles)
    topics: list[ResearchTopic] = Field(default_factory=_default_topics)
    sharing: LibrarySharing = Field(default_factory=LibrarySharing)
    connectors: list[Connector] = Field(default_factory=list)


def load_config() -> ResearchConfig:
    path = _config_path()
    if path.exists():
        return ResearchConfig.model_validate_json(path.read_text())
    cfg = ResearchConfig()
    save_config(cfg)
    return cfg


def save_config(cfg: ResearchConfig) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cfg.model_dump_json(indent=2))
    try:
        # connector auth tokens live in this file — keep it user-only-readable.
        path.chmod(0o600)
    except OSError:
        # filesystem may not support chmod (e.g. some Windows FS); not fatal.
        pass
