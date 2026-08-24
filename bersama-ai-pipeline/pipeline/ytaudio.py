"""Keyless audio-source fallbacks for caption-less videos on a blocked IP.

WHY THIS EXISTS
YouTube bot-blocks datacenter IPs for *media* downloads (yt-dlp #12475/#15865/
#16229). On the pipeline VM, metadata extraction still works — the title comes
through fine — but every `player_client` gets HTTP 403 on the actual audio
bytes, so `asr._download_audio` returns nothing and every caption-less video
skips with `nocaption_asr_blocked`.

The rungs, cheapest first (asr.py walks them in order):
  1. yt-dlp `player_client` rotation                 — asr.py, unchanged
  2. yt-dlp again through YTDLP_PROXY / YTDLP_COOKIES_FILE, if the owner set
     either — the paid-but-reliable escape hatch (a residential proxy is the
     only thing that reliably beats an IP block)
  3. THIS MODULE: public Invidious / Piped instances, which fetch the media on
     THEIR IP and stream it back to us. Keyless, cookieless, free — the same
     doctrine as the Bluesky mirror for the stock digest and fxtwitter for
     Serenity. Public instances come and go, so the list is env-overridable.

Everything here is best-effort: any failure returns None and the caller falls
back to today's clean skip + staff alert. Nothing regresses if every mirror is
down; the worst case is exactly the current behaviour.

A hardcoded host list rots fast (measured 2026-08-24: all six seeds 403/401'd or
had moved), so unless YT_AUDIO_MIRRORS pins specific hosts we ALSO pull the
public instance directories at runtime and try those — more hosts means a better
chance one of them isn't blocked from here.

Config:
  YT_AUDIO_MIRRORS  comma-separated, overrides the list entirely AND disables
                    discovery, e.g.
                    "piped:https://pipedapi.example,invidious:https://inv.example"
                    The "piped:"/"invidious:" prefix is optional — a host
                    containing "piped" is treated as Piped, otherwise Invidious.
  YT_AUDIO_DISCOVER         "0" to skip the instance-directory lookup
  YT_AUDIO_MAX_HOSTS        how many hosts to try per run (default 14)
  YT_AUDIO_MIRROR_TIMEOUT   per-request seconds (default 20)
  YT_AUDIO_MAX_MB           hard cap on downloaded bytes (default 220)
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

import requests

# Seed list. Public instances churn constantly — when these rot, set
# YT_AUDIO_MIRRORS rather than editing code (see check_audio_sources.py, which
# tells you which hosts are alive from THIS machine right now).
DEFAULT_MIRRORS = [
    "piped:https://pipedapi.kavin.rocks",
    "piped:https://pipedapi.adminforge.de",
    "piped:https://api.piped.private.coffee",
    "invidious:https://inv.nadeko.net",
    "invidious:https://invidious.nerdvpn.de",
    "invidious:https://yewtu.be",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Outcome of the last download_via_mirror() call, for the skip-reason plumbing:
#   ok | no_mirrors | all_failed
last_status: str = ""


def _timeout() -> float:
    try:
        return float(os.environ.get("YT_AUDIO_MIRROR_TIMEOUT") or 20)
    except ValueError:
        return 20.0


def _max_bytes() -> int:
    try:
        return int(float(os.environ.get("YT_AUDIO_MAX_MB") or 220) * 1024 * 1024)
    except ValueError:
        return 220 * 1024 * 1024


def _parse_entries(entries: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for e in entries:
        e = e.strip()
        kind, sep, url = e.partition(":")
        if sep and kind in ("piped", "invidious"):
            base = url.strip()
        else:                       # no explicit prefix — infer from the host
            base, kind = e, ("piped" if "piped" in e.lower() else "invidious")
        base = base.strip().rstrip("/")
        if base.startswith("http"):
            out.append((kind, base))
    return out


def _max_hosts() -> int:
    try:
        return max(1, int(os.environ.get("YT_AUDIO_MAX_HOSTS") or 14))
    except ValueError:
        return 14


def mirrors() -> list[tuple[str, str]]:
    """[(kind, base_url)] to try, in order.

    YT_AUDIO_MIRRORS pins the list exactly (and skips discovery) — that is the
    lever to use once `check_audio_sources.py` has told you what works from this
    machine. Otherwise: seeds first (fast, no extra request), then whatever the
    public instance directories list, deduped and capped.
    """
    raw = (os.environ.get("YT_AUDIO_MIRRORS") or "").strip()
    if raw:
        return _parse_entries(raw.split(","))

    out = _parse_entries(DEFAULT_MIRRORS)
    if (os.environ.get("YT_AUDIO_DISCOVER") or "").strip() != "0":
        seen = {u for _, u in out}
        for kind, base in discover_mirrors():
            if base not in seen:
                seen.add(base)
                out.append((kind, base))
    return out[:_max_hosts()]


# ── instance discovery ───────────────────────────────────────────────────────
# Both directories are themselves third-party and may be down or gone; each is
# wrapped so a failure just means "no extra hosts", never a broken run.

PIPED_DIRECTORIES = [
    "https://piped-instances.kavin.rocks/",
    "https://raw.githubusercontent.com/TeamPiped/documentation/main/content/docs/public-instances/index.md",
]
INVIDIOUS_DIRECTORY = "https://api.invidious.io/instances.json?sort_by=type,users"


def discover_mirrors() -> list[tuple[str, str]]:
    """Live instance lists from the public directories. [] on any failure."""
    found: list[tuple[str, str]] = []
    try:
        found += _discover_piped()
    except Exception as e:  # noqa: BLE001
        print(f"[ytaudio] piped directory unavailable ({str(e)[:80]})")
    try:
        found += _discover_invidious()
    except Exception as e:  # noqa: BLE001
        print(f"[ytaudio] invidious directory unavailable ({str(e)[:80]})")
    if found:
        print(f"[ytaudio] discovered {len(found)} instance(s) from the public directories")
    return found


def _discover_piped() -> list[tuple[str, str]]:
    for url in PIPED_DIRECTORIES:
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=_timeout())
            r.raise_for_status()
        except Exception:  # noqa: BLE001 — try the next directory
            continue
        if url.endswith(".md"):
            # The docs table lists api urls in a markdown column.
            hits = re.findall(r"https://[\w.-]*piped[\w.-]*\.[a-z]{2,}", r.text)
            return [("piped", u.rstrip("/")) for u in dict.fromkeys(hits)]
        try:
            data = r.json()
        except ValueError:
            continue
        out = []
        for inst in data if isinstance(data, list) else []:
            api = str((inst or {}).get("api_url") or "").strip().rstrip("/")
            if api.startswith("http"):
                out.append(("piped", api))
        if out:
            return out
    return []


def _discover_invidious() -> list[tuple[str, str]]:
    r = requests.get(INVIDIOUS_DIRECTORY, headers={"User-Agent": UA}, timeout=_timeout())
    r.raise_for_status()
    out = []
    for row in r.json():
        # rows look like ["host.tld", {"uri": "...", "type": "https", "api": true}]
        if not (isinstance(row, list) and len(row) == 2 and isinstance(row[1], dict)):
            continue
        info = row[1]
        if info.get("type") != "https" or info.get("api") is False:
            continue
        uri = str(info.get("uri") or "").strip().rstrip("/")
        if uri.startswith("https://"):
            out.append(("invidious", uri))
    return out


def download_via_mirror(video_id: str, td: str) -> Optional[Path]:
    """Try each mirror until one yields an audio file in `td`. None if all fail."""
    global last_status
    if not video_id:
        last_status = "no_mirrors"
        return None
    hosts = mirrors()
    if not hosts:
        last_status = "no_mirrors"
        print("[ytaudio] no mirrors configured (YT_AUDIO_MIRRORS is empty)")
        return None
    for kind, base in hosts:
        try:
            stream = (_piped_stream(base, video_id) if kind == "piped"
                      else _invidious_stream(base, video_id))
        except Exception as e:  # noqa: BLE001 — a dead instance must not kill the run
            print(f"[ytaudio] {kind} {base}: lookup failed ({str(e)[:120]})")
            continue
        if not stream:
            print(f"[ytaudio] {kind} {base}: no audio stream offered")
            continue
        url, ext = stream
        path = _stream_to_file(url, td, ext)
        if path:
            print(f"[ytaudio] got audio via {kind} {base} ({path.stat().st_size // 1024} KB)")
            last_status = "ok"
            return path
    last_status = "all_failed"
    print(f"[ytaudio] every mirror failed ({len(hosts)} tried)")
    return None


def _get_json(url: str) -> dict:
    r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json"},
                     timeout=_timeout())
    r.raise_for_status()
    return r.json()


def _piped_stream(base: str, video_id: str) -> Optional[tuple[str, str]]:
    """Piped proxies media through the instance already, so its URL is usable
    as-is. Pick the LOWEST-bitrate audio track: we downmix to 16 kHz mono 32 kbps
    for Whisper anyway, so a bigger file buys nothing but transfer time."""
    data = _get_json(f"{base}/streams/{video_id}")
    best: Optional[tuple[int, str, str]] = None
    for s in data.get("audioStreams") or []:
        url = str(s.get("url") or "")
        if not url:
            continue
        bitrate = _as_int(s.get("bitrate"))
        ext = str(s.get("format") or "").lower()
        ext = "webm" if "webm" in ext or "opus" in str(s.get("codec", "")).lower() else "m4a"
        if bitrate <= 0:
            bitrate = 10 ** 6      # unknown bitrate sorts last
        if best is None or bitrate < best[0]:
            best = (bitrate, url, ext)
    return (best[1], best[2]) if best else None


def _invidious_stream(base: str, video_id: str) -> Optional[tuple[str, str]]:
    """Invidious hands back googlevideo URLs that are bound to ITS IP, so we must
    request the instance's own proxy endpoint (`local=true`) instead."""
    data = _get_json(f"{base}/api/v1/videos/{video_id}")
    best: Optional[tuple[int, str, str]] = None
    for f in data.get("adaptiveFormats") or []:
        mime = str(f.get("type") or "")
        if not mime.startswith("audio/"):
            continue
        itag = str(f.get("itag") or "").strip()
        if not itag:
            continue
        bitrate = _as_int(f.get("bitrate")) or 10 ** 6
        ext = "webm" if "webm" in mime else "m4a"
        if best is None or bitrate < best[0]:
            best = (bitrate, itag, ext)
    if not best:
        return None
    return (f"{base}/latest_version?id={video_id}&itag={best[1]}&local=true", best[2])


def _stream_to_file(url: str, td: str, ext: str) -> Optional[Path]:
    """Stream the media to disk with a hard size cap (a mirror serving an HTML
    error page or an endless stream must not fill the VM's disk)."""
    out = Path(td) / f"mirror_audio.{ext}"
    cap = _max_bytes()
    written = 0
    try:
        with requests.get(url, headers={"User-Agent": UA}, stream=True,
                          timeout=_timeout()) as r:
            r.raise_for_status()
            ctype = (r.headers.get("Content-Type") or "").lower()
            if ctype.startswith("text/") or "html" in ctype:
                print(f"[ytaudio] refused non-media response ({ctype[:40]})")
                return None
            with open(out, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > cap:
                        print(f"[ytaudio] aborted: exceeded {cap // (1024 * 1024)} MB cap")
                        return None
                    fh.write(chunk)
    except Exception as e:  # noqa: BLE001
        print(f"[ytaudio] download failed ({str(e)[:120]})")
        return None
    # A few KB is an error page or a truncated stream, not audio.
    if written < 32 * 1024:
        print(f"[ytaudio] discarded tiny file ({written} bytes)")
        return None
    return out


def _as_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0
