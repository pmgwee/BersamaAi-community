"""Weekly engagement digest — the owner's window into what the loop is learning.

Runs as `python -m pipeline.digest` via a weekly GitHub Actions workflow
(.github/workflows/engagement-digest.yml). Reads only committed state/ files,
formats ~30 lines (top/bottom posts, per-channel engagement rate, current
preference scores + quota allocation, n_events), and posts one embed to
#staff-chat via DISCORD_STAFF_CHAT_WEBHOOK_URL. No bot token needed.

Defensive: missing webhook or no data → print + exit 0, never fail the workflow.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta

import requests

from .stateutil import (
    POSTED_LOG, ENGAGEMENT_LOG, ACTIVITY_BASELINE, read_jsonl, read_json,
)
from .preferences import load_preferences, MIN_EVENTS
from .news import (
    _quota_for_topic, _cap_for_topic, _quota_for_shared,
    TOPICS, HN_QUOTA, HF_QUOTA, RSS_QUOTA, PER_TOPIC_QUOTA,
)

WINDOW_DAYS = 7


def _parse_ts(ts) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _week_events(now: datetime) -> list[dict]:
    """Engagement snapshots for posts made in the last WINDOW_DAYS days."""
    posts = {str(r.get("message_id")): r for r in read_jsonl(POSTED_LOG)}
    cutoff = now - timedelta(days=WINDOW_DAYS)
    events: list[dict] = []
    for r in read_jsonl(ENGAGEMENT_LOG):
        mid = str(r.get("message_id") or "")
        p = posts.get(mid)
        if not p:
            continue
        ptime = _parse_ts(p.get("posted_at"))
        if ptime is not None and ptime < cutoff:
            continue
        reacts = r.get("reactions") or {}
        engaged = sum(1 for v in reacts.values() if isinstance(v, (int, float)) and v > 0) or int(r.get("reply_count") or 0) > 0
        events.append({
            "headline": p.get("headline", ""), "topic": p.get("topic", ""),
            "channel": p.get("channel", ""), "reward": float(r.get("reward") or 0.0),
            "engaged": bool(engaged),
        })
    return events


def _line(prefix: str, s: str, width: int = 22) -> str:
    return f"`{prefix.ljust(width)}` {s}"


def build_text() -> str:
    now = datetime.now(timezone.utc)
    prefs = load_preferences()
    base = read_json(ACTIVITY_BASELINE, {}) or {}
    events = _week_events(now)

    ranked = sorted(events, key=lambda e: e["reward"], reverse=True)
    top = ranked[:5]
    bottom = [e for e in ranked if e["reward"] < 0][-3:]

    # Per-channel engagement rate.
    chans: dict[str, list[dict]] = {}
    for e in events:
        chans.setdefault(e["channel"], []).append(e)

    lines = [f"**📊 Weekly Engagement Digest** — <t:{int(now.timestamp())}:D>"]
    state = "ENABLED" if prefs.enabled else "dormant"
    lines.append(f"n_events **{prefs.n_events}** (actuator {state}; needs {MIN_EVENTS}+ and PREFS_ENABLED) · "
                 f"active_members_7d **{base.get('active_members_7d', '–')}**")

    lines.append(f"\n**🔥 Top {len(top)} posts by reward (last {WINDOW_DAYS}d):**")
    if top:
        for i, e in enumerate(top, 1):
            lines.append(f"`{i}.` **[{e['reward']:+.1f}]** {e['headline'][:70]} _({e['topic']})_")
    else:
        lines.append("_no posts in window_")

    if bottom:
        lines.append(f"\n**💀 Bottom {len(bottom)}:**")
        for i, e in enumerate(bottom, 1):
            lines.append(f"`{i}.` **[{e['reward']:+.1f}]** {e['headline'][:70]} _({e['topic']})_")

    if chans:
        lines.append(f"\n**📈 Per-channel engagement rate (last {WINDOW_DAYS}d):**")
        for ch, evs in sorted(chans.items()):
            hit = sum(1 for e in evs if e["engaged"])
            lines.append(_line(ch, f"{hit}/{len(evs)} posts reacted ({100 * hit // max(len(evs),1)}%)"))

    # Current preference scores.
    if prefs.topic or prefs.source or prefs.category:
        lines.append("\n**🎯 Current preference scores:**")

        def _fmt(d: dict, n: int = 5) -> str:
            items = sorted(((k, float(v)) for k, v in d.items() if v), key=lambda kv: kv[1], reverse=True)[:n]
            return ", ".join(f"{k} {v:+.1f}" for k, v in items) or "–"

        if prefs.topic:
            lines.append(_line("topic", _fmt(prefs.topic)))
        if prefs.source:
            lines.append(_line("source", _fmt(prefs.source)))
        if prefs.category:
            lines.append(_line("category", _fmt(prefs.category)))

    # Resulting quota allocation.
    lines.append("\n**⚙️ Current quota allocation (→ vs default):**")
    if prefs.enabled:
        bits = [f"{t.key} {_quota_for_topic(prefs.score('topic', t.key))}/{PER_TOPIC_QUOTA} (cap {_cap_for_topic(prefs.score('topic', t.key))})"
                for t in TOPICS]
        lines.append(_line("topics", "; ".join(bits)))
        lines.append(_line("shared",
            f"HN {_quota_for_shared(HN_QUOTA, prefs.score('source','HN'))}/{HN_QUOTA} · "
            f"HF {_quota_for_shared(HF_QUOTA, prefs.score('source','huggingface'))}/{HF_QUOTA} · "
            f"RSS {_quota_for_shared(RSS_QUOTA, prefs.score('source','official'))}/{RSS_QUOTA}"))
    else:
        lines.append(_line("all", f"static defaults (per-topic {PER_TOPIC_QUOTA}, cap 3, HN {HN_QUOTA}/HF {HF_QUOTA}/RSS {RSS_QUOTA})"))

    return "\n".join(lines)


def post(webhook_url: str, text: str, dry_run: bool = False) -> bool:
    """Post the digest as one embed (description cap 4096). Truncate if long."""
    payload = {"username": "BersamaAi", "embeds": [{
        "description": text[:4096],
        "color": 0x5865F2,
    }]}
    if dry_run:
        print(f"\n[digest DRY-RUN] -> #staff-chat\n{text}\n")
        return True
    r = requests.post(webhook_url, json=payload, timeout=15)
    if r.status_code not in (200, 204):
        print(f"[digest] post failed: {r.status_code} {r.text[:200]}")
        return False
    return True


def main() -> int:
    wh = os.environ.get("DISCORD_STAFF_CHAT_WEBHOOK_URL", "")
    dry_run = os.environ.get("DIGEST_DRY_RUN", "").lower() == "true"
    text = build_text()
    if not wh and not dry_run:
        print("[digest] DISCORD_STAFF_CHAT_WEBHOOK_URL not set — skipping (exit 0).")
        print(text)
        return 0
    post(wh, text, dry_run=dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
