"""Persistent runtime control surface.

This is the "section where you can turn search concentration on/off". It is a
JSON file (config/research_config.json) so it survives restarts and can be
edited live — either through the /config API or by hand on the VPS. The
autonomous scheduler re-reads it every cycle, so toggles take effect without
a restart.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from app.config import data_path
from app.models import FocusMode, SourcePlatform, TradeType


def _config_path() -> Path:
    return data_path("config/research_config.json")


class AutonomousConfig(BaseModel):
    enabled: bool = False
    # 10-minute default so the tree keeps producing. On a paid LLM (Opus) this
    # adds up — use Gemini's free tier for frequent cycles, or raise this.
    interval_seconds: int = Field(default=600, ge=60)
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


class ExtractionConfig(BaseModel):
    """Controls how hard J81 tries to find strategies when a link is sparse."""

    # If a study-link finds fewer strategies than this, J81 will broaden the
    # search using terms from the page and run extra research cycles. Set to
    # 0 to disable the broaden behaviour entirely.
    min_strategies_target: int = Field(default=2, ge=0, le=10)
    # Hard budget on broaden iterations (each runs the full source pipeline).
    # Tradeoff: higher = more thorough, more API tokens used.
    max_broaden_iterations: int = Field(default=2, ge=0, le=5)
    # How many results per source on each broaden attempt.
    broaden_results_per_source: int = Field(default=3, ge=1, le=10)


class LibrarySharing(BaseModel):
    """How and when the gathered library is shipped to the J81 analyser."""

    auto_send_to_analyser: bool = False
    auto_send_interval_seconds: int = Field(default=7200, ge=300)  # default 2h
    auto_send_after_study_link: bool = False
    # Fire the analyser-send after every N autonomous research cycles.
    # 0 = disabled (use the time-based loop instead). 2 = "ship every other cycle".
    auto_send_every_n_cycles: int = Field(default=0, ge=0, le=100)
    archive_after_send: bool = True  # frees the library for fresh data


class ResearchConfig(BaseModel):
    autonomous: AutonomousConfig = Field(default_factory=AutonomousConfig)
    sources: SourceToggles = Field(default_factory=SourceToggles)
    focus: FocusToggles = Field(default_factory=FocusToggles)
    topics: list[ResearchTopic] = Field(default_factory=_default_topics)
    sharing: LibrarySharing = Field(default_factory=LibrarySharing)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    # Per-trade-type search weighting, set by the Analyser via balance
    # commands. Higher weight = more results pulled for topics of that type.
    # Empty = all weighted 1.0.
    trade_type_weights: dict[str, float] = Field(default_factory=dict)


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
