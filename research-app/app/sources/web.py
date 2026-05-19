"""Articles, blogs and papers via keyless DuckDuckGo search + trafilatura
article extraction. Fully functional with no API key.
"""

from __future__ import annotations

import asyncio

from app.models import RawDocument, SourcePlatform
from app.sources.base import SourceAdapter

MIN_ARTICLE_CHARS = 400


class WebSource(SourceAdapter):
    platform = SourcePlatform.WEB

    async def search(self, query: str, max_results: int) -> list[RawDocument]:
        return await asyncio.to_thread(self._search_sync, query, max_results)

    def _search_sync(self, query: str, max_results: int) -> list[RawDocument]:
        from ddgs import DDGS  # imported lazily so the service starts without it
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
