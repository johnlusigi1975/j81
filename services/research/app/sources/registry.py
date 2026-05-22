from __future__ import annotations

from app.models import SourcePlatform
from app.sources.base import SourceAdapter
from app.sources.instagram import InstagramSource
from app.sources.reddit import RedditSource
from app.sources.tiktok import TikTokSource
from app.sources.web import WebSource
from app.sources.youtube import YouTubeSource

_REGISTRY: dict[SourcePlatform, SourceAdapter] = {
    SourcePlatform.WEB: WebSource(),
    SourcePlatform.YOUTUBE: YouTubeSource(),
    SourcePlatform.REDDIT: RedditSource(),
    SourcePlatform.TIKTOK: TikTokSource(),
    SourcePlatform.INSTAGRAM: InstagramSource(),
}


def get_adapter(platform: SourcePlatform) -> SourceAdapter:
    return _REGISTRY[platform]
