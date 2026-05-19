"""TikTok adapter. Best-effort: discovers public video URLs via web search,
pulls title/description via yt-dlp, optionally Whisper-transcribes audio.
See app/sources/_short_video.py for constraints.
"""

from __future__ import annotations

from app.models import RawDocument, SourcePlatform
from app.sources._short_video import search_short_video
from app.sources.base import SourceAdapter


class TikTokSource(SourceAdapter):
    platform = SourcePlatform.TIKTOK

    async def search(self, query: str, max_results: int) -> list[RawDocument]:
        return await search_short_video(
            query, max_results, self.platform, "tiktok.com"
        )
