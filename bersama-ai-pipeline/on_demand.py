"""On-demand summarizer + share portal — a tiny HTTP endpoint for phone-style access.

Run on the GCP VM. Set ON_DEMAND_TOKEN in .env, open the port in the GCP firewall,
then bookmark   http://<VM_IP>:8080/?token=<TOKEN>   on your phone. Paste a link →
it summarizes (YouTube) or turns it into a news card (any link) and posts to Discord.

Zero dependencies (Python stdlib only). Run it, keep it running:

    cd ~/bersama/bersama-ai-pipeline
    source .venv/bin/activate
    nohup python on_demand.py > on_demand.log 2>&1 &

Security: the token authenticates requests. This is plain HTTP (fine for personal
use on a phone bookmark); add a TLS terminator (Caddy/nginx) in front if you want HTTPS.
"""
from __future__ import annotations

import html
import os
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")  # pick up ON_DEMAND_TOKEN etc.
except ImportError:
    pass

PORT = int(os.environ.get("ON_DEMAND_PORT", "8080"))
TOKEN = os.environ.get("ON_DEMAND_TOKEN", "")
ROOT = Path(__file__).resolve().parent  # bersama-ai-pipeline/

# ── UI ────────────────────────────────────────────────────────────────────────
# Premium dark/blurple theme, mobile-first, zero external dependencies (system
# fonts + emoji). NOTE: token is injected with .replace("{tok}", TOKEN), NOT
# str.format — so the <style> block's CSS braces stay literal and safe.

_CSS = """<style>
:root{--bg:#0b0e16;--surface:#161a26;--surface2:#1d2230;--accent:#5865F2;--accent2:#8a96ff;
--text:#e8eaf0;--muted:#9aa1b1;--faint:#6b7280;--line:#262b3a;--green:#22c55e}
*{box-sizing:border-box}
html,body{margin:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
background:radial-gradient(1200px 600px at 50% -10%,#161a30 0%,#0b0e16 55%);
color:var(--text);min-height:100vh;-webkit-font-smoothing:antialiased}
.wrap{max-width:480px;margin:0 auto;padding:30px 18px 60px}
.brand{display:flex;align-items:center;gap:13px;margin-bottom:8px}
.logo{width:46px;height:46px;border-radius:13px;display:flex;align-items:center;justify-content:center;
font-size:24px;background:linear-gradient(135deg,var(--accent),var(--accent2));
box-shadow:0 8px 24px rgba(88,101,242,.4)}
.brand h1{margin:0;font-size:19px;letter-spacing:.2px}
.brand .tag{font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px}
.subtitle{color:var(--muted);font-size:13.5px;line-height:1.55;margin:16px 0 22px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:20px;
margin-bottom:15px;box-shadow:0 12px 34px rgba(0,0,0,.28)}
.card h2{margin:0 0 5px;font-size:16px;display:flex;align-items:center;gap:9px}
.card .desc{color:var(--muted);font-size:13px;line-height:1.6;margin:0 0 16px}
.dest{color:var(--accent2);font-weight:600}
.chips{display:flex;flex-wrap:wrap;gap:7px;margin:0 0 16px}
.chip{font-size:11px;color:var(--muted);background:var(--surface2);border:1px solid var(--line);
padding:5px 10px;border-radius:999px}
input{width:100%;padding:15px 14px;font-size:16px;background:var(--surface2);color:var(--text);
border:1px solid var(--line);border-radius:12px;outline:none;transition:border-color .15s,box-shadow .15s}
input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(88,101,242,.25)}
input::placeholder{color:var(--faint)}
button{width:100%;padding:15px;margin-top:13px;font-size:15px;font-weight:600;color:#fff;
background:linear-gradient(135deg,var(--accent),var(--accent2));border:0;border-radius:12px;
cursor:pointer;box-shadow:0 8px 20px rgba(88,101,242,.38);transition:transform .08s ease}
button:active{transform:translateY(1px) scale(.995)}
.note{color:var(--faint);font-size:12px;line-height:1.55;margin:13px 0 0}
a.back{display:inline-flex;align-items:center;gap:6px;margin-top:20px;color:var(--accent2);
text-decoration:none;font-size:14px;font-weight:600}
.badge{width:58px;height:58px;border-radius:16px;display:flex;align-items:center;justify-content:center;
font-size:30px;margin-bottom:16px}
.badge.green{background:linear-gradient(135deg,#22c55e,#16a34a);box-shadow:0 10px 26px rgba(34,197,94,.38)}
h2.result{margin:0 0 8px;font-size:21px}
p.result{color:var(--muted);font-size:13.5px;line-height:1.6;margin:0}
.foot{color:#525968;font-size:11px;text-align:center;margin-top:28px}
</style>"""


def _page(body: str) -> str:
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            '<meta name="theme-color" content="#0b0e16"><title>BersamaAi</title>' + _CSS +
            '</head><body><div class="wrap">' + body + '</div></body></html>')


_FORM = """
<div class="brand">
  <div class="logo">🤖</div>
  <div><h1>BersamaAi</h1><div class="tag">content engine</div></div>
</div>
<p class="subtitle">Paste a link from your phone. BersamaAi summarizes it or turns it into a news
card and posts to the right Discord channel automatically.</p>

<form class="card" method="post" action="/run?token={tok}">
  <h2>🎬 <span>Summarize a video</span></h2>
  <p class="desc">YouTube → a 5-point summary + thumbnail in <span class="dest">#curated-resources</span>.
  No captions? The audio is transcribed automatically.</p>
  <input name="url" type="url" placeholder="Paste a YouTube URL" required>
  <button type="submit">Summarize &nbsp;→&nbsp; #curated-resources</button>
</form>

<form class="card" method="post" action="/share?token={tok}">
  <h2>📣 <span>Share a link</span></h2>
  <p class="desc">Any link → a news card in the <b>matching topic channel</b>. It auto-routes by
  topic, grabs the thumbnail, and keeps the source language (Chinese stays Chinese).</p>
  <div class="chips">
    <span class="chip">Threads / X</span>
    <span class="chip">Instagram Reels</span>
    <span class="chip">小红书 / XHS</span>
    <span class="chip">TikTok</span>
    <span class="chip">Articles</span>
    <span class="chip">GitHub repos</span>
  </div>
  <input name="url" type="url" placeholder="Paste any link" required>
  <button type="submit">Share &nbsp;→&nbsp; matching channel</button>
  <p class="note">📹 For videos (Reels / XHS / TikTok) the audio is transcribed, so the card describes
  what the video actually shows — not just the caption.</p>
</form>

<div class="foot">BersamaAi · Malaysia AI community</div>
"""

_DONE = """
<div class="badge green">✅</div>
<h2 class="result">Summarizing…</h2>
<p class="result">Running in the background. The summary will appear in
<span class="dest">#curated-resources</span> in about 1–3 minutes — longer if the video has no
captions (it's being transcribed).</p>
<a class="back" href="/?token={tok}">← Share another</a>
"""

_SHARED = """
<div class="badge green">✅</div>
<h2 class="result">Building the card…</h2>
<p class="result">Running in the background. The card will appear in the matching topic channel in
about 10–30 seconds — a little longer for videos while the audio is transcribed.</p>
<a class="back" href="/?token={tok}">← Share another</a>
"""


def _summarize_async(url: str) -> None:
    """Run the summarizer in the background (fire-and-forget). Errors go to logs."""
    try:
        subprocess.run(
            ["python", "-m", "pipeline.main", "--mode", "url", "--url", url],
            cwd=str(ROOT), capture_output=True, text=True, timeout=900,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[on-demand] summarize failed for {url}: {e}")


def _share_async(url: str) -> None:
    """Share a link as a news card in the background (fire-and-forget)."""
    try:
        subprocess.run(
            ["python", "-m", "pipeline.main", "--mode", "share", "--url", url],
            cwd=str(ROOT), capture_output=True, text=True, timeout=300,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[on-demand] share failed for {url}: {e}")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: str) -> None:
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _token_ok(self) -> bool:
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        return bool(TOKEN) and urllib.parse.parse_qs(qs).get("token", [""])[0] == TOKEN

    def _render(self, tmpl: str) -> str:
        return _page(tmpl.replace("{tok}", html.escape(TOKEN)))

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/" and self._token_ok():
            return self._send(200, self._render(_FORM))
        self._send(403, "forbidden — bad or missing token")

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if not self._token_ok():
            return self._send(403, "forbidden — bad or missing token")
        length = int(self.headers.get("Content-Length") or 0)
        data = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        url = (data.get("url", [""])[0]).strip()
        if not url or "://" not in url:
            return self._send(400, "missing or invalid url")
        if path == "/run":
            threading.Thread(target=_summarize_async, args=(url,), daemon=True).start()
            print(f"[on-demand] summarize queued: {url}")
            self._send(200, self._render(_DONE))
        elif path == "/share":
            threading.Thread(target=_share_async, args=(url,), daemon=True).start()
            print(f"[on-demand] share queued: {url}")
            self._send(200, self._render(_SHARED))
        else:
            self._send(404, "not found")

    def log_message(self, *args):  # quieter access log
        pass


if __name__ == "__main__":
    if not TOKEN:
        print("Set ON_DEMAND_TOKEN in .env first (any random secret).")
        raise SystemExit(1)
    print(f"[on-demand] listening on 0.0.0.0:{PORT} (token configured)")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
