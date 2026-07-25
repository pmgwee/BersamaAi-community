"""Fetch video metadata + transcript.

HARDENED FOR GITHUB ACTIONS (mid-2026): YouTube bot-blocks Microsoft Azure
datacenter IPs (yt-dlp #12475, #15865, #16229). There is NO fully-reliable
code-only fix — even cookies are now IP-bound (#16229) and PO-token providers
need a Node/Deno server and don't unblock datacenter IPs anyway (#11053). The
strategy below is the best code-only shot: try hard, degrade gracefully.

Transcript strategy:
  1. yt-dlp info across several `player_client` orderings with skip_download=True
     (we only want caption-track URLs, never the heavily-gated format URLs).
     YouTube rotates which clients are LOGIN_REQUIRED ~weekly (#15751), so we try
     a handful and take the first that survives.  <- primary
  2. youtube-transcript-api .fetch(video_id)  <- fallback (works locally; usually
     RequestBlocked on cloud IPs, but cheap to try)
  3. None  -> caller skips the video (Whisper fallback is v1.1)

Metadata safety net: if EVERY player_client returns LOGIN_REQUIRED, fall back to
YouTube oEmbed (no key; the one call reliably reachable from datacenter IPs) so
the pipeline logs a clean, titled skip instead of a hard fetch error.

Returns (transcript_text, source_lang_hint) where source_lang_hint is "en"/"zh"/"other".
"""
from __future__ import annotations

import os
import re
import time
from typing import Optional

import requests
import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi

# Caption language preference, best first.
LANG_ORDER = ["en", "en-US", "en-Origin", "en-GB", "zh-Hans", "zh-CN", "zh", "zh-Hant"]
# Format preference within a language: json3 carries timing + clean text; vtt is the fallback.
FMT_ORDER = ["json3", "vtt", "srv1"]

# Mid-2026 best-hope YouTube player_client orderings. YouTube rotates which
# clients are LOGIN_REQUIRED roughly weekly (yt-dlp #15751, #15865), so we try
# several and take the first that returns real metadata. None is guaranteed.
PLAYER_CLIENT_ORDERINGS = [
    None,                                              # yt-dlp's built-in default client set (best on residential IPs)
    ["default", "-web"],                               # drop the PO-token-gated web client (most-cited CI fix)
    ["ios", "mediaconnect", "web_safari"],
    ["default", "-tv", "web_safari", "web_embedded"],  # r/youtubedl "some tv client https formats" fix
    ["android_vr", "web_safari"],
    ["mediaconnect"],                                  # sparse 2026 reports; may need nightly yt-dlp
]


class FetchError(Exception):
    pass


# ── metadata + playlist ──────────────────────────────────────────────────────

def _ydl_opts(*, player_client=None, playlist: bool = False) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,        # NEVER request format/streaming URLs (most PO-token-gated)
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["all"],
        "socket_timeout": 30,
        "retries": 5,
    }
    if playlist:
        opts["extract_flat"] = "in_playlist"
    else:
        opts["noplaylist"] = True
        opts["extract_flat"] = False
    if player_client:
        opts["extractor_args"] = {"youtube": {"player_client": player_client}}
    return opts


def _rotate_extract(url: str, *, playlist: bool = False) -> dict:
    """yt-dlp extract_info across PLAYER_CLIENT_ORDERINGS.

    Best code-only shot at evading YouTube's datacenter-IP bot-block. Returns
    the first info dict that has a real title (video) or an entries list
    (playlist), or raises FetchError if every client failed.
    """
    last_err: Optional[Exception] = None
    for i, client in enumerate(PLAYER_CLIENT_ORDERINGS):
        info = None
        try:
            with yt_dlp.YoutubeDL(_ydl_opts(player_client=client, playlist=playlist)) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as e:
            last_err = e
            print(f"[fetch] player_client={client} failed: {str(e)[:160]}")
        # usable = real title (video) or an entries list (playlist); a LOGIN_REQUIRED
        # block makes yt-dlp fall back to initial_data with no title -> keep trying.
        usable = bool(info) and (("entries" in info) if playlist else info.get("title"))
        if usable:
            return info
        if i < len(PLAYER_CLIENT_ORDERINGS) - 1:
            time.sleep(min(2 ** i, 8))   # short backoff; helps when the block is transient
    raise FetchError(
        f"yt-dlp could not extract {url} on any player_client "
        f"(YouTube likely bot-blocked this datacenter IP; last error: {last_err})"
    )


def _video_id_from_url(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{6,})", url or "")
    return m.group(1) if m else ""


# YouTube channel sub-pages that already name an explicit tab. If a channel URL
# ends with one of these, yt-dlp already lists the right thing — leave it alone.
_CHANNEL_TABS = (
    "/videos", "/shorts", "/streams", "/live", "/playlists", "/featured",
    "/community", "/about", "/channels", "/posts", "/store",
)


def _normalize_channel_url(url: str) -> str:
    """Append ``/videos`` to a bare YouTube channel URL so yt-dlp lists real uploads.

    Why this exists: a bare channel URL — ``/@handle``, ``/channel/UC...``,
    ``/c/name`` or ``/user/name`` WITHOUT a tab — makes yt-dlp's flat extractor
    return the channel's *tabs* (Videos / Live / Shorts) as sub-playlists, each
    keyed by the channel's own ``UC...`` id with ``url=None`` and no per-video
    metadata. ``list_playlist_entries`` then turns those into bogus
    ``watch?v=UC...`` entries that always FETCH_FAILED — so the creator-watch
    scan never saw a single real upload. Pointing yt-dlp at the ``/videos`` tab
    lists actual videos, newest-first (1059 for 零度解说, verified 2026-07-25).

    No-op for: URLs that already name a tab, single videos (``/watch``,
    ``youtu.be/``, ``/embed/``, ``/shorts/<id>``, ``/live/<id>``, ``/clip/``),
    playlists (``/playlist``), and non-YouTube hosts.
    """
    from urllib.parse import urlsplit, urlunsplit

    p = urlsplit(url.strip())
    if (p.hostname or "").lower() not in (
        "www.youtube.com", "youtube.com", "m.youtube.com", "music.youtube.com",
    ):
        return url
    path = p.path.rstrip("/") or "/"

    # Single video / clip URLs and playlist URLs are not channels — leave alone.
    if (path == "/watch"
            or path.startswith(("/embed/", "/clip/", "/live/", "/shorts/"))
            or "/playlist" in path):
        return url

    # Already names a tab explicitly — leave alone.
    if any(path.endswith(t) for t in _CHANNEL_TABS):
        return url

    # Only rewrite shapes we recognize as bare channel URLs.
    looks_channel = (path.startswith("/@")
                     or path.startswith("/channel/")
                     or path.startswith("/c/")
                     or path.startswith("/user/"))
    if not looks_channel:
        return url
    return urlunsplit((p.scheme, p.netloc, path + "/videos", p.query, p.fragment))


def _oembed_lookup(url: str) -> dict:
    """YouTube oEmbed (no API key; reliably reachable from datacenter IPs).

    Used as a metadata safety net when yt-dlp is fully blocked — gives
    title/author so the pipeline can log a clean, titled skip. Returns {} on any failure.
    """
    try:
        r = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json() or {}
    except requests.RequestException as e:  # noqa: BLE001
        print(f"[fetch] oEmbed lookup failed: {e}")
    return {}


def _oembed_stub(url: str) -> dict:
    """Minimal info dict from oEmbed when yt-dlp is fully blocked.

    Shaped so downstream (get_transcript, _pick_track, the skip logger) won't
    crash: empty caption stores => _pick_track returns None => the run routes to
    SKIPPED_NOCAPTION with the real title instead of FETCH_FAILED.
    """
    data = _oembed_lookup(url)
    if not data:
        return {}
    author = data.get("author_name") or ""
    return {
        "id": _video_id_from_url(url),
        "title": data.get("title") or "(unknown)",
        "uploader": author,
        "channel": author,
        "webpage_url": url,
        "url": url,
        "duration": None,
        "subtitles": {},
        "automatic_captions": {},
        "_oembed_only": True,
    }


def get_video_info(url: str) -> dict:
    """Return yt-dlp info dict for a single video (raises FetchError on failure).

    Tries several player_client orderings to evade YouTube's datacenter-IP
    bot-block; if all fail, falls back to an oEmbed-only stub so downstream can
    still log a titled skip instead of a hard fetch error.
    """
    try:
        return _rotate_extract(url)
    except FetchError:
        stub = _oembed_stub(url)
        if stub:
            print(f"[fetch] yt-dlp fully blocked on all clients; using oEmbed-only metadata for {url}")
            return stub
        raise


def list_playlist_entries(playlist_url: str) -> list[dict]:
    """Flat-list a playlist: returns [{id, url, title, duration}, ...] per video.

    Uses --flat-playlist so we don't fetch each video's full metadata here.
    Bare channel URLs are first normalized to their /videos tab (see
    _normalize_channel_url) — otherwise yt-dlp returns the channel's tabs, not
    its uploads.
    """
    playlist_url = _normalize_channel_url(playlist_url)
    try:
        res = _rotate_extract(playlist_url, playlist=True)
    except FetchError as e:
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
            "upload_date": e.get("upload_date") or "",
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
    Fallback: youtube-transcript-api instance .fetch() (works locally; on cloud IPs
    usually raises RequestBlocked — broad-except logs it and we fall through).

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

    # --- fallback: youtube-transcript-api (instance API, >= 1.0) ---
    # Broad except: on older library versions .fetch doesn't exist (AttributeError);
    # on cloud IPs it raises RequestBlocked/IpBlocked. Either way, fall through.
    vid = info.get("id")
    if vid and not info.get("_oembed_only"):
        try:
            fetched = YouTubeTranscriptApi().fetch(
                vid, languages=["en", "en-US", "zh-Hans", "zh-CN", "zh"]
            )
            text = _clean(" ".join(snippet.text for snippet in fetched))
            if text:
                lang_hint = "zh" if re.search(r"[一-鿿]", text) else "en"
                return text, lang_hint
        except Exception as e:  # noqa: BLE001
            print(f"[fetch] youtube-transcript-api fallback unavailable: {e}")

    # --- tier 3: ASR fallback for caption-less videos (Groq Whisper) ---
    # Opt-in via GROQ_API_KEY; multilingual, so non-English audio is transcribed
    # and the summarizer then writes an English card. Skipped for the oEmbed-only
    # stub (no audio path there, and yt-dlp would be blocked anyway).
    groq_key = os.environ.get("GROQ_API_KEY") or os.environ.get("GROQ_KEY") or ""
    if groq_key and not info.get("_oembed_only"):
        url = info.get("webpage_url") or info.get("url")
        if url:
            print("[fetch] no captions available — transcribing audio via Groq Whisper")
            from . import asr  # lazy import; the groq package is optional
            model = os.environ.get("GROQ_WHISPER_MODEL") or asr.GROQ_DEFAULT_MODEL
            text = asr.transcribe(url, groq_key, model=model)
            if text:
                text = _clean(text)
                if text:
                    lang_hint = "zh" if re.search(r"[一-鿿]", text) else "en"
                    return text, lang_hint

    return None, "other"
