"""Articles / blogs / papers — web search.

If TAVILY_API_KEY is set, J81 uses the Tavily Search API, which is
designed for research agents and returns extracted page content directly
(no separate fetch + trafilatura roundtrip per result). Otherwise it
falls back to keyless DuckDuckGo search + trafilatura extraction.
"""

from __future__ import annotations

import asyncio

import httpx

from app.config import get_settings
from app.models import RawDocument, SourcePlatform
from app.sources.base import SourceAdapter

MIN_ARTICLE_CHARS = 400
_TAVILY_ENDPOINT = "https://api.tavily.com/search"


class WebSource(SourceAdapter):
    platform = SourcePlatform.WEB

    async def search(self, query: str, max_results: int) -> list[RawDocument]:
        settings = get_settings()
        if settings.tavily_api_key:
            try:
                return await self._search_tavily(query, max_results, settings)
            except Exception:
                pass  # fall back to keyless path on any Tavily failure
        return await asyncio.to_thread(self._search_ddgs, query, max_results)

    # --- Tavily (preferred) ----------------------------------------------

    async def _search_tavily(
        self, query: str, max_results: int, settings
    ) -> list[RawDocument]:
        payload = {
            "api_key": settings.tavily_api_key,
            "query": query,
            "max_results": min(max_results * 2, 20),
            "search_depth": "advanced",
            "include_raw_content": True,
        }
        async with httpx.AsyncClient(
            timeout=settings.request_timeout_seconds
        ) as client:
            resp = await client.post(_TAVILY_ENDPOINT, json=payload)
            resp.raise_for_status()
            data = resp.json()

        docs: list[RawDocument] = []
        for r in data.get("results", []) or []:
            if len(docs) >= max_results:
                break
            url = r.get("url")
            text = (r.get("raw_content") or r.get("content") or "").strip()
            if not url or len(text) < MIN_ARTICLE_CHARS:
                continue
            docs.append(
                RawDocument(
                    platform=self.platform,
                    url=url,
                    title=r.get("title"),
                    text=text[:20000],
                )
            )
        return docs

    # --- DDGS + trafilatura (fallback) -----------------------------------

    def _search_ddgs(self, query: str, max_results: int) -> list[RawDocument]:
        from ddgs import DDGS
        import trafilatura

        docs: list[RawDocument] = []
        with DDGS() as ddgs:
            hits = list(ddgs.text(query, max_results=max_results * 3))

        for hit in hits:
            if len(docs) >= max_results:
                break
            url = hit.get("href") or hit.get("url")
            if not url:
                continue
            downloaded = trafilatura.fetch_url(url)
            if not downloaded:
                continue
            text = trafilatura.extract(
                downloaded, include_comments=False, include_tables=False
            )
            if not text or len(text) < MIN_ARTICLE_CHARS:
                continue
            docs.append(
                RawDocument(
                    platform=self.platform,
                    url=url,
                    title=hit.get("title"),
                    text=text[:20000],
                )
            )
        return docs
