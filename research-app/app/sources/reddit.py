"""Reddit via the public search JSON endpoint. No OAuth needed for light
read use, but a descriptive User-Agent is required or Reddit returns 429.
"""

from __future__ import annotations

import httpx

from app.models import RawDocument, SourcePlatform
from app.sources.base import SourceAdapter

_UA = "research-app/0.1 (deriv strategy research; contact: you@example.com)"


class RedditSource(SourceAdapter):
    platform = SourcePlatform.REDDIT

    async def search(self, query: str, max_results: int) -> list[RawDocument]:
        params = {
            "q": query,
            "limit": str(max_results * 2),
            "sort": "relevance",
            "type": "link",
        }
        async with httpx.AsyncClient(
            timeout=30, headers={"User-Agent": _UA}
        ) as client:
            resp = await client.get(
                "https://www.reddit.com/search.json", params=params
            )
            resp.raise_for_status()
            data = resp.json()

        docs: list[RawDocument] = []
        for child in data.get("data", {}).get("children", []):
            if len(docs) >= max_results:
                break
            d = child.get("data", {})
            body = (d.get("selftext") or "").strip()
            title = d.get("title") or ""
            text = f"{title}\n\n{body}".strip()
            if len(text) < 120:  # skip pure link posts with no discussion
                continue
            permalink = d.get("permalink", "")
            docs.append(
                RawDocument(
                    platform=self.platform,
                    url=f"https://www.reddit.com{permalink}",
                    title=title,
                    author=d.get("author"),
                    text=text[:20000],
                )
            )
        return docs
