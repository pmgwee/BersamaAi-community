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
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import yt_dlp

GROQ_DEFAULT_MODEL = "whisper-large-v3"


def transcribe(
    url: str,
    api_key: str,
    *,
    model: str = GROQ_DEFAULT_MODEL,
) -> Optional[str]:
    """Download the video's audio and transcribe it via Groq Whisper.

    Returns the transcript text, or None on any failure (so the caller can
    fall through to a clean skip).
    """
    if not api_key:
        return None
    try:
        from groq import Groq
    except ImportError:
        print("[asr] 'groq' package not installed — run: pip install groq")
        return None

    with tempfile.TemporaryDirectory() as td:
        raw = _download_audio(url, td)
        if not raw:
            return None
        mp3 = _to_small_mp3(raw, td)
        if not mp3:
            return None
        try:
            client = Groq(api_key=api_key)
            with open(mp3, "rb") as f:
                resp = client.audio.transcriptions.create(
                    model=model, file=f, response_format="text",
                )
            return (resp or "").strip() or None
        except Exception as e:  # noqa: BLE001 — never crash the run on an ASR error
            print(f"[asr] Groq transcription failed: {e}")
            return None


def _download_audio(url: str, td: str) -> Optional[Path]:
    """Download the best audio track into td; return its path."""
    tmpl = str(Path(td) / "audio.%(ext)s")
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": tmpl,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:  # noqa: BLE001
        print(f"[asr] audio download failed: {e}")
        return None
    files = [p for p in Path(td).glob("audio.*") if p.is_file()]
    if not files:
        print("[asr] no audio file produced")
        return None
    return files[0]


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
