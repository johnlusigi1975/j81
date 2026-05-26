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
    SourcePlatform,
    Strategy,
    StudyLinkRequest,
    StudyLinkResponse,
)
from app.research_config import load_config
from app.sharing import send_to_analyser
from app.sources.registry import get_adapter
from app.url_fetcher import UnsafeURLError, fetch_url

_MAX_CONCURRENT_EXTRACTIONS = 4

# Dedupe maintenance reports: only file the same root-cause issue once per
# process so the panel doesn't fill with identical "extractor failed" entries.
_REPORTED_ROOTCAUSES: set[str] = set()


async def _report_root_cause(summary: str, *, severity: str = "warning",
                              area: str = "researcher/extractor",
                              detail: str | None = None) -> None:
    """Surface a silent extractor failure to the maintenance bus — exactly
    once per (summary, severity) pair, so the user sees the root cause in
    the maintenance card instead of just `0 strategies, 0 insights`."""
    key = f"{severity}|{area}|{summary[:120]}"
    if key in _REPORTED_ROOTCAUSES:
        return
    _REPORTED_ROOTCAUSES.add(key)
    try:
        from app import comms_client
        await comms_client.report_issue(
            summary, severity=severity, area=area, detail=detail,
        )
    except Exception:
        pass  # bus best-effort; never break extraction


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

        from app.config import get_settings as _get
        if not _get().anthropic_api_key:
            return StudyLinkResponse(
                url=req.url,
                fetched=True,
                platform=doc.platform,
                title=doc.title,
                error="LLM not configured: ANTHROPIC_API_KEY is missing — "
                "the page was fetched but no strategies/insights could be "
                "extracted. Add the key to research-app/.env and refresh.",
            )

        strategies: list[Strategy] = []
        insights: list[Insight] = []
        extraction_errors: list[str] = []
        if FocusMode.STRATEGIES in req.focus:
            try:
                strategies = await self._extractor.extract(
                    doc, req.trade_type
                )
            except Exception as exc:
                extraction_errors.append(f"strategy extraction: {exc!r}")
        if FocusMode.UPDATES in req.focus:
            try:
                insights = await self._extractor.extract_insights(doc)
            except Exception as exc:
                extraction_errors.append(f"insight extraction: {exc!r}")

        # ---- broaden on shortfall ------------------------------------
        # If the user asked for strategies and we came up short, broaden the
        # search using terms from the page and try again. Hard-capped by
        # config to prevent runaway costs.
        cfg = load_config()
        ext = cfg.extraction
        if (
            FocusMode.STRATEGIES in req.focus
            and ext.min_strategies_target > 0
            and len(strategies) < ext.min_strategies_target
            and ext.max_broaden_iterations > 0
        ):
            seen_urls = {doc.url}
            for i in range(ext.max_broaden_iterations):
                if len(strategies) >= ext.min_strategies_target:
                    break
                broader_query = self._derive_broader_query(doc, req, i)
                try:
                    sub = await self.run(
                        ResearchRequest(
                            trade_type=req.trade_type,
                            query=broader_query,
                            sources=[
                                SourcePlatform.WEB,
                                SourcePlatform.YOUTUBE,
                                SourcePlatform.REDDIT,
                            ],
                            focus=[FocusMode.STRATEGIES],
                            push_to_logging_app=False,
                            max_results_per_source=ext.broaden_results_per_source,
                        )
                    )
                except Exception:
                    continue
                for s in sub.strategies:
                    if s.provenance.url in seen_urls:
                        continue
                    seen_urls.add(s.provenance.url)
                    strategies.append(s)

        pushed = insights_pushed = 0
        if req.push_to_logging_app:
            if strategies:
                pushed = await self._logging.push(strategies)
            if insights:
                insights_pushed = await self._logging.push_insights(insights)

        # If the user has "auto-send after each studied link" enabled, ship
        # the whole library to the analyser right now (and archive if set).
        if load_config().sharing.auto_send_after_study_link:
            try:
                await send_to_analyser()
            except Exception:
                # Best-effort; don't fail the study because of a downstream issue.
                pass

        return StudyLinkResponse(
            url=req.url,
            fetched=True,
            platform=doc.platform,
            title=doc.title,
            strategies=strategies,
            insights=insights,
            pushed=pushed,
            insights_pushed=insights_pushed,
            error="; ".join(extraction_errors) if extraction_errors else None,
        )

    def _derive_broader_query(
        self, doc: RawDocument, req: StudyLinkRequest, iteration: int
    ) -> str:
        """Build a broader search query for the broaden-on-shortfall loop.

        Iteration 0: use the page title + trade type + "deriv strategy".
        Iteration 1: drop the title, keep the trade type — wider net.
        Iteration 2: pure trade-type sweep, no page-specific terms.
        """
        tt = req.trade_type.value.replace("_", " ")
        if iteration == 0 and doc.title:
            seed = doc.title[:80]
            return f"{seed} deriv {tt} strategy"
        if iteration == 1 and doc.title:
            # take only the first few words of the title as a topic seed
            words = doc.title.split()[:3]
            return f"{' '.join(words)} deriv {tt} strategy rules"
        return f"deriv {tt} strategy rules indicators"

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
                    except Exception as exc:
                        # Don't swallow silently — file a maintenance issue so
                        # the user sees the root cause (e.g. missing API key,
                        # provider mis-config, schema rejection).
                        await _report_root_cause(
                            f"extractor.extract failed: {type(exc).__name__}",
                            severity="error",
                            detail=repr(exc)[:300],
                        )
                        strategies = []
                if want_insights:
                    try:
                        insights = await self._extractor.extract_insights(doc)
                    except Exception as exc:
                        await _report_root_cause(
                            f"extractor.extract_insights failed: {type(exc).__name__}",
                            severity="error",
                            detail=repr(exc)[:300],
                        )
                        insights = []
                return strategies, insights

        nested = await asyncio.gather(*(extract_one(d) for d in docs))
        all_strategies = [s for st, _ in nested for s in st]
        all_insights = [i for _, ins in nested for i in ins]

        # If we found sources but extracted nothing, that's the silent-yield
        # case the user kept seeing in the maintenance log. File ONE warning
        # so they know the LLM call ran but the model couldn't pull a strategy
        # — usually means the LLM key is missing OR the content was noisy.
        if docs and not all_strategies and not all_insights:
            await _report_root_cause(
                f"0-yield extraction across {len(docs)} sources "
                f"(trade_type={getattr(request.trade_type, 'value', request.trade_type)})",
                severity="warning",
                detail=("Either the LLM provider key is missing in the "
                        "researcher service (GOOGLE_API_KEY for Gemini, or "
                        "ANTHROPIC_API_KEY for Claude — see render.yaml), or "
                        "the sources lacked rule-based content. Check the "
                        "researcher logs for the extractor trail."),
            )
        return all_strategies, all_insights
