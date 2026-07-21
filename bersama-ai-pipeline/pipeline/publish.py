"""Publish to Discord (webhook embed) and Telegram (sendMessage), length-safe.

Both platforms cap message size. We use a Discord embed (description limit 4096)
and hard-wrap into multiple embeds only if a single 5-point summary somehow
overflows. Telegram caps at 4096 UTF-16 code units, so we send one English
message (summary + source link).

Security: dry-run NEVER prints the webhook URL or bot token — both are bearer
credentials. They are masked in all log output.
"""
from __future__ import annotations

import requests

from .summarize import Summary

DISCORD_EMBED_DESC_LIMIT = 4096
TELEGRAM_TEXT_LIMIT = 4000   # 4096 hard cap; 4000 leaves headroom for markup + emoji
BRAND_COLOR = 0x5865F2       # blurple; change to your brand

DISCORD_USERNAME = "BersamaAi"
DISCORD_FOOTER = "BersamaAi · curated AI talks"


def _mask_url(url: str) -> str:
    """Hide the bearer secret in a URL's last path segment (webhook token / etc.)."""
    if "/" not in url:
        return "***"
    head, _ = url.rsplit("/", 1)
    return head + "/***"


# ── Discord ──────────────────────────────────────────────────────────────────

def _discord_blocks(summary: Summary) -> list[str]:
    """One or more embed `description` chunks, each <= DISCORD_EMBED_DESC_LIMIT."""
    body = f"**{summary.hook}**\n\n" + "\n\n".join(f"• {p}" for p in summary.points)
    if len(body) <= DISCORD_EMBED_DESC_LIMIT:
        return [body]
    # hard-wrap an unexpectedly long body at the limit (rare; may split markdown)
    return [body[i:i + DISCORD_EMBED_DESC_LIMIT]
            for i in range(0, len(body), DISCORD_EMBED_DESC_LIMIT)]


def _yt_thumbnail(meta: dict):
    """YouTube thumbnail URL — prefer yt-dlp's, else build from the video id."""
    thumb = meta.get("thumbnail")
    if thumb:
        return thumb
    vid = meta.get("id")
    return f"https://img.youtube.com/vi/{vid}/hqdefault.jpg" if vid else None


def build_discord_payload(summary: Summary, meta: dict) -> dict:
    title = (meta.get("title") or "AI talk summary")[:256]
    chunks = _discord_blocks(summary)
    thumb = _yt_thumbnail(meta)
    embeds = []
    for i, desc in enumerate(chunks):
        embeds.append({
            "title": title if i == 0 else f"{title} (cont’d {i + 1})",
            "description": desc,
            "url": summary.source_url or None,
            "color": BRAND_COLOR,
            "footer": {"text": DISCORD_FOOTER},
            "author": {"name": summary.speaker[:256]} if summary.speaker else None,
            # thumbnail on the first embed only; Discord omits null fields
            "thumbnail": {"url": thumb} if (i == 0 and thumb) else None,
        })
    return {"username": DISCORD_USERNAME, "embeds": embeds}


def send_discord(webhook_url: str, payload: dict, dry_run: bool = False) -> None:
    # Discord caps total embed payload; if we built >10 embeds, send in batches.
    embeds = payload.get("embeds", [])
    for i in range(0, len(embeds), 10):
        batch = {**payload, "embeds": embeds[i:i + 10]}
        if dry_run:
            print(f"\n[discord DRY-RUN] POST {_mask_url(webhook_url)}\n{batch}\n")
            continue
        r = requests.post(webhook_url, json=batch, timeout=15)
        if r.status_code not in (200, 204):
            raise RuntimeError(f"Discord webhook failed: {r.status_code} {r.text[:300]}")


# ── Telegram ─────────────────────────────────────────────────────────────────

def _tg_escape(text: str) -> str:
    """Escape for Telegram HTML parse_mode."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tg_truncate(text: str, limit_units: int = TELEGRAM_TEXT_LIMIT) -> str:
    """Truncate to <= limit_units UTF-16 code units (Telegram's actual limit).

    Python len() counts code points; Telegram counts UTF-16 units. For text with
    astral-plane chars (e.g. 🎬) they differ, so encode-and-trim.
    """
    b = text.encode("utf-16-le")
    if len(b) // 2 <= limit_units:
        return text
    return b[: limit_units * 2].decode("utf-16-le", errors="ignore")


def _tg_chunks(summary: Summary) -> list[str]:
    """One HTML message: English summary + speaker + source link."""
    lines = [f"<b>{_tg_escape(summary.hook)}</b>"] + [
        f"• {_tg_escape(p)}" for p in summary.points
    ]
    text = "\n".join(lines) + (
        f"\n\n🎬 {_tg_escape(summary.speaker)} — "
        f"<a href=\"{_tg_escape(summary.source_url)}\">watch</a>"
    )
    return [_tg_truncate(text)]


def send_telegram(
    token: str, channel_id: str, summary: Summary, dry_run: bool = False
) -> None:
    if not token or not channel_id:
        print("[telegram] skipped — TELEGRAM_BOT_TOKEN or CHANNEL_ID not set")
        return
    if dry_run:
        print("\n[telegram DRY-RUN] POST https://api.telegram.org/bot***/sendMessage "
              f"→ {channel_id}")
    base = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in _tg_chunks(summary):
        if dry_run:
            print(chunk + "\n")
            continue
        r = requests.post(base, data={
            "chat_id": channel_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }, timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"Telegram sendMessage failed: {r.status_code} {r.text[:300]}")


def alert(token: str, chat_id: str, message: str, dry_run: bool = False) -> None:
    """DM the maintainer when a run fails or a video is skipped."""
    if not chat_id:
        print(f"[alert] no TELEGRAM_DM_CHAT_ID set; would have sent: {message}")
        return
    base = f"https://api.telegram.org/bot{token}/sendMessage"
    if dry_run:
        print(f"\n[alert DRY-RUN] → {chat_id}\n{message}\n")
        return
    try:
        requests.post(base, data={"chat_id": chat_id, "text": message}, timeout=15)
    except Exception as e:  # noqa: BLE001 — alerts must never crash the run
        print(f"[alert] failed to send: {e}")
