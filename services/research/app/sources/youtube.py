"""YouTube adapter.

If YOUTUBE_API_KEY is set, J81 uses the official YouTube Data API v3 for
search (stable, structured) — then pulls transcripts via
youtube-transcript-api as before. Without a key it falls back to yt-dlp
scrape search (works but is fragile when YouTube changes pages).
"""

from __future__ import annotations

import asyncio

import httpx

from app.config import get_settings
from app.models import RawDocument, SourcePlatform
from app.sources.base import SourceAdapter

_API_SEARCH = "https://www.googleapis.com/youtube/v3/search"


class YouTubeSource(SourceAdapter):
    platform = SourcePlatform.YOUTUBE

    async def search(self, query: str, max_results: int) -> list[RawDocument]:
        settings = get_settings()
        if settings.youtube_api_key:
            try:
                entries = await self._api_search(query, max_results, settings)
            except Exception:
                entries = await asyncio.to_thread(
                    self._ytdlp_entries, query, max_results
                )
        else:
            entries = await asyncio.to_thread(
                self._ytdlp_entries, query, max_results
            )

        # Transcript fetching is blocking; isolate per-video failures.
        return await asyncio.to_thread(
            self._fetch_transcripts, entries, max_results
        )

    # --- Data API v3 (preferred) -----------------------------------------

    async def _api_search(
        self, query: str, max_results: int, settings
    ) -> list[dict]:
        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": min(50, max_results * 2),
            "key": settings.youtube_api_key,
            # NOTE: no relevanceLanguage filter — we accept content in any
            # language and translate downstream in the extractor.
        }
        async with httpx.AsyncClient(
            timeout=settings.request_timeout_seconds
        ) as client:
            resp = await client.get(_API_SEARCH, params=params)
            resp.raise_for_status()
            data = resp.json()
        out = []
        for item in data.get("items", []):
            vid = (item.get("id") or {}).get("videoId")
            sn = item.get("snippet") or {}
            if vid:
                out.append(
                    {"id": vid, "title": sn.get("title"), "uploader": sn.get("channelTitle")}
                )
        return out

    # --- yt-dlp scrape (fallback) ----------------------------------------

    def _ytdlp_entries(self, query: str, max_results: int) -> list[dict]:
        import yt_dlp

        opts = {"quiet": True, "skip_download": True, "extract_flat": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"ytsearch{max_results * 2}:{query}", download=False
            )
        return (info or {}).get("entries") or []

    # --- shared transcript step ------------------------------------------

    def _fetch_transcripts(
        self, entries: list[dict], max_results: int
    ) -> list[RawDocument]:
        from youtube_transcript_api import YouTubeTranscriptApi

        ytt = YouTubeTranscriptApi()
        docs: list[RawDocument] = []
        for entry in entries:
            if len(docs) >= max_results:
                break
            video_id = entry.get("id")
            if not video_id:
                continue
            fetched = _fetch_transcript_any_language(ytt, video_id)
            if fetched is None:
                continue
            text = " ".join(s.text for s in fetched if s.text)
            if len(text) < 200:
                continue
            docs.append(
                RawDocument(
                    platform=self.platform,
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    title=entry.get("title"),
                    author=entry.get("uploader") or entry.get("channel"),
                    text=text[:20000],
                )
            )
        return docs


def _fetch_transcript_any_language(ytt, video_id):
    """Get a transcript in whatever language is available, preferring manual
    captions (more accurate) over auto-generated."""
    try:
        transcripts = list(ytt.list(video_id))
    except Exception:
        return None
    if not transcripts:
        return None
    transcripts.sort(key=lambda t: getattr(t, "is_generated", True))
    for t in transcripts:
        try:
            return t.fetch()
        except Exception:
            continue
    return None
