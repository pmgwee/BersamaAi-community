"""Publish to Discord (rich plain-text webhook message) and Telegram (sendMessage).

Discord posts a readable card matching the #ai-dev-tools news style (divider +
badge + bold title + bare source URL that auto-unfurls a preview + byline +
'Why it matters' + 'Key takeaways'), split into <=2000-char messages if needed.
Telegram caps at 4096 UTF-16 code units, so we send one English message.

Security: dry-run NEVER prints the webhook URL or bot token — both are bearer
credentials. They are masked in all log output.
"""
from __future__ import annotations

import requests

from .summarize import Summary

DISCORD_MESSAGE_LIMIT = 2000   # plain-text message cap (rich cards, not embeds)
TELEGRAM_TEXT_LIMIT = 4000   # 4096 hard cap; 4000 leaves headroom for markup + emoji

DISCORD_USERNAME = "BersamaAi"


def _mask_url(url: str) -> str:
    """Hide the bearer secret in a URL's last path segment (webhook token / etc.)."""
    if "/" not in url:
        return "***"
    head, _ = url.rsplit("/", 1)
    return head + "/***"


# ── Discord ──────────────────────────────────────────────────────────────────

def _talk_messages(summary: Summary, meta: dict) -> list[str]:
    """Rich plain-text card matching the #ai-dev-tools news style: divider + badge
    + bold title + bare source URL (auto-unfurls a YouTube preview) + byline +
    'Why it matters' + 'Key takeaways' bullets. Splits into <=2000-char messages
    at bullet boundaries if needed."""
    title = meta.get("title") or "AI talk"
    url = summary.source_url or meta.get("webpage_url") or meta.get("url") or ""
    speaker = summary.speaker or meta.get("uploader") or meta.get("channel") or ""
    mins = (summary.duration_sec or 0) // 60
    dur = f"{mins} min" if mins else ""
    byline = " · ".join(p for p in (speaker, dur) if p)
    header = (
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "**🎬 Curated Talk**\n\n"
        f"**{title}**\n"
        + (f"🔗 {url}\n" if url else "")
        + (f"*By {byline}*\n" if byline else "")
        + f"\n**Why it matters**\n{summary.hook}\n\n**Key takeaways**\n"
    )
    msgs: list[str] = []
    cur = header
    for pt in summary.points:
        line = f"• {pt}"
        if len(cur) + len(line) + 1 > DISCORD_MESSAGE_LIMIT:
            msgs.append(cur.rstrip())
            cur = "*(continued)*\n"
        cur += line + "\n"
    if cur.strip():
        msgs.append(cur.rstrip())
    return msgs or [header[:DISCORD_MESSAGE_LIMIT]]


def build_discord_payload(summary: Summary, meta: dict) -> dict:
    return {"username": DISCORD_USERNAME, "messages": _talk_messages(summary, meta)}


def send_discord(webhook_url: str, payload: dict, dry_run: bool = False) -> None:
    for content in payload.get("messages", []):
        if dry_run:
            print(f"\n[discord DRY-RUN] POST {_mask_url(webhook_url)}\n{content}\n")
            continue
        r = requests.post(webhook_url,
                          json={"username": payload.get("username"), "content": content},
                          timeout=15)
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
