"""Social video posts (Instagram reels, XHS/小红书, TikTok, Threads) for on-demand share.

These platforms serve JS-heavy / anti-scraper pages whose server HTML has no usable
og:image or title — that's why an Instagram reel failed to post (fetch_url_meta hit a
login wall → no title → SHARE_FETCH_FAILED) and why XHS showed its red-book LOGO
instead of the video frame (the og:image served to scrapers is the brand mark).

yt-dlp's per-platform extractors get the REAL metadata (title, caption, the video's
cover frame), and — because the caption is often engagement-bait that doesn't describe
the actual content — we also transcribe the video's audio via Groq Whisper (reusing
asr.transcribe) so the judge can write a card about what the video actually shows.

Reuses yt-dlp + asr already in the pipeline. No new heavy dependencies.
"""
from __future__ import annotations

import os

import yt_dlp

# Hosts whose server HTML is JS-only / anti-scraper → route through yt-dlp.
SOCIAL_VIDEO_HOSTS = (
    "instagram.com", "xhslink.com", "xiaohongshu.com",
    "tiktok.com", "douyin.com", "threads.net", "threads.com",
)


def is_social_video_url(url: str) -> bool:
    return any(h in (url or "").lower() for h in SOCIAL_VIDEO_HOSTS)


def _groq_key() -> str:
    # fetch.py reads GROQ_API_KEY; the pipeline .env historically uses GROQ_KEY. Accept both.
    return os.environ.get("GROQ_API_KEY") or os.environ.get("GROQ_KEY") or ""


def _https(url: str) -> str:
    """Discord embeds prefer https; most CDNs serve both."""
    return "https://" + url[len("http://"):] if url.lower().startswith("http://") else url


def fetch_social_video(url: str) -> dict:
    """yt-dlp metadata (title, caption, cover frame) + Groq Whisper transcript of the
    audio. Returns {title, description, image, transcript, url} (any field may be
    empty), or {} only if yt-dlp itself failed. Transcript is '' when no GROQ_*KEY is
    set or ASR fails — the card then falls back to the caption."""
    try:
        with yt_dlp.YoutubeDL({
            "quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True, "socket_timeout": 30, "retries": 3,
        }) as ydl:
            info = ydl.extract_info(url, download=False) or {}
    except Exception as e:  # noqa: BLE001
        print(f"[social] yt-dlp failed for {url}: {str(e)[:160]}")
        return {}
    title = (info.get("title") or "").strip()
    desc = (info.get("description") or "").strip()
    image = _https(info.get("thumbnail") or "")
    transcript = ""
    key = _groq_key()
    # Transcribe when there's audio and it's not a livestream. ASR is best-effort —
    # if it fails or no key, the card still posts from title/caption.
    if key and info.get("duration") and not info.get("is_live"):
        try:
            from . import asr  # lazy: the groq package is optional
            model = os.environ.get("GROQ_WHISPER_MODEL") or asr.GROQ_DEFAULT_MODEL
            transcript = (asr.transcribe(url, key, model=model) or "").strip()
            if transcript:
                print(f"[social] transcribed {len(transcript)} chars of audio")
        except Exception as e:  # noqa: BLE001
            print(f"[social] ASR failed (non-fatal — card will use caption): {str(e)[:120]}")
    return {"title": title, "description": desc, "image": image,
            "transcript": transcript, "url": url}
