"""Turns researched content into structured, testable Strategy objects using
Claude (tool use + client-side Pydantic validation).

Design notes:
  * The LLM only produces the *strategy logic* (LLMStrategy). Provenance, IDs,
    timestamps and status are attached in code so the model can't fabricate
    them.
  * We use a forced tool call rather than strict structured outputs because
    indicator/strategy `params` are free-form dicts, which strict JSON-schema
    output doesn't handle well. We validate the tool input with Pydantic.
  * The system prompt + tool schema are prompt-cached. Tools render before the
    system block in the cache prefix, so a cache_control marker on the system
    block caches both together across every document in a research run.
"""

from __future__ import annotations

import json

import anthropic
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

MAX_TOKENS = 16000


# --- What we ask the model to produce (no provenance/id/status) ------------


class LLMStrategy(BaseModel):
    name: str
    description: str = ""
    trade_type: TradeType
    symbols: list[str] = Field(default_factory=list)
    timeframe: str = "1m"
    indicators: list[Indicator] = Field(default_factory=list)
    rules: StrategyRules
    params: dict = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)
    raw_excerpt: str = ""
    source_language: str | None = None


class LLMStrategies(BaseModel):
    strategies: list[LLMStrategy] = Field(default_factory=list)


SYSTEM_PROMPT = """You are a quantitative trading-strategy extraction engine \
for the Deriv platform. You read researched content (video transcripts, \
articles, forum posts) about trading and extract any concrete, rule-based \
trading strategies into a strict machine-testable format.

Multilingual input: the source content may be in ANY language (English, \
Spanish, Portuguese, French, Arabic, Hindi, Swahili, Chinese, Japanese, \
Indonesian, Russian, etc.). Read and understand it natively. Translate \
the user-facing fields (`name`, `description`, `tags`) into clear English. \
Indicator names, rule expressions, symbols, and timeframes are technical \
and should stay in their canonical English/ASCII form. Keep `raw_excerpt` \
VERBATIM in the original source language — do not translate it. Detect the \
content's language and put its ISO 639-1 code in `source_language` \
(e.g. 'en','es','pt','fr','ar','hi','sw','zh','ja','ru','id'); use 'mixed' \
if multilingual, omit if unknown.

Hard rules:
- Only extract strategies that have OBJECTIVE, testable entry/exit logic. \
Discard vague advice ("trade with the trend", "manage risk"), hype, signal-\
selling promotions, and anything that cannot be expressed as a rule.
- If the content contains no testable strategy, return an empty list.
- Never invent indicators or thresholds the source did not state. If a needed \
parameter is missing, make the smallest reasonable assumption and lower the \
confidence accordingly.

Strategy format:
- trade_type is one of: rise_fall, even_odd, over_under, matches_differs, \
touch_no_touch, higher_lower, asian.
- indicators[]: each has `ref` (short name used in expressions), `type` \
(RSI, SMA, EMA, MACD, BBANDS, STOCH, ATR, ...), and `params` (e.g. {"period": 14}).
- rules.entry / rules.exit are boolean expressions. Allowed names: each \
indicator `ref`, plus price, open, high, low, close, volume, last_digit. \
Allowed operators: < <= > >= == != and or not + - * / and parentheses. \
NO function calls, NO attribute access, NO assignment.
- For digit trades (even_odd/over_under/matches_differs) set rules.prediction \
to the predicted digit/barrier. For rise_fall/higher_lower set rules.direction \
to 'up', 'down', or an expression.
- rules.duration / duration_unit describe the contract length (unit: t ticks, \
s, m, h).
- confidence (0..1): how clearly and completely the source specified the \
strategy. Be conservative.
- raw_excerpt: the short snippet of source text the strategy came from.

Return results ONLY by calling the submit_strategies tool."""


def _tool_schema() -> dict:
    inner = LLMStrategies.model_json_schema()
    return {
        "name": "submit_strategies",
        "description": (
            "Submit every testable trading strategy found in the content. "
            "Call with an empty list if none are testable."
        ),
        "input_schema": inner,
    }


class LLMInsight(BaseModel):
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


class LLMInsights(BaseModel):
    insights: list[LLMInsight] = Field(default_factory=list)


INSIGHT_SYSTEM_PROMPT = """You monitor what people online are saying about \
the Deriv trading platform (and its synthetic indices / binary trading). You \
read researched content and extract concise, factual INSIGHTS — not trading \
strategies.

Multilingual input: source content may be in ANY language. Translate \
`summary` and `details` into clear English so downstream systems and the \
backtester read a single language. Keep `raw_excerpt` VERBATIM in the \
original language (no translation) for audit. Detect the language and set \
`source_language` to its ISO 639-1 code (e.g. 'en','es','pt','fr','ar', \
'hi','sw','zh','ja','ru','id'); use 'mixed' if multilingual.

Capture things like:
- rule: HOW a Deriv contract or market actually works — payout/multiplier \
formulas, barrier/duration/tick rules, digit ranges, entry/exit timing, \
restrictions, account/limits. Include both officially documented ("open") \
rules AND undocumented or "hidden" behaviours that traders report having \
discovered through experience. For an undocumented one, say so in `details` \
(e.g. "not in official docs; reported by traders").
- update: platform/app/API/feature/payout/account changes.
- news: broker or market news relevant to Deriv traders.
- sentiment: the community mood or strong opinions (good or bad experiences).
- warning: scam claims, withdrawal problems, outages, risky-bot promotions.
- education: general teaching that is NOT a concrete testable strategy.

Rules:
- One insight per distinct point. Keep `summary` to a single clear sentence.
- Only include things actually stated in the content. No speculation. For \
"hidden" rules, only report what the source actually claims — do not invent \
mechanics; lower `confidence` when it is anecdotal.
- Set `trade_type` to the Deriv trade type the insight is about \
(rise_fall, even_odd, over_under, matches_differs, touch_no_touch, \
higher_lower, asian) when it is specific to one; leave it null if general.
- Set sentiment to bullish/bearish/neutral when there is a clear directional \
opinion about Deriv or a market, else n/a.
- Pull any hashtags present into `hashtags`.
- If the content has nothing noteworthy about Deriv, return an empty list.

Return results ONLY by calling the submit_insights tool."""


def _insight_tool_schema() -> dict:
    return {
        "name": "submit_insights",
        "description": (
            "Submit every noteworthy Deriv-related insight: contract/market "
            "RULES (documented or undocumented), updates, news, sentiment, "
            "warnings. Empty list if nothing noteworthy."
        ),
        "input_schema": LLMInsights.model_json_schema(),
    }


class StrategyExtractor:
    def __init__(self) -> None:
        settings = get_settings()
        self._model = settings.extractor_model
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._tool = _tool_schema()
        self._insight_tool = _insight_tool_schema()

    async def extract(
        self, doc: RawDocument, trade_type: TradeType
    ) -> list[Strategy]:
        user_content = (
            f"Target trade type: {trade_type.value}\n"
            f"Source platform: {doc.platform.value}\n"
            f"Source URL: {doc.url}\n"
            f"Title: {doc.title or '(none)'}\n\n"
            f"--- CONTENT START ---\n{doc.text}\n--- CONTENT END ---"
        )

        response = await self._client.messages.create(
            model=self._model,
            max_tokens=MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[self._tool],
            tool_choice={"type": "tool", "name": "submit_strategies"},
            messages=[{"role": "user", "content": user_content}],
        )

        tool_block = next(
            (b for b in response.content if b.type == "tool_use"), None
        )
        if tool_block is None:
            return []

        try:
            parsed = LLMStrategies.model_validate(tool_block.input)
        except ValidationError:
            # Model returned something off-schema; skip this doc rather than
            # crash the whole research run.
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
                indicators=s.indicators,
                rules=s.rules,
                params=s.params,
                provenance=provenance,
                raw_excerpt=s.raw_excerpt or doc.text[:500],
                source_language=s.source_language,
                confidence=s.confidence,
                tags=s.tags,
            )
            for s in parsed.strategies
        ]

    async def extract_insights(self, doc: RawDocument) -> list[Insight]:
        user_content = (
            f"Source platform: {doc.platform.value}\n"
            f"Source URL: {doc.url}\n"
            f"Title: {doc.title or '(none)'}\n\n"
            f"--- CONTENT START ---\n{doc.text}\n--- CONTENT END ---"
        )

        response = await self._client.messages.create(
            model=self._model,
            max_tokens=MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": INSIGHT_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[self._insight_tool],
            tool_choice={"type": "tool", "name": "submit_insights"},
            messages=[{"role": "user", "content": user_content}],
        )

        tool_block = next(
            (b for b in response.content if b.type == "tool_use"), None
        )
        if tool_block is None:
            return []

        try:
            parsed = LLMInsights.model_validate(tool_block.input)
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
