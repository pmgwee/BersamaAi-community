"""On-demand summarizer trigger — a tiny HTTP endpoint for phone-style access.

Run on the GCP VM. Set ON_DEMAND_TOKEN in .env, open the port in the GCP firewall,
then bookmark   http://<VM_IP>:8080/?token=<TOKEN>   on your phone. Paste a
YouTube URL into the form → it summarizes + posts to #curated-resources.

Zero dependencies (Python stdlib only). Run it, keep it running:

    cd ~/bersama/bersama-ai-pipeline
    source .venv/bin/activate
    nohup python on_demand.py > logs/on_demand.log 2>&1 &

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

FORM = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>BersamaAi summarize</title></head>
<body style='font-family:system-ui,sans-serif;max-width:520px;margin:40px auto;padding:0 16px'>
<h2>🎬 Summarize a YouTube video</h2>
<p style='color:#666'>Posts a 5-point summary to <b>#curated-resources</b>.</p>
<form method=post action='/run?token={tok}'>
<input name=url placeholder='Paste YouTube URL'
  style='width:100%;padding:14px;font-size:16px;border:1px solid #ccc;border-radius:8px' required>
<button style='width:100%;padding:14px;margin-top:12px;font-size:16px;background:#5865F2;color:#fff;
  border:0;border-radius:8px'>Summarize &amp; post</button>
</form></body></html>"""

DONE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'></head>
<body style='font-family:system-ui,sans-serif;max-width:520px;margin:40px auto;padding:0 16px'>
<h2>✅ Started</h2>
<p>Summarizing in the background — check <b>#curated-resources</b> in ~1–3 min.</p>
<p style='color:#666'>Caption-less videos take longer (audio transcription).</p>
<p><a href='/?token={tok}'>← summarize another</a></p>
</body></html>"""


def _summarize_async(url: str) -> None:
    """Run the summarizer in the background (fire-and-forget). Errors go to logs."""
    try:
        subprocess.run(
            ["python", "-m", "pipeline.main", "--mode", "url", "--url", url],
            cwd=str(ROOT), capture_output=True, text=True, timeout=900,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[on-demand] summarize failed for {url}: {e}")


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

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/" and self._token_ok():
            return self._send(200, FORM.format(tok=html.escape(TOKEN)))
        self._send(403, "forbidden — bad or missing token")

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path != "/run" or not self._token_ok():
            return self._send(403, "forbidden — bad or missing token")
        length = int(self.headers.get("Content-Length") or 0)
        data = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        url = (data.get("url", [""])[0]).strip()
        if not url or "://" not in url:
            return self._send(400, "missing or invalid url")
        threading.Thread(target=_summarize_async, args=(url,), daemon=True).start()
        print(f"[on-demand] queued: {url}")
        self._send(200, DONE.format(tok=html.escape(TOKEN)))

    def log_message(self, *args):  # quieter access log
        pass


if __name__ == "__main__":
    if not TOKEN:
        print("Set ON_DEMAND_TOKEN in .env first (any random secret).")
        raise SystemExit(1)
    print(f"[on-demand] listening on 0.0.0.0:{PORT} (token configured)")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
