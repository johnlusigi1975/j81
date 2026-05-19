"""YouTube via yt-dlp search (no API key) + youtube-transcript-api for
captions. Videos without transcripts are skipped.
"""

from __future__ import annotations

import asyncio

from app.models import RawDocument, SourcePlatform
from app.sources.base import SourceAdapter


class YouTubeSource(SourceAdapter):
    platform = SourcePlatform.YOUTUBE

    async def search(self, query: str, max_results: int) -> list[RawDocument]:
        return await asyncio.to_thread(self._search_sync, query, max_results)

    def _search_sync(self, query: str, max_results: int) -> list[RawDocument]:
        import yt_dlp
        from youtube_transcript_api import YouTubeTranscriptApi

        opts = {"quiet": True, "skip_download": True, "extract_flat": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"ytsearch{max_results * 2}:{query}", download=False
            )
        entries = (info or {}).get("entries") or []

        ytt = YouTubeTranscriptApi()
        docs: list[RawDocument] = []
        for entry in entries:
            if len(docs) >= max_results:
                break
            video_id = entry.get("id")
            if not video_id:
                continue
            try:
                fetched = ytt.fetch(video_id, languages=["en", "en-US"])
            except Exception:
                # No transcript / disabled / unavailable / region-blocked.
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
