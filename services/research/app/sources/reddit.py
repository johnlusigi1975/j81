"""Reddit adapter.

If REDDIT_CLIENT_ID/SECRET are set, J81 authenticates as a registered Reddit
app (OAuth client_credentials, no user login required for read-only) and
uses the official `oauth.reddit.com` search endpoint — higher rate limits
and stable. Otherwise it falls back to the public www.reddit.com/.json
endpoint with a descriptive User-Agent.
"""

from __future__ import annotations

import time

import httpx

from app.config import get_settings
from app.models import RawDocument, SourcePlatform
from app.sources.base import SourceAdapter


class RedditSource(SourceAdapter):
    platform = SourcePlatform.REDDIT

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    async def search(self, query: str, max_results: int) -> list[RawDocument]:
        settings = get_settings()
        if settings.reddit_client_id and settings.reddit_client_secret:
            try:
                return await self._search_oauth(query, max_results, settings)
            except Exception:
                pass  # graceful fallback to keyless path
        return await self._search_public(query, max_results, settings)

    # --- OAuth (preferred) ------------------------------------------------

    async def _get_token(self, settings) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        async with httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            headers={"User-Agent": settings.reddit_user_agent},
            auth=(settings.reddit_client_id, settings.reddit_client_secret),
        ) as client:
            resp = await client.post(
                "https://www.reddit.com/api/v1/access_token",
                data={"grant_type": "client_credentials"},
            )
            resp.raise_for_status()
            data = resp.json()
        self._token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 3600)
        return self._token

    async def _search_oauth(
        self, query: str, max_results: int, settings
    ) -> list[RawDocument]:
        token = await self._get_token(settings)
        async with httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            headers={
                "User-Agent": settings.reddit_user_agent,
                "Authorization": f"Bearer {token}",
            },
        ) as client:
            resp = await client.get(
                "https://oauth.reddit.com/search",
                params={
                    "q": query,
                    "limit": str(max_results * 2),
                    "sort": "relevance",
                    "type": "link",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        return _parse_reddit_listing(data, max_results)

    # --- keyless public JSON (fallback) -----------------------------------

    async def _search_public(
        self, query: str, max_results: int, settings
    ) -> list[RawDocument]:
        async with httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            headers={"User-Agent": settings.reddit_user_agent},
        ) as client:
            resp = await client.get(
                "https://www.reddit.com/search.json",
                params={
                    "q": query,
                    "limit": str(max_results * 2),
                    "sort": "relevance",
                    "type": "link",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        return _parse_reddit_listing(data, max_results)


def _parse_reddit_listing(data: dict, max_results: int) -> list[RawDocument]:
    docs: list[RawDocument] = []
    for child in data.get("data", {}).get("children", []):
        if len(docs) >= max_results:
            break
        d = child.get("data", {})
        body = (d.get("selftext") or "").strip()
        title = d.get("title") or ""
        text = f"{title}\n\n{body}".strip()
        if len(text) < 120:
            continue
        docs.append(
            RawDocument(
                platform=SourcePlatform.REDDIT,
                url=f"https://www.reddit.com{d.get('permalink', '')}",
                title=title,
                author=d.get("author"),
                text=text[:20000],
            )
        )
    return docs
