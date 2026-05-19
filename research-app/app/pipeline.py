"""Research pipeline: query sources -> extract strategies/insights -> push.

Per-source failures are isolated so one flaky platform (TikTok blocking,
Reddit rate-limit) never aborts the whole run.
"""

from __future__ import annotations

import asyncio

from app.config import get_settings
from app.extractor import StrategyExtractor
from app.hashtags import build_query
from app.logging_client import LoggingAppClient
from app.models import (
    FocusMode,
    Insight,
    RawDocument,
    ResearchRequest,
    ResearchResponse,
    SourceError,
    Strategy,
    StudyLinkRequest,
    StudyLinkResponse,
)
from app.sources.registry import get_adapter
from app.url_fetcher import UnsafeURLError, fetch_url

_MAX_CONCURRENT_EXTRACTIONS = 4


class ResearchPipeline:
    def __init__(self) -> None:
        self._extractor = StrategyExtractor()
        self._logging = LoggingAppClient()

    async def run(self, request: ResearchRequest) -> ResearchResponse:
        settings = get_settings()
        per_source = (
            request.max_results_per_source or settings.max_results_per_source
        )
        query = build_query(request.query, request.hashtags)

        docs, errors = await self._gather_documents(
            request, query, per_source
        )
        strategies, insights = await self._extract_all(docs, request)

        pushed = 0
        insights_pushed = 0
        if request.push_to_logging_app:
            if strategies:
                pushed = await self._logging.push(strategies)
            if insights:
                insights_pushed = await self._logging.push_insights(insights)

        return ResearchResponse(
            request=request,
            documents_found=len(docs),
            strategies=strategies,
            insights=insights,
            pushed=pushed,
            insights_pushed=insights_pushed,
            errors=errors,
        )

    async def study_url(self, req: StudyLinkRequest) -> StudyLinkResponse:
        """Fetch a single user-supplied link, study it, extract, push."""
        try:
            doc = await fetch_url(req.url)
        except UnsafeURLError as exc:
            return StudyLinkResponse(
                url=req.url, fetched=False, error=str(exc)
            )
        except Exception as exc:
            return StudyLinkResponse(
                url=req.url, fetched=False, error=f"fetch failed: {exc!r}"
            )
        if doc is None:
            return StudyLinkResponse(
                url=req.url,
                fetched=False,
                error="no usable content found at that URL",
            )

        strategies: list[Strategy] = []
        insights: list[Insight] = []
        if FocusMode.STRATEGIES in req.focus:
            try:
                strategies = await self._extractor.extract(
                    doc, req.trade_type
                )
            except Exception:
                strategies = []
        if FocusMode.UPDATES in req.focus:
            try:
                insights = await self._extractor.extract_insights(doc)
            except Exception:
                insights = []

        pushed = insights_pushed = 0
        if req.push_to_logging_app:
            if strategies:
                pushed = await self._logging.push(strategies)
            if insights:
                insights_pushed = await self._logging.push_insights(insights)

        return StudyLinkResponse(
            url=req.url,
            fetched=True,
            platform=doc.platform,
            title=doc.title,
            strategies=strategies,
            insights=insights,
            pushed=pushed,
            insights_pushed=insights_pushed,
        )

    async def _gather_documents(
        self, request: ResearchRequest, query: str, per_source: int
    ) -> tuple[list[RawDocument], list[SourceError]]:
        async def search_one(platform):
            adapter = get_adapter(platform)
            return await adapter.search(query, per_source)

        results = await asyncio.gather(
            *(search_one(p) for p in request.sources),
            return_exceptions=True,
        )

        docs: list[RawDocument] = []
        errors: list[SourceError] = []
        for platform, result in zip(request.sources, results):
            if isinstance(result, Exception):
                errors.append(
                    SourceError(platform=platform, error=repr(result))
                )
            else:
                docs.extend(result)
        return docs, errors

    async def _extract_all(
        self, docs: list[RawDocument], request: ResearchRequest
    ) -> tuple[list[Strategy], list[Insight]]:
        sem = asyncio.Semaphore(_MAX_CONCURRENT_EXTRACTIONS)
        want_strategies = FocusMode.STRATEGIES in request.focus
        want_insights = FocusMode.UPDATES in request.focus

        async def extract_one(
            doc: RawDocument,
        ) -> tuple[list[Strategy], list[Insight]]:
            async with sem:
                strategies: list[Strategy] = []
                insights: list[Insight] = []
                if want_strategies:
                    try:
                        strategies = await self._extractor.extract(
                            doc, request.trade_type
                        )
                    except Exception:
                        strategies = []
                if want_insights:
                    try:
                        insights = await self._extractor.extract_insights(doc)
                    except Exception:
                        insights = []
                return strategies, insights

        nested = await asyncio.gather(*(extract_one(d) for d in docs))
        all_strategies = [s for st, _ in nested for s in st]
        all_insights = [i for _, ins in nested for i in ins]
        return all_strategies, all_insights
