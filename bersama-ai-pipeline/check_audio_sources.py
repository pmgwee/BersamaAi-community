"""Which audio source works from THIS machine, right now?

Run on the pipeline VM when caption-less videos start skipping with
`nocaption_asr_blocked`:

    cd ~/bersama/bersama-ai-pipeline && source .venv/bin/activate
    python check_audio_sources.py https://www.youtube.com/watch?v=<id>

It walks the same rungs `pipeline/asr.py` uses — yt-dlp direct, yt-dlp via
YTDLP_PROXY/YTDLP_COOKIES_FILE, then every configured Invidious/Piped mirror —
and prints which ones actually hand over audio bytes. Nothing is transcribed
and no Groq call is made, so it costs nothing but bandwidth.

Use the output to set YT_AUDIO_MIRRORS in .env to the hosts that answered.
Secrets are never printed: a configured proxy shows as "set", not its URL.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from pipeline import ytaudio                                    # noqa: E402
from pipeline.asr import _ytdlp_audio                           # noqa: E402
from pipeline.fetch import video_id_from_url, ydl_network_opts  # noqa: E402


def _kb(p: Path) -> str:
    try:
        return f"{p.stat().st_size // 1024} KB"
    except OSError:
        return "?"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    url = argv[1]
    vid = video_id_from_url(url)
    if not vid:
        print(f"could not parse a video id out of {url!r}")
        return 2
    print(f"video id: {vid}\n")

    results: list[tuple[str, str]] = []

    print("── rung 1: yt-dlp direct ──────────────────────────────────────")
    with tempfile.TemporaryDirectory() as td:
        t0 = time.time()
        got = _ytdlp_audio(url, td)
        results.append(("yt-dlp direct",
                        f"OK ({_kb(got)}, {time.time() - t0:.1f}s)" if got else "FAILED"))

    print("\n── rung 2: yt-dlp via proxy / cookies ─────────────────────────")
    net = ydl_network_opts()
    if not net:
        print("skipped — neither YTDLP_PROXY nor YTDLP_COOKIES_FILE is set")
        results.append(("yt-dlp via proxy/cookies", "not configured"))
    else:
        print(f"using: {'+'.join(sorted(net))} (values hidden)")
        with tempfile.TemporaryDirectory() as td:
            t0 = time.time()
            got = _ytdlp_audio(url, td, extra=net)
            results.append(("yt-dlp via proxy/cookies",
                            f"OK ({_kb(got)}, {time.time() - t0:.1f}s)" if got else "FAILED"))

    print("\n── rung 3: public mirrors ─────────────────────────────────────")
    hosts = ytaudio.mirrors()
    src = ("YT_AUDIO_MIRRORS (pinned)" if os.environ.get("YT_AUDIO_MIRRORS")
           else "built-in seeds + live instance directories")
    print(f"{len(hosts)} host(s) from {src}\n")
    alive: list[str] = []
    for kind, base in hosts:
        with tempfile.TemporaryDirectory() as td:
            t0 = time.time()
            try:
                stream = (ytaudio._piped_stream(base, vid) if kind == "piped"
                          else ytaudio._invidious_stream(base, vid))
            except Exception as e:  # noqa: BLE001
                print(f"  {kind:9} {base:45} lookup failed: {str(e)[:60]}")
                results.append((f"{kind} {base}", "lookup failed"))
                continue
            if not stream:
                print(f"  {kind:9} {base:45} no audio stream offered")
                results.append((f"{kind} {base}", "no audio stream"))
                continue
            got = ytaudio._stream_to_file(stream[0], td, stream[1])
            if got:
                print(f"  {kind:9} {base:45} OK ({_kb(got)}, {time.time() - t0:.1f}s)")
                results.append((f"{kind} {base}", "OK"))
                alive.append(f"{kind}:{base}")
            else:
                print(f"  {kind:9} {base:45} download failed")
                results.append((f"{kind} {base}", "download failed"))

    print("\n=== SUMMARY ===")
    for name, status in results:
        print(f"  {status:28} {name}")

    if alive:
        print("\nPut the working mirrors first in .env (order = try order):")
        print(f"YT_AUDIO_MIRRORS={','.join(alive)}")
    elif any(s.startswith("OK") for _, s in results):
        print("\nyt-dlp itself works here — no mirror needed.")
    else:
        print("\nNothing worked from this machine. The remaining lever is a residential\n"
              "proxy: set YTDLP_PROXY in .env and re-run this script.")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main(sys.argv))
