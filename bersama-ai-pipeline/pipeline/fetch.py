"""Fetch video metadata + transcript.

Transcript strategy (corrected after validation — youtube-transcript-api gets
IP-banned on cloud/GH Actions IPs, so it must NOT be primary):

  1. yt-dlp info -> pick the best caption track URL (preferred language first,
     manual preferred over auto for the SAME language). Fetch + parse.  <- primary
  2. youtube-transcript-api.get_transcript(video_id)  <- fallback (works locally)
  3. None  -> caller skips the video (Whisper fallback is v1.1)

Returns (transcript_text, source_lang_hint) where source_lang_hint is "en"/"zh"/"other".
"""
from __future__ import annotations

import re
from typing import Optional

import requests
import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi

# Caption language preference, best first.
LANG_ORDER = ["en", "en-US", "en-Origin", "en-GB", "zh-Hans", "zh-CN", "zh", "zh-Hant"]
# Format preference within a language: json3 carries timing + clean text; vtt is the fallback.
FMT_ORDER = ["json3", "vtt", "srv1"]


class FetchError(Exception):
    pass


# ── metadata + playlist ──────────────────────────────────────────────────────

def _ydl_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,  # a single video URL must not expand to its whole playlist
        "extract_flat": False,
    }


def get_video_info(url: str) -> dict:
    """Return yt-dlp info dict for a single video (raises FetchError on failure)."""
    try:
        with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
            return ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise FetchError(f"yt-dlp could not extract {url}: {e}") from e


def list_playlist_entries(playlist_url: str) -> list[dict]:
    """Flat-list a playlist: returns [{id, url, title, duration}, ...] per video.

    Uses --flat-playlist so we don't fetch each video's full metadata here.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            res = ydl.extract_info(playlist_url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise FetchError(f"could not list playlist {playlist_url}: {e}") from e

    entries = []
    for e in (res or {}).get("entries", []) or []:
        if not e:
            continue
        vid = e.get("id")
        if not vid:
            continue
        entries.append({
            "id": vid,
            "url": e.get("url") or f"https://www.youtube.com/watch?v={vid}",
            "title": e.get("title") or "",
            "duration": e.get("duration") or 0,
        })
    return entries


# ── transcript ───────────────────────────────────────────────────────────────

def _lang_code(lang: str) -> str:
    """Normalize a caption language code to our hint bucket."""
    if not lang:
        return "other"
    low = lang.lower()
    if low.startswith("en"):
        return "en"
    if low.startswith("zh") or "hans" in low or "hant" in low:
        return "zh"
    return "other"


def _pick_track(info: dict):
    """Find the best (format_dict, lang_hint).

    Order: (1) any PREFERRED language across BOTH manual+auto stores (manual wins
    ties because it's listed first), checking json3/vtt/srv1 per language; then
    (2) last resort — any language in any store, still preferring json3 over vtt.

    This fixes the bug where a manual track in a non-preferred language (e.g. 'es')
    would beat a preferred auto track (e.g. 'en').
    """
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    stores = [s for s in (manual, auto) if s]

    # 1. Preferred language, anywhere, manual-first.
    for store in stores:
        for lang in LANG_ORDER:
            for t in store.get(lang, []):
                if (t.get("ext") or "").lower() in FMT_ORDER:
                    return t, _lang_code(lang)

    # 2. Last resort: any language, but still prefer json3 > vtt > srv1.
    for want_fmt in FMT_ORDER:
        for store in stores:
            for lang, tracks in store.items():
                for t in tracks:
                    if (t.get("ext") or "").lower() == want_fmt:
                        return t, _lang_code(lang)
    return None


def _parse_json3(data: dict) -> str:
    """json3 = {"events":[{"segs":[{"utf8":"hi "}]}, ...]}. Join all seg text."""
    parts = []
    for ev in data.get("events", []) or []:
        for seg in ev.get("segs", []) or []:
            t = seg.get("utf8")
            if t:
                parts.append(t)
    return _clean("".join(parts))


# A full WebVTT cue timestamp block contains " --> " between two timestamps.
_VTT_CUE_TS = re.compile(r"\d{1,2}:\d{2}(:\d{2})?\.\d{3}\s*-->\s*\d{1,2}:\d{2}(:\d{2})?\.\d{3}")
_BARE_NUMBER = re.compile(r"^\d+$")


def _parse_vtt(text: str) -> str:
    """Strip WebVTT header, cue timestamps, and bare cue indices — keep captions."""
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("WEBVTT") or line.startswith("NOTE"):
            continue
        if _VTT_CUE_TS.search(line):     # the timestamp line: "00:00:01.000 --> 00:00:04.000"
            continue
        if _BARE_NUMBER.match(line.strip()):  # a bare cue index (number alone on its line)
            continue
        clean = re.sub(r"<[^>]+>", "", line)  # strip <00:00:01.000> style inline tags
        out.append(clean)
    return _clean(" ".join(out))


def _clean(text: str) -> str:
    """Collapse whitespace; this is the transcript the LLM sees."""
    text = text.replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def get_transcript(info: dict) -> tuple[Optional[str], str]:
    """Return (transcript_text, source_lang_hint) or (None, hint) if unavailable.

    Primary: yt-dlp caption track URL -> fetch + parse.
    Fallback: youtube-transcript-api (works locally, often blocked on cloud IPs).

    NOTE: yt-dlp caption URLs are signed and expire (minutes–hours). We fetch the
    caption immediately after extract_info (sub-second gap), so staleness is a
    non-issue today — but if you ever insert a slow step between the two, fetch
    will 403. Keep them adjacent.
    """
    # --- primary: yt-dlp caption URL ---
    try:
        picked = _pick_track(info)
        if picked:
            fmt, lang_hint = picked
            url = fmt.get("url")
            ext = (fmt.get("ext") or "").lower()
            if url:
                r = requests.get(url, timeout=30)
                r.raise_for_status()
                if ext == "json3":
                    return _parse_json3(r.json()), lang_hint
                if ext == "vtt":
                    return _parse_vtt(r.text), lang_hint
                # srv1 etc.: try json parse, else treat as text
                try:
                    return _parse_json3(r.json()), lang_hint
                except ValueError:
                    return _clean(r.text), lang_hint
    except Exception as e:  # noqa: BLE001 — caption fetch is best-effort
        print(f"[fetch] yt-dlp caption path failed: {e}; trying fallback")

    # --- fallback: youtube-transcript-api (by video id) ---
    # Broad except: we don't import youtube_transcript_api's private error classes
    # (they moved between minor versions); any failure here means "no transcript
    # via this path" and we fall through to a skip. Includes RequestBlocked (cloud IPs).
    vid = info.get("id")
    if vid:
        try:
            chunks = YouTubeTranscriptApi.get_transcript(
                vid, languages=["en", "en-US", "zh-Hans", "zh-CN", "zh"]
            )
            text = _clean(" ".join(c.get("text", "") for c in chunks))
            if text:
                lang_hint = "zh" if re.search(r"[一-鿿]", text) else "en"
                return text, lang_hint
        except Exception as e:  # noqa: BLE001
            print(f"[fetch] youtube-transcript-api fallback unavailable: {e}")

    return None, "other"
