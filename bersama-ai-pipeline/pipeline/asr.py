"""Audio transcription fallback for videos WITHOUT captions.

When a video has no subtitle track (creator disabled captions), we download the
audio with yt-dlp and transcribe it with Groq Whisper — the "Whisper fallback
(v1.1)" the docs anticipated. Multilingual (whisper-large-v3 handles 99 langs),
so it works on non-English audio too; the summarizer then writes an English card.

Opt-in: only runs when GROQ_API_KEY is set (free key at https://console.groq.com).
Without it, caption-less videos keep skipping as before — no behaviour change.

Size: Groq caps a single audio file at 25 MB. We transcode to 16 kHz mono
~32 kbps mp3 (~0.24 MB/min), so a 60-min video (~14 MB) fits in one call — no
chunking needed.

Getting the audio at all is the hard part on a datacenter IP (YouTube 403s the
media bytes even when metadata works). `_download_audio` walks three rungs:
  1. yt-dlp across PLAYER_CLIENT_ORDERINGS            — free, works on residential IPs
  2. yt-dlp again via YTDLP_PROXY / YTDLP_COOKIES_FILE — only if the owner set one
  3. public Invidious/Piped mirrors (`ytaudio`)        — keyless, fetches on THEIR IP
Each rung logs why it failed; if all three fail the caller skips the video and
alerts staff exactly as before.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import yt_dlp

from . import ytaudio
from .fetch import PLAYER_CLIENT_ORDERINGS, ydl_network_opts, video_id_from_url

GROQ_DEFAULT_MODEL = "whisper-large-v3"

# Outcome of the most recent transcribe() call, so the caller can report a SPECIFIC
# skip reason instead of the generic "no captions". One of:
#   ok | no_key | no_groq_pkg | download_failed | transcode_failed | groq_error | empty
last_status: str = ""


def transcribe(
    url: str,
    api_key: str,
    *,
    model: str = GROQ_DEFAULT_MODEL,
) -> Optional[str]:
    """Download the video's audio and transcribe it via Groq Whisper.

    Returns the transcript text, or None on any failure (so the caller can
    fall through to a clean skip). Sets ``last_status`` so the caller can report
    a SPECIFIC skip reason (no key / IP-blocked download / Groq error / ...).
    """
    global last_status
    if not api_key:
        last_status = "no_key"
        return None
    try:
        from groq import Groq
    except ImportError:
        last_status = "no_groq_pkg"
        print("[asr] 'groq' package not installed — run: pip install groq")
        return None

    with tempfile.TemporaryDirectory() as td:
        raw = _download_audio(url, td)
        if not raw:
            last_status = "download_failed"
            return None
        mp3 = _to_small_mp3(raw, td)
        if not mp3:
            last_status = "transcode_failed"
            return None
        try:
            client = Groq(api_key=api_key)
            with open(mp3, "rb") as f:
                resp = client.audio.transcriptions.create(
                    model=model, file=f, response_format="text",
                )
            text = (resp or "").strip() or None
            last_status = "ok" if text else "empty"
            return text
        except Exception as e:  # noqa: BLE001 — never crash the run on an ASR error
            last_status = "groq_error"
            print(f"[asr] Groq transcription failed: {e}")
            return None


def _download_audio(url: str, td: str) -> Optional[Path]:
    """Download the best audio track into td; return its path.

    Three rungs (see the module docstring). YouTube bot-blocks datacenter IPs:
    the default ``web`` client's audio formats are PO-token-gated, so a bare
    ``bestaudio`` download 403s on the VM even though the hardened caption path
    (``fetch._rotate_extract``) succeeds. Rung 1 is the player_client rotation,
    rung 2 re-runs it through a proxy/cookies if configured, and rung 3 asks a
    public Invidious/Piped instance to fetch the media on its own IP.
    Returns None only when every rung failed.
    """
    # --- rung 1: yt-dlp direct (free; the only rung that works on a clean IP) ---
    path = _ytdlp_audio(url, td)
    if path:
        return path

    # --- rung 2: yt-dlp through a proxy / cookie jar, if the owner configured one ---
    net = ydl_network_opts()
    if net:
        via = "+".join(sorted(net))   # names only — never the proxy URL or cookie path
        print(f"[asr] retrying audio download via {via}")
        path = _ytdlp_audio(url, td, extra=net)
        if path:
            return path
        print(f"[asr] {via} audio download also failed")

    # --- rung 3: public mirrors fetch the bytes on their IP, not ours ---
    vid = video_id_from_url(url)
    print("[asr] trying public YouTube mirrors for audio")
    return ytaudio.download_via_mirror(vid, td)


def _ytdlp_audio(url: str, td: str, *, extra: Optional[dict] = None) -> Optional[Path]:
    """One full PLAYER_CLIENT_ORDERINGS sweep. `extra` merges in proxy/cookies."""
    tmpl = str(Path(td) / "audio.%(ext)s")
    last_err: Optional[Exception] = None
    for client in PLAYER_CLIENT_ORDERINGS:
        # Clear any partial audio.* left by a failed client so the glob below only
        # ever sees THIS attempt's output.
        for stale in Path(td).glob("audio.*"):
            try:
                stale.unlink()
            except OSError:
                pass
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": "bestaudio/best",
            "outtmpl": tmpl,
            "socket_timeout": 30,
            "retries": 3,
        }
        if client:
            opts["extractor_args"] = {"youtube": {"player_client": client}}
        opts.update(extra or {})
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[asr] player_client={client} audio download failed: {str(e)[:160]}")
            continue
        files = [p for p in Path(td).glob("audio.*") if p.is_file()]
        if files:
            return files[0]
        print(f"[asr] player_client={client} produced no audio file")
    if last_err:
        print(f"[asr] audio download failed on all player_clients (last: {str(last_err)[:160]})")
    return None


def _to_small_mp3(raw: Path, td: str) -> Optional[str]:
    """Transcode to 16 kHz mono ~32 kbps mp3 (~0.24 MB/min) to fit Groq's 25 MB limit."""
    out = str(Path(td) / "audio.mp3")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(raw), "-vn",
             "-ar", "16000", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "32k", out],
            check=True, capture_output=True, timeout=600,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[asr] ffmpeg transcode failed: {e}")
        return None
    return out
