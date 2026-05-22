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

# IMPORTANT — we use FORCED tool_choice for both extractions
# ({"type": "tool", "name": "submit_strategies"} / submit_insights).
# That guarantees the model emits the structured tool call. With
# tool_choice "any" + extra server tools (web_search, web_fetch), Anthropic
# would let the model end the response WITHOUT calling our submit tool —
# producing 200 responses with empty results and no error, which is exactly
# the failure mode we were debugging.
# Forced tool_choice precludes other tools in the same call, so we don't
# list web_search/web_fetch here. We can re-add a SEPARATE pre-extraction
# search pass later if a topic needs grounding.

# Per-call diagnostic trail — written to <DATA_DIR>/logs/extractor-trail.log
# so the operator can see exactly what Claude returned (stop_reason, block
# counts, types) without burning more credits to guess.
def _trail_log(line: str) -> None:
    try:
        from datetime import datetime, timezone
        from app.config import data_path
        path = data_path("logs/extractor-trail.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with path.open("a") as f:
            f.write(f"{ts}  {line}\n")
    except Exception:
        pass  # logging should never break extraction


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
- ALWAYS attempt extraction. If the source mentions ANY rule-like behaviour \
(an indicator threshold, a digit target, a duration, an entry signal, an \
exit cue, a stake-management rule), emit a strategy with whatever fields \
you can populate confidently.
- For strategies that are MOSTLY testable but missing one piece (e.g. an \
exact threshold, period, or exit condition), STILL emit them with \
confidence <= 0.5 and leave the missing field at its default — never \
fabricate the missing parameter.
- Trade-type relevance: the request specifies a target trade_type. If the \
source describes that exact trade_type, emit strategies tagged with it. If \
the source describes a RELATED trade_type the user might still find useful \
(e.g. even/odd content when target is over/under), emit it anyway with the \
source's actual trade_type — the user can re-route later.
- Never invent indicators, trade types, or thresholds the source did not \
state. Lower confidence to reflect uncertainty.
- Auto-generated captions are noisy ("even OD" instead of "even odd", \
"running your boat" instead of "running your bot"). Read past the typos \
and extract the intended meaning.

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
- When a term/feature/rule the source mentions is unfamiliar to you, USE the \
web_search tool to look up what it means before deciding which category and \
how to summarise it. Multiple searches are fine.
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
        self._provider = (settings.llm_provider or "anthropic").strip().lower()
        self._model = settings.extractor_model
        # Anthropic client is only meaningfully used when provider=anthropic,
        # but instantiating is cheap and harmless either way.
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._tool = _tool_schema()
        self._insight_tool = _insight_tool_schema()

    # ---- public dispatch ------------------------------------------------

    async def extract(
        self, doc: RawDocument, trade_type: TradeType
    ) -> list[Strategy]:
        if self._provider == "google":
            from app.extractor_gemini import extract_strategies as _gemini
            return await _gemini(doc, trade_type, SYSTEM_PROMPT)
        return await self._extract_anthropic(doc, trade_type)

    async def extract_insights(self, doc: RawDocument) -> list[Insight]:
        if self._provider == "google":
            from app.extractor_gemini import extract_insights as _gemini_ins
            return await _gemini_ins(doc, INSIGHT_SYSTEM_PROMPT)
        return await self._extract_insights_anthropic(doc)

    # ---- Anthropic implementation (unchanged from earlier) --------------

    async def _extract_anthropic(
        self, doc: RawDocument, trade_type: TradeType
    ) -> list[Strategy]:
        user_content = (
            f"Target trade type: {trade_type.value}\n"
            f"Source platform: {doc.platform.value}\n"
            f"Source URL: {doc.url}\n"
            f"Title: {doc.title or '(none)'}\n\n"
            f"--- CONTENT START ---\n{doc.text}\n--- CONTENT END ---"
        )

        # Anthropic constraint: adaptive thinking + forced tool_choice are
        # mutually exclusive ("Thinking may not be enabled when tool_choice
        # forces tool use"). Picking forced submit over thinking because
        # silent-empty responses are worse than slightly less reasoning.
        # Future: a 2-stage flow (think → then forced submit) can restore
        # both.
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

        block_types = [b.type for b in response.content]
        _trail_log(
            f"extract url={doc.url!s:.80} tt={trade_type.value} "
            f"text_len={len(doc.text)} stop_reason={response.stop_reason} "
            f"blocks={block_types}"
        )

        tool_block = next(
            (
                b for b in response.content
                if b.type == "tool_use" and b.name == "submit_strategies"
            ),
            None,
        )
        if tool_block is None:
            _trail_log(
                f"extract NO_SUBMIT_TOOL_CALL url={doc.url!s:.80} "
                f"stop_reason={response.stop_reason}"
            )
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

    async def _extract_insights_anthropic(
        self, doc: RawDocument
    ) -> list[Insight]:
        user_content = (
            f"Source platform: {doc.platform.value}\n"
            f"Source URL: {doc.url}\n"
            f"Title: {doc.title or '(none)'}\n\n"
            f"--- CONTENT START ---\n{doc.text}\n--- CONTENT END ---"
        )

        # Same constraint as extract() above — see note there.
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

        block_types = [b.type for b in response.content]
        _trail_log(
            f"insights url={doc.url!s:.80} text_len={len(doc.text)} "
            f"stop_reason={response.stop_reason} blocks={block_types}"
        )

        tool_block = next(
            (
                b for b in response.content
                if b.type == "tool_use" and b.name == "submit_insights"
            ),
            None,
        )
        if tool_block is None:
            _trail_log(
                f"insights NO_SUBMIT_TOOL_CALL url={doc.url!s:.80} "
                f"stop_reason={response.stop_reason}"
            )
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
