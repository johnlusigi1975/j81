"""Core data contracts shared across the Research App and consumed by the
downstream Logging/Backtest app.

The Strategy schema is deliberately structured (indicators + restricted
boolean expressions) rather than free text or executable code, so the
backtest engine can run it deterministically and safely.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid4().hex


class TradeType(str, Enum):
    """Deriv contract families a strategy can target."""

    RISE_FALL = "rise_fall"
    EVEN_ODD = "even_odd"
    OVER_UNDER = "over_under"
    MATCHES_DIFFERS = "matches_differs"
    TOUCH_NO_TOUCH = "touch_no_touch"
    HIGHER_LOWER = "higher_lower"
    ASIAN = "asian"


class SourcePlatform(str, Enum):
    WEB = "web"
    YOUTUBE = "youtube"
    REDDIT = "reddit"
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"


class Provenance(BaseModel):
    """Where a strategy was researched from, for auditing/trust."""

    platform: SourcePlatform
    url: str
    title: str | None = None
    author: str | None = None
    published_at: str | None = None
    retrieved_at: datetime = Field(default_factory=_utcnow)


class Indicator(BaseModel):
    """A technical indicator the backtest engine must compute.

    `ref` is the name used to reference this indicator's output inside the
    entry/exit expressions, e.g. an Indicator with ref="rsi" can be used in
    an expression like "rsi < 30".
    """

    ref: str = Field(description="identifier used inside rule expressions")
    type: str = Field(description="e.g. RSI, SMA, EMA, MACD, BBANDS, STOCH, ATR")
    params: dict[str, Any] = Field(default_factory=dict)


class StrategyRules(BaseModel):
    """Entry/exit logic as restricted boolean expressions.

    Allowed names in expressions: indicator `ref`s, plus `price`, `open`,
    `high`, `low`, `close`, `volume`, `last_digit`. Allowed operators:
    comparison, and/or/not, arithmetic. No function calls, no attribute
    access -> the backtest engine evaluates these in a sandbox.
    """

    entry: str = Field(description="boolean expression; True => open a trade")
    exit: str | None = Field(
        default=None, description="boolean expression; True => close the trade"
    )
    direction: str | None = Field(
        default=None,
        description="for rise_fall/higher_lower: expression or 'up'/'down'",
    )
    prediction: int | None = Field(
        default=None,
        description="for digit trades (even_odd/over_under/matches): the digit/barrier",
    )
    duration: int = Field(default=1, description="contract duration")
    duration_unit: str = Field(default="t", description="t=ticks, s, m, h")


class StrategyStatus(str, Enum):
    EXTRACTED = "extracted"  # produced by research, not yet tested
    QUEUED = "queued"  # accepted by logging/backtest app
    SURVIVED = "survived"  # passed backtest
    REJECTED = "rejected"  # failed backtest


class Strategy(BaseModel):
    """A testable trading strategy extracted from researched content."""

    id: str = Field(default_factory=_new_id)
    name: str
    description: str = ""
    trade_type: TradeType
    symbols: list[str] = Field(
        default_factory=list,
        description="Deriv symbols, e.g. R_100, R_50, frxEURUSD; empty = any",
    )
    timeframe: str = Field(default="1m", description="e.g. 1t, 1m, 5m")
    indicators: list[Indicator] = Field(default_factory=list)
    rules: StrategyRules
    params: dict[str, Any] = Field(default_factory=dict)

    provenance: Provenance
    raw_excerpt: str = Field(
        default="",
        description="the snippet of source content this was derived from",
    )
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0, description="LLM self-rated extraction confidence"
    )
    tags: list[str] = Field(default_factory=list)

    status: StrategyStatus = StrategyStatus.EXTRACTED
    created_at: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------------------
# Insights (non-strategy intel: updates, news, sentiment, warnings)
# ---------------------------------------------------------------------------


class FocusMode(str, Enum):
    """What the research run should pay attention to."""

    STRATEGIES = "strategies"  # testable trading rules
    UPDATES = "updates"  # platform updates / news / sentiment / warnings


class InsightCategory(str, Enum):
    RULE = "rule"  # how a Deriv contract/market actually works (open or
    #                undocumented-but-reported behaviour)
    UPDATE = "update"  # platform/feature/API change
    NEWS = "news"  # broker or market news
    SENTIMENT = "sentiment"  # community mood / opinion
    WARNING = "warning"  # scam / risky-broker / outage chatter
    EDUCATION = "education"  # general teaching, not a concrete strategy
    OTHER = "other"


class Sentiment(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    NA = "n/a"


class Insight(BaseModel):
    """A non-strategy takeaway: what people are saying about Deriv."""

    id: str = Field(default_factory=_new_id)
    category: InsightCategory
    summary: str = Field(description="one-line takeaway")
    details: str = ""
    sentiment: Sentiment = Sentiment.NA
    topics: list[str] = Field(default_factory=list)
    hashtags: list[str] = Field(default_factory=list)

    trade_type: TradeType | None = Field(
        default=None,
        description="most relevant Deriv trade type, or null if general",
    )
    provenance: Provenance
    raw_excerpt: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    status: str = "collected"
    created_at: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------------------
# Internal pipeline model
# ---------------------------------------------------------------------------


class RawDocument(BaseModel):
    """Normalized content pulled from any source, fed to the extractor."""

    platform: SourcePlatform
    url: str
    title: str | None = None
    author: str | None = None
    published_at: str | None = None
    text: str = Field(description="transcript / article body / post text")


# ---------------------------------------------------------------------------
# API request / response
# ---------------------------------------------------------------------------


class ResearchRequest(BaseModel):
    trade_type: TradeType
    query: str = Field(
        description="what to research, e.g. 'even odd digit strategy R_100'"
    )
    sources: list[SourcePlatform] = Field(
        default_factory=lambda: [
            SourcePlatform.WEB,
            SourcePlatform.YOUTUBE,
            SourcePlatform.REDDIT,
        ]
    )
    max_results_per_source: int | None = None
    push_to_logging_app: bool = True
    focus: list[FocusMode] = Field(
        default_factory=lambda: [FocusMode.STRATEGIES]
    )
    hashtags: list[str] = Field(
        default_factory=list,
        description="hashtags to fold into the search, e.g. ['#deriv']",
    )


class SourceError(BaseModel):
    platform: SourcePlatform
    error: str


class ResearchResponse(BaseModel):
    request: ResearchRequest
    documents_found: int
    strategies: list[Strategy]
    insights: list[Insight] = Field(default_factory=list)
    pushed: int
    insights_pushed: int = 0
    errors: list[SourceError] = Field(default_factory=list)
    finished_at: datetime = Field(default_factory=_utcnow)


class StudyLinkRequest(BaseModel):
    """Paste a link you've seen; J81 fetches it, studies it, extracts."""

    url: str = Field(description="the URL to study")
    trade_type: TradeType = TradeType.RISE_FALL
    focus: list[FocusMode] = Field(
        default_factory=lambda: [FocusMode.STRATEGIES, FocusMode.UPDATES]
    )
    push_to_logging_app: bool = True


class StudyLinkResponse(BaseModel):
    url: str
    fetched: bool
    platform: SourcePlatform | None = None
    title: str | None = None
    strategies: list[Strategy] = Field(default_factory=list)
    insights: list[Insight] = Field(default_factory=list)
    pushed: int = 0
    insights_pushed: int = 0
    error: str | None = None
    finished_at: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------------------
# Connected-bot trade recording (multi-tenant trade journal)
# ---------------------------------------------------------------------------


class AccountRegistration(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class Account(BaseModel):
    id: str
    name: str
    created_at: datetime


class AccountCredentials(Account):
    """Returned ONCE at registration; api_key is not stored in plaintext."""

    api_key: str


class IncomingTrade(BaseModel):
    """A trade as sent by a connected bot. Unknown keys are kept verbatim
    (extra='allow') so any bot's payload is recorded losslessly; known keys
    are normalized into queryable columns.
    """

    model_config = ConfigDict(extra="allow")

    external_id: str | None = Field(
        default=None, description="the bot's own trade id; used to dedupe"
    )
    symbol: str | None = None
    trade_type: str | None = None
    direction: str | None = None
    stake: float | None = None
    payout: float | None = None
    profit: float | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    entry_time: str | None = None
    exit_time: str | None = None
    status: str | None = None
    currency: str | None = None


class TradeIngestRequest(BaseModel):
    trades: list[IncomingTrade]


class TradeIngestResult(BaseModel):
    recorded: int
    duplicates_skipped: int
    account_id: str


class TradeRecord(BaseModel):
    """A stored trade."""

    id: str
    account_id: str
    external_id: str | None = None
    symbol: str | None = None
    trade_type: str | None = None
    direction: str | None = None
    stake: float | None = None
    payout: float | None = None
    profit: float | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    entry_time: str | None = None
    exit_time: str | None = None
    status: str | None = None
    currency: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
    recorded_at: datetime


class TradeStats(BaseModel):
    account_id: str
    total: int
    wins: int
    losses: int
    win_rate: float
    total_profit: float
    total_stake: float
    by_symbol: dict[str, int] = Field(default_factory=dict)
