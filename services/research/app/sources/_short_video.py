"""Shared logic for short-video platforms (TikTok, Instagram).

Honest constraints:
  * Neither platform offers an official search or transcript API.
  * We discover candidate URLs with a site-scoped web search, then use
    yt-dlp to pull title + description (always available for public posts).
  * The actual spoken strategy lives in the audio. If ENABLE_WHISPER is on
    and `faster-whisper` is installed, we download the audio and transcribe
    it; otherwise we work from title/description only (lower quality).
These adapters are best-effort and can break when the platforms change.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from app.config import get_settings
from app.models import RawDocument, SourcePlatform


def _discover_urls(query: str, site: str, max_results: int) -> list[str]:
    from ddgs import DDGS

    with DDGS() as ddgs:
        hits = list(ddgs.text(f"{query} site:{site}", max_results=max_results * 4))
    urls: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        url = hit.get("href") or hit.get("url") or ""
        if site in url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _whisper_transcribe(audio_path: str) -> str:
    settings = get_settings()
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return ""
    model = WhisperModel(settings.whisper_model, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(audio_path)
    return " ".join(seg.text for seg in segments).strip()


def _extract_one(
    url: str, platform: SourcePlatform, enable_whisper: bool
) -> RawDocument | None:
    import yt_dlp

    with tempfile.TemporaryDirectory() as tmp:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": not enable_whisper,
        }
        if enable_whisper:
            opts.update(
                {
                    "format": "bestaudio/best",
                    "outtmpl": str(Path(tmp) / "%(id)s.%(ext)s"),
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                        }
                    ],
                }
            )
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=enable_whisper)
        except Exception:
            return None
        if not info:
            return None

        title = info.get("title") or ""
        description = info.get("description") or ""
        parts = [p for p in (title, description) if p]

        if enable_whisper:
            audio = next(Path(tmp).glob("*.mp3"), None)
            if audio:
                spoken = _whisper_transcribe(str(audio))
                if spoken:
                    parts.append(spoken)

        text = "\n\n".join(parts).strip()
        if len(text) < 60:
            return None
        return RawDocument(
            platform=platform,
            url=url,
            title=title or None,
            author=info.get("uploader") or info.get("channel"),
            text=text[:20000],
        )


async def search_short_video(
    query: str, max_results: int, platform: SourcePlatform, site: str
) -> list[RawDocument]:
    settings = get_settings()
    urls = await asyncio.to_thread(_discover_urls, query, site, max_results)

    docs: list[RawDocument] = []
    for url in urls:
        if len(docs) >= max_results:
            break
        doc = await asyncio.to_thread(
            _extract_one, url, platform, settings.enable_whisper
        )
        if doc:
            docs.append(doc)
    return docs
