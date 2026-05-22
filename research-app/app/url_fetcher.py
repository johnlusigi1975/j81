"""Fetch and normalize a single user-supplied URL into a RawDocument.

Routes by domain so each platform is studied the right way:
  * YouTube      -> transcript (youtube-transcript-api) + yt-dlp metadata
  * TikTok / IG   -> yt-dlp metadata (+ optional Whisper) via _short_video
  * Reddit        -> public JSON (title + selftext)
  * anything else -> trafilatura main-text extraction

SSRF guard: users paste arbitrary links and this runs on your VPS, so we
refuse non-http(s) schemes and any host that resolves to a private,
loopback, link-local, or otherwise non-public address (blocks cloud
metadata endpoints, internal services, etc.).
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlparse

import httpx

from app.config import get_settings
from app.models import RawDocument, SourcePlatform
from app.sources._short_video import _extract_one

_UA = "j81-deriv-researcher/0.3 (link study)"


class UnsafeURLError(ValueError):
    pass


_NAT64_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")


def _is_unsafe(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Reject only addresses that are actually local/internal. For NAT64-
    wrapped IPv4 (common on dual-stack WSL2 / IPv6-only networks), unwrap
    to the underlying IPv4 first — the wrapper itself is "reserved" per
    RFC 6052, but what matters is whether the real destination is public."""
    if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64_PREFIX:
        ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True
    # is_reserved is meaningful for IPv4 (catches 240.0.0.0/4 etc.)
    # but overly broad on IPv6 once we've stripped NAT64.
    if isinstance(ip, ipaddress.IPv4Address) and ip.is_reserved:
        return True
    return False


def _assert_safe_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError("only http/https URLs are allowed")
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"cannot resolve host: {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _is_unsafe(ip):
            raise UnsafeURLError(
                f"host {host} resolves to a non-public address ({ip})"
            )


def _detect_platform(url: str) -> SourcePlatform:
    host = (urlparse(url).hostname or "").lower()
    if "youtube.com" in host or "youtu.be" in host:
        return SourcePlatform.YOUTUBE
    if "tiktok.com" in host:
        return SourcePlatform.TIKTOK
    if "instagram.com" in host:
        return SourcePlatform.INSTAGRAM
    if "reddit.com" in host:
        return SourcePlatform.REDDIT
    return SourcePlatform.WEB


def _fetch_youtube(url: str) -> RawDocument | None:
    import yt_dlp
    from youtube_transcript_api import YouTubeTranscriptApi

    with yt_dlp.YoutubeDL(
        {"quiet": True, "skip_download": True, "no_warnings": True}
    ) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    video_id = info.get("id")
    if not video_id:
        return None
    try:
        fetched = YouTubeTranscriptApi().fetch(
            video_id, languages=["en", "en-US"]
        )
    except Exception:
        return None
    text = " ".join(s.text for s in fetched if s.text)
    if len(text) < 100:
        return None
    return RawDocument(
        platform=SourcePlatform.YOUTUBE,
        url=url,
        title=info.get("title"),
        author=info.get("uploader") or info.get("channel"),
        text=text[:20000],
    )


def _fetch_reddit(url: str) -> RawDocument | None:
    json_url = re.sub(r"/?$", "", url) + "/.json"
    try:
        resp = httpx.get(
            json_url,
            headers={"User-Agent": _UA},
            timeout=get_settings().request_timeout_seconds,
            follow_redirects=True,
        )
        resp.raise_for_status()
        data = resp.json()
        post = data[0]["data"]["children"][0]["data"]
    except Exception:
        return _fetch_generic(url, SourcePlatform.REDDIT)
    title = post.get("title") or ""
    body = (post.get("selftext") or "").strip()
    text = f"{title}\n\n{body}".strip()
    if len(text) < 80:
        return None
    return RawDocument(
        platform=SourcePlatform.REDDIT,
        url=url,
        title=title,
        author=post.get("author"),
        text=text[:20000],
    )


def _fetch_generic(
    url: str, platform: SourcePlatform = SourcePlatform.WEB
) -> RawDocument | None:
    import trafilatura

    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return None
    text = trafilatura.extract(
        downloaded, include_comments=False, include_tables=False
    )
    if not text or len(text) < 200:
        return None
    return RawDocument(
        platform=platform, url=url, title=None, text=text[:20000]
    )


def _fetch_sync(url: str) -> RawDocument | None:
    platform = _detect_platform(url)
    if platform == SourcePlatform.YOUTUBE:
        return _fetch_youtube(url)
    if platform in (SourcePlatform.TIKTOK, SourcePlatform.INSTAGRAM):
        return _extract_one(
            url, platform, get_settings().enable_whisper
        )
    if platform == SourcePlatform.REDDIT:
        return _fetch_reddit(url)
    return _fetch_generic(url, SourcePlatform.WEB)


async def fetch_url(url: str) -> RawDocument | None:
    """Validate, then fetch+normalize a single URL. Raises UnsafeURLError
    for disallowed/private targets."""
    await asyncio.to_thread(_assert_safe_url, url)
    return await asyncio.to_thread(_fetch_sync, url)
