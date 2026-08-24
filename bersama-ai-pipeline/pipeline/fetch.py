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

# Realistic browser UA for the uploads-RSS fetch. YouTube throttles the default
# python-requests UA on datacenter IPs (the GCP VM got HTTP 500 here while a
# residential IP got 200 for the same URL + channel_id); a browser UA gives the
# dated recency feed a real chance to work. See _rss_recent_uploads.
_RSS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

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


# Outcome reason for the most recent get_transcript() call, so a caption-less skip
# can carry a SPECIFIC cause (no GROQ key / ASR IP-blocked / Groq error / ...) to the
# alert instead of the generic "check captions + GROQ_API_KEY". "" on success or when
# not yet run; see get_transcript's terminal block for the code set.
last_transcript_status: str = ""


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
    opts.update(ydl_network_opts())   # no-op unless YTDLP_PROXY / YTDLP_COOKIES_FILE is set
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


# Public alias — asr.py needs the id to ask a mirror for the same video.
video_id_from_url = _video_id_from_url


def ydl_network_opts() -> dict:
    """Optional yt-dlp network overrides from the environment, or {} when unset.

    A residential proxy is the only thing that reliably beats YouTube's
    datacenter-IP block, and a cookie jar helps on some age/region-gated videos.
    Both are the owner's call (they cost money / carry account risk), so the
    pipeline never assumes them — it just uses them when present. Callers log
    the KEY NAMES only: a proxy URL usually embeds credentials.
      YTDLP_PROXY         e.g. http://user:pass@host:port  (also socks5://)
      YTDLP_COOKIES_FILE  path to a Netscape cookies.txt on the VM
    """
    opts: dict = {}
    proxy = (os.environ.get("YTDLP_PROXY") or "").strip()
    if proxy:
        opts["proxy"] = proxy
    cookies = (os.environ.get("YTDLP_COOKIES_FILE") or "").strip()
    if cookies and os.path.exists(cookies):
        opts["cookiefile"] = cookies
    elif cookies:
        print("[fetch] YTDLP_COOKIES_FILE is set but the file does not exist — ignoring")
    return opts


# YouTube channel sub-pages that already name an explicit tab. If a channel URL
# ends with one of these, yt-dlp already lists the right thing — leave it alone.
_CHANNEL_TABS = (
    "/videos", "/shorts", "/streams", "/live", "/playlists", "/featured",
    "/community", "/about", "/channels", "/posts", "/store",
)


def _looks_like_channel(url: str) -> bool:
    """True if ``url`` points at a YouTube channel — a ``/@handle``,
    ``/channel/UC...``, ``/c/name`` or ``/user/name`` path — regardless of any
    trailing ``/videos`` (or other) tab. Used to decide whether to apply the
    channel listing path (RSS uploads) vs. a real playlist's flat listing.
    """
    from urllib.parse import urlsplit
    path = urlsplit(url.strip()).path
    return (path.startswith("/@") or path.startswith("/channel/")
            or path.startswith("/c/") or path.startswith("/user/"))


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
    if not _looks_like_channel(url):
        return url
    return urlunsplit((p.scheme, p.netloc, path + "/videos", p.query, p.fragment))


def _rss_recent_uploads(channel_id: str, days: int):
    """A channel's recent uploads via the YouTube RSS feed, newest-first.

    The uploads feed (``/feeds/videos.xml?channel_id=UC...``) carries real
    ``<published>`` dates — unlike yt-dlp's flat ``/videos`` listing, which has
    *no* date field at all — and it's not bot-blocked like the player, so it's
    the scalable source for creator-watch: each channel contributes only its
    last ``days`` of uploads (typically 0–1), keeping per-run volume bounded by
    "new uploads this window" rather than by catalog size. That means adding
    more creators to playlists.txt can never let one channel monopolize the run
    or push stale content through as if it were new.

    Returns a list of ``{id, url, title, duration:0, upload_date:'YYYYMMDD'}``
    (possibly empty) on success, or ``None`` if the feed couldn't be
    fetched/parsed — the caller then falls back to the undated flat listing.
    ``duration`` is 0 here because RSS omits it; ``process_video`` refetches
    full metadata per video anyway.
    """
    import xml.etree.ElementTree as ET
    from datetime import datetime, timezone, timedelta

    if not channel_id:
        return None
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    # Browser UA + retries: YouTube sometimes serves a transient 500 to datacenter
    # IPs (GCP VM observed; same URL/ID returns 200 from residential). A realistic
    # UA + a couple of retries recovers the dated recency feed instead of silently
    # falling back to the undated flat listing — which would leave the 3-day "only
    # new uploads" filter inert and let one channel's backlog monopolize the run.
    # (run_scheduled's per-channel cap is the backstop if this still fails.)
    root = None
    last_status: Optional[int] = None
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": _RSS_UA})
            last_status = r.status_code
            if r.status_code == 200:
                root = ET.fromstring(r.text)
                break
        except (requests.RequestException, ET.ParseError) as e:  # noqa: BLE001
            print(f"[fetch] uploads RSS for {channel_id} attempt {attempt + 1} error: {e}")
        if attempt < 2:
            time.sleep(2 * (attempt + 1))   # 2s, then 4s backoff
    if root is None:
        print(f"[fetch] uploads RSS for {channel_id} returned {last_status} after retries; "
              f"falling back to flat listing (recency inert this run)")
        return None

    ns = {"atom": "http://www.w3.org/2005/Atom",
          "yt": "http://www.youtube.com/xml/schemas/2015"}
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for e in root.findall("atom:entry", ns):
        pub = (e.find("atom:published", ns).text or "").strip()
        try:
            dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt < cutoff:
            continue
        vid = (e.find("yt:videoId", ns).text or "").strip()
        if not vid:
            continue
        out.append({
            "id": vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "title": (e.find("atom:title", ns).text or "").strip(),
            "duration": 0,
            "upload_date": dt.strftime("%Y%m%d"),
        })
    return out


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


def list_playlist_entries(playlist_url: str, *, recent_days: Optional[int] = None) -> list[dict]:
    """Flat-list a playlist/channel: returns [{id, url, title, duration, upload_date}, ...].

    Uses --flat-playlist so we don't fetch each video's full metadata here.
    Bare channel URLs are first normalized to their /videos tab (see
    _normalize_channel_url) — otherwise yt-dlp returns the channel's tabs, not
    its uploads.

    Scalability: when ``recent_days`` is set and the URL is a channel, the dated
    uploads RSS feed is preferred over the flat listing. yt-dlp's flat
    ``/videos`` extract carries no upload date, which would leave the
    creator-watch recency filter inert and let a channel's full backlog flood
    the run. The RSS feed returns only the last ``recent_days`` of uploads with
    real publish dates, so adding more creators can't let one channel
    monopolize the daily quota.

    Shorts: the uploads RSS includes Shorts alongside long-form videos. We keep
    only entries that also appear in the ``/videos`` listing (long-form only —
    Shorts live in a separate ``/shorts`` tab), so 45-second clips are never
    summarized. Falls back to the undated flat listing if the feed is unreachable.
    """
    playlist_url = _normalize_channel_url(playlist_url)
    try:
        res = _rotate_extract(playlist_url, playlist=True)
    except FetchError as e:
        raise FetchError(f"could not list playlist {playlist_url}: {e}") from e

    # Build the flat /videos listing up front — it's both the fallback and the
    # long-form ID set used to strip Shorts from the dated RSS candidates.
    flat_entries = []
    flat_ids: set[str] = set()
    for e in (res or {}).get("entries", []) or []:
        if not e:
            continue
        vid = e.get("id")
        if not vid:
            continue
        flat_entries.append({
            "id": vid,
            "url": e.get("url") or f"https://www.youtube.com/watch?v={vid}",
            "title": e.get("title") or "",
            "duration": e.get("duration") or 0,
            "upload_date": e.get("upload_date") or "",
        })
        flat_ids.add(vid)

    # Channel + recency requested: prefer the dated uploads RSS feed. ``res``
    # from the /videos extract carries the channel_id we need (verified
    # 2026-07-25); None from _rss_recent_uploads means "feed unavailable" →
    # fall through to the undated flat listing (best-effort, never a hard fail).
    if recent_days and _looks_like_channel(playlist_url):
        dated = _rss_recent_uploads(res.get("channel_id") or "", recent_days)
        if dated is not None:
            if flat_ids:
                # Strip Shorts: keep only RSS uploads that are also in /videos.
                dated = [d for d in dated if d["id"] in flat_ids]
            return dated

    return flat_entries


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

    On failure, sets module-global ``last_transcript_status`` to a specific reason
    so the caller's skip alert can name the actual lever (no GROQ key vs. ASR
    IP-blocked vs. Groq error) instead of a generic "check captions + key".
    """
    global last_transcript_status
    last_transcript_status = ""   # reset; only set to a specific reason on failure
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
    asr_ran = False
    if groq_key and not info.get("_oembed_only"):
        url = info.get("webpage_url") or info.get("url")
        if url:
            print("[fetch] no captions available — transcribing audio via Groq Whisper")
            from . import asr  # lazy import; the groq package is optional
            model = os.environ.get("GROQ_WHISPER_MODEL") or asr.GROQ_DEFAULT_MODEL
            text = asr.transcribe(url, groq_key, model=model)
            asr_ran = True
            if text:
                text = _clean(text)
                if text:
                    lang_hint = "zh" if re.search(r"[一-鿿]", text) else "en"
                    return text, lang_hint

    # No transcript from any tier — record a SPECIFIC reason so the skip alert can
    # tell the owner the real lever (set GROQ_API_KEY? IP-blocked? Groq quota?)
    # instead of the opaque "check captions + GROQ_API_KEY".
    if not groq_key:
        last_transcript_status = "nocaption_nokey"
    elif info.get("_oembed_only"):
        last_transcript_status = "nocaption_oembed"
    elif asr_ran:
        last_transcript_status = {
            "download_failed": "nocaption_asr_blocked",
            "transcode_failed": "nocaption_transcode_failed",
            "groq_error": "nocaption_asr_error",
            "empty": "nocaption_asr_empty",
            # ASR returned text but _clean() emptied it (whitespace-only garbage):
            "ok": "nocaption_asr_empty",
            "no_groq_pkg": "nocaption_no_groq_pkg",
        }.get(asr.last_status, "nocaption_misc")
    else:
        last_transcript_status = "nocaption_misc"
    return None, "other"
