"""Instagram adapter. Best-effort: discovers public reel/post URLs via web
search, pulls title/description via yt-dlp, optionally Whisper-transcribes
audio. Private content is not accessible. See _short_video.py for details.
"""

from __future__ import annotations

from app.models import RawDocument, SourcePlatform
from app.sources._short_video import search_short_video
from app.sources.base import SourceAdapter


class InstagramSource(SourceAdapter):
    platform = SourcePlatform.INSTAGRAM

    async def search(self, query: str, max_results: int) -> list[RawDocument]:
        return await search_short_video(
            query, max_results, self.platform, "instagram.com"
        )
