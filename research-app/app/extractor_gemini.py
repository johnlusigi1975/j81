"""Gemini (Google) backend for the strategy/insight extractor.

Why a separate file: Gemini's `response_schema` rejects the untyped
`dict[str, Any]` fields the Anthropic path tolerates, so we use a
flattened set of Pydantic models here (`GeminiLLMStrategies` etc.) and
map them back to the real Strategy/Insight types on return.

Free-tier note: gemini-2.5-flash gives generous free quotas
(~15 RPM, ~1500/day at time of writing). Get a key at
https://aistudio.google.com → "Get API key".
"""

from __future__ import annotations

from pydantic import BaseModel, Field, ValidationError

from app.config import get_settings
from app.models import (
    Indicator,
    Insight,
    InsightCategory,
    Provenance,
    RawDocument,
    Sentiment,
    Strategy,
    StrategyRules,
    TradeType,
)


# ---------- Gemini-friendly schemas (no untyped dicts) -------------------


class GeminiIndicatorParams(BaseModel):
    """Named indicator params instead of a free dict, so Gemini's schema
    validator accepts it. Anything the LLM doesn't set stays None and
    we drop it when mapping back to the real Indicator.params."""

    period: int | None = None
    fast: int | None = None
    slow: int | None = None
    signal: int | None = None
    std_dev: float | None = None
    k_period: int | None = None
    d_period: int | None = None


class GeminiIndicator(BaseModel):
    ref: str
    type: str
    params: GeminiIndicatorParams = Field(default_factory=GeminiIndicatorParams)


class GeminiStrategy(BaseModel):
    name: str
    description: str = ""
    trade_type: TradeType
    symbols: list[str] = Field(default_factory=list)
    timeframe: str = "1m"
    indicators: list[GeminiIndicator] = Field(default_factory=list)
    rules: StrategyRules
    confidence: float = Field(ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)
    raw_excerpt: str = ""
    source_language: str | None = None


class GeminiStrategies(BaseModel):
    strategies: list[GeminiStrategy] = Field(default_factory=list)


class GeminiInsight(BaseModel):
    category: InsightCategory
    summary: str
    details: str = ""
    sentiment: Sentiment = Sentiment.NA
    trade_type: TradeType | None = None
    topics: list[str] = Field(default_factory=list)
    hashtags: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    raw_excerpt: str = ""
    source_language: str | None = None


class GeminiInsights(BaseModel):
    insights: list[GeminiInsight] = Field(default_factory=list)


# ---------- Gemini client (lazy import so import-time is cheap) ----------


def _client_and_model():
    from google import genai

    settings = get_settings()
    if not settings.google_api_key:
        raise RuntimeError(
            "GOOGLE_API_KEY is not set — add it to .env to use the Gemini "
            "backend (LLM_PROVIDER=google)"
        )
    client = genai.Client(api_key=settings.google_api_key)
    return client, settings.gemini_model


def _user_strategy_text(doc: RawDocument, trade_type: TradeType) -> str:
    return (
        f"Target trade type: {trade_type.value}\n"
        f"Source platform: {doc.platform.value}\n"
        f"Source URL: {doc.url}\n"
        f"Title: {doc.title or '(none)'}\n\n"
        f"--- CONTENT START ---\n{doc.text}\n--- CONTENT END ---"
    )


def _user_insight_text(doc: RawDocument) -> str:
    return (
        f"Source platform: {doc.platform.value}\n"
        f"Source URL: {doc.url}\n"
        f"Title: {doc.title or '(none)'}\n\n"
        f"--- CONTENT START ---\n{doc.text}\n--- CONTENT END ---"
    )


# ---------- the actual extraction calls ----------------------------------


async def extract_strategies(
    doc: RawDocument, trade_type: TradeType, system_prompt: str
) -> list[Strategy]:
    from google.genai import types

    client, model = _client_and_model()
    response = await client.aio.models.generate_content(
        model=model,
        contents=_user_strategy_text(doc, trade_type),
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=GeminiStrategies,
        ),
    )
    try:
        parsed = GeminiStrategies.model_validate_json(response.text or "{}")
    except ValidationError:
        return []

    provenance = Provenance(
        platform=doc.platform,
        url=doc.url,
        title=doc.title,
        author=doc.author,
        published_at=doc.published_at,
    )
    return [
        Strategy(
            name=s.name,
            description=s.description,
            trade_type=s.trade_type,
            symbols=s.symbols,
            timeframe=s.timeframe,
            indicators=[
                Indicator(
                    ref=i.ref,
                    type=i.type,
                    params=i.params.model_dump(exclude_none=True),
                )
                for i in s.indicators
            ],
            rules=s.rules,
            params={},  # gemini-path strategies don't expose top-level params
            provenance=provenance,
            raw_excerpt=s.raw_excerpt or doc.text[:500],
            source_language=s.source_language,
            confidence=s.confidence,
            tags=s.tags,
        )
        for s in parsed.strategies
    ]


async def extract_insights(
    doc: RawDocument, insight_system_prompt: str
) -> list[Insight]:
    from google.genai import types

    client, model = _client_and_model()
    response = await client.aio.models.generate_content(
        model=model,
        contents=_user_insight_text(doc),
        config=types.GenerateContentConfig(
            system_instruction=insight_system_prompt,
            response_mime_type="application/json",
            response_schema=GeminiInsights,
        ),
    )
    try:
        parsed = GeminiInsights.model_validate_json(response.text or "{}")
    except ValidationError:
        return []

    provenance = Provenance(
        platform=doc.platform,
        url=doc.url,
        title=doc.title,
        author=doc.author,
        published_at=doc.published_at,
    )
    return [
        Insight(
            category=i.category,
            summary=i.summary,
            details=i.details,
            sentiment=i.sentiment,
            trade_type=i.trade_type,
            topics=i.topics,
            hashtags=i.hashtags,
            provenance=provenance,
            raw_excerpt=i.raw_excerpt or doc.text[:500],
            source_language=i.source_language,
            confidence=i.confidence,
        )
        for i in parsed.insights
    ]
