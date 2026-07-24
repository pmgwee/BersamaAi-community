"""Engagement sweep — the sensor that reads reactions off posted news cards.

Runs as `python -m pipeline.engagement` (a step in the news-digest workflow,
every ~3h). For every news card in state/posted_log.jsonl that is 24h–8d old,
fetch its current reactions + reply count via Discord REST (bot token, read-only
— no Gateway), compute a reward, and append a snapshot to state/engagement.jsonl.

Also refreshes state/activity_baseline.json (the 7-day active-member count used
to normalize rewards so growth doesn't inflate scores), and prunes the JSONL
logs past their retention windows.

Defensive by design: a missing DISCORD_TOKEN, a deleted message, a rate limit,
or a parse error on one post must NEVER fail the news run — exit 0 and move on.
"""
from __future__ import annotations

import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import requests

from .stateutil import (
    POSTED_LOG, ENGAGEMENT_LOG, ACTIVITY_BASELINE,
    read_jsonl, append_jsonl, atomic_write_json, rewrite_jsonl, read_posted_log,
)
from .preferences import (
    W_REACT_UP, W_REACT_FIRE, W_REACT_MEH, W_REACT_OTHER, W_REPLY,
    REWARD_MIN, REWARD_MAX, DEFAULT_ACTIVE_BASELINE,
)

API = "https://discord.com/api/v10"

# The three seeded reactions. MUST match bersama-bot/bot.py SEED_EMOJIS exactly.
# Order is a contract: [0]=useful (+2), [1]=amazing (+3), [2]=not-for-me (−2).
# Change the emoji HERE only — _reactions + _reward key off these positions, and
# the bot's own seed reaction (the `me` flag) is subtracted so only MEMBER clicks count.
SEED_EMOJIS = ("👍", "🔥", "👎")

SWEEP_MIN_AGE_DAYS = 1     # don't sweep a card until it has had ~24h to collect reactions
SWEEP_MAX_AGE_DAYS = 8     # beyond this the card is stale; stop sweeping it
FETCH_MAX_MSGS = 500       # hard cap per channel per sweep (≤5 paginated calls)
ACTIVE_WINDOW_DAYS = 7
POSTED_RETENTION_DAYS = 14      # posted_log: keep a buffer beyond the sweep window
ENGAGEMENT_RETENTION_DAYS = 60  # engagement.jsonl: the EMA recompute window


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_emoji(e: str) -> str:
    """Strip U+FE0F variation selectors (mirrors bersama-bot/bot.py _norm_emoji)
    so 🔥/😐 stored with/without the selector compare equal."""
    return (e or "").replace("️", "")


def _parse_ts(ts) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _age_days(ts, now: datetime) -> float | None:
    dt = _parse_ts(ts)
    if dt is None:
        return None
    return max((now - dt).total_seconds() / 86400.0, 0.0)


def _get(session: requests.Session, path: str, token: str, params: dict | None = None):
    """One REST GET with bot-token auth + minimal 429 backoff. Returns parsed JSON
    or None on any non-200 (deleted message, missing perms, etc.)."""
    url = f"{API}{path}"
    for _ in range(2):  # one retry to absorb a transient rate limit
        try:
            r = session.get(url, headers={"Authorization": f"Bot {token}"}, params=params, timeout=15)
        except requests.RequestException:
            return None
        if r.status_code == 429:
            try:
                wait = float((r.json() or {}).get("retry_after", 1.0))
            except ValueError:
                wait = 1.0
            time.sleep(min(max(wait, 0.5), 5.0))
            continue
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                return None
        return None  # 403/404/5xx — skip this call, don't crash the sweep
    return None


def _fetch_recent(session: requests.Session, token: str, channel_id: str,
                  cutoff: datetime) -> tuple[list[dict], dict[str, dict]]:
    """Page backwards through a channel's recent messages until we pass the 8-day
    cutoff or hit FETCH_MAX_MSGS. Returns (messages, {message_id: message})."""
    msgs: list[dict] = []
    by_id: dict[str, dict] = {}
    before = None
    while len(msgs) < FETCH_MAX_MSGS:
        params: dict = {"limit": 100}
        if before:
            params["before"] = before
        batch = _get(session, f"/channels/{channel_id}/messages", token, params)
        if not isinstance(batch, list) or not batch:
            break
        for m in batch:
            mid = str(m.get("id") or "")
            if mid:
                by_id[mid] = m
        msgs.extend(batch)
        before = str(batch[-1].get("id") or "")
        oldest = _parse_ts(batch[-1].get("timestamp"))
        if oldest is None or oldest < cutoff:
            break
        if len(batch) < 100:
            break  # reached the channel's history end
    return msgs, by_id


_LINK_RE = re.compile(r"discord\.com/channels/\d+/\d+/(\d+)", re.IGNORECASE)


def _reply_counts(msgs: list[dict], target_ids: set[str]) -> dict[str, int]:
    """One pass over channel history → {post_id: reply_count}. A message counts
    as a reply if it has a message_reference to a target OR links to it."""
    idx = {mid: 0 for mid in target_ids}
    for m in msgs:
        ref = (m.get("message_reference") or {}).get("message_id")
        if ref and str(ref) in idx:
            idx[str(ref)] += 1
            continue
        for mid in _LINK_RE.findall(m.get("content", "") or ""):
            if mid in idx:
                idx[mid] += 1
    return idx


def _reactions(msg: dict) -> dict:
    """Member-only reaction counts per seeded emoji + an 'other' bucket. Uses the
    per-reaction `me` flag to subtract the bot's own seed (strictly more correct
    than a blanket −1: handles a partial seed if the bot was briefly down)."""
    seed = {e: 0 for e in SEED_EMOJIS}
    other = 0
    for r in (msg.get("reactions") or []):
        name = _norm_emoji((r.get("emoji") or {}).get("name", ""))
        count = int(r.get("count") or 0)
        net = count - (1 if r.get("me") else 0)
        if net <= 0:
            continue
        if name in seed:
            seed[name] += net
        else:
            other += net
    return {**seed, "other": other}


def _reward(reacts: dict, reply_count: int, baseline: int | float) -> float:
    up, fire, meh = SEED_EMOJIS  # positional roles (see SEED_EMOJIS contract)
    raw = (
        W_REACT_UP * reacts.get(up, 0)
        + W_REACT_FIRE * reacts.get(fire, 0)
        + W_REACT_MEH * reacts.get(meh, 0)
        + W_REACT_OTHER * reacts.get("other", 0)
        + W_REPLY * reply_count
    )
    b = baseline if baseline and baseline > 0 else DEFAULT_ACTIVE_BASELINE
    return max(REWARD_MIN, min(REWARD_MAX, raw / b))


def _active_members(all_msgs: list[dict], now: datetime) -> int:
    """Distinct non-bot authors in the last ACTIVE_WINDOW_DAYS across fetched
    channels — the reward normalizer (proxy for guild-wide activity; we only
    fetch news channels, but that's where the conversational members are)."""
    cutoff = now - timedelta(days=ACTIVE_WINDOW_DAYS)
    members: set[str] = set()
    for m in all_msgs:
        ts = _parse_ts(m.get("timestamp"))
        if ts is None or ts < cutoff:
            continue
        author = m.get("author") or {}
        if not author.get("bot") and author.get("id"):
            members.add(str(author["id"]))
    return len(members)


def _prune(now: datetime) -> None:
    """Drop JSONL rows past retention so the committed state files stay small."""
    posts = [r for r in read_jsonl(POSTED_LOG)
             if (_age_days(r.get("posted_at"), now) or 0) <= POSTED_RETENTION_DAYS]
    rewrite_jsonl(POSTED_LOG, posts)
    snaps = [r for r in read_jsonl(ENGAGEMENT_LOG)
             if (_age_days(r.get("sweep_at"), now) or 0) <= ENGAGEMENT_RETENTION_DAYS]
    rewrite_jsonl(ENGAGEMENT_LOG, snaps)


def sweep() -> int:
    token = os.environ.get("DISCORD_TOKEN", "")
    if not token:
        print("[engagement] DISCORD_TOKEN not set — skipping sweep (exit 0).")
        return 0

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=SWEEP_MAX_AGE_DAYS)

    # Target posts: 24h–8d old (in the sweep window).
    targets: dict[str, dict] = {}
    for row in read_posted_log():   # auto-news (posted_log.jsonl) + owner /share (posted_log_share.jsonl)
        mid = str(row.get("message_id") or "")
        if not mid:
            continue
        age = _age_days(row.get("posted_at"), now)
        if age is None or not (SWEEP_MIN_AGE_DAYS <= age <= SWEEP_MAX_AGE_DAYS):
            continue
        targets[mid] = row

    # Group by channel; fetch each channel's recent history once.
    by_chan: dict[str, list[str]] = defaultdict(list)
    for mid, p in targets.items():
        cid = str(p.get("channel_id") or "")
        if cid:
            by_chan[cid].append(mid)

    session = requests.Session()
    all_msgs: list[dict] = []
    chan_msg_map: dict[str, dict[str, dict]] = {}
    reply_idx: dict[str, int] = {}
    for cid, mids in by_chan.items():
        msgs, by_id = _fetch_recent(session, token, cid, cutoff)
        all_msgs.extend(msgs)
        chan_msg_map[cid] = by_id
        reply_idx.update(_reply_counts(msgs, set(mids)))

    baseline_raw = _active_members(all_msgs, now)
    baseline = baseline_raw or DEFAULT_ACTIVE_BASELINE
    atomic_write_json(ACTIVITY_BASELINE, {
        "updated_at": _now_iso(), "active_members_7d": baseline_raw, "window_days": ACTIVE_WINDOW_DAYS,
    })

    swept = 0
    for mid, p in targets.items():
        msg = chan_msg_map.get(str(p.get("channel_id") or ""), {}).get(mid)
        if not msg:
            continue  # post deleted, or scrolled past the fetched window — skip silently
        reacts = _reactions(msg)
        rc = reply_idx.get(mid, 0)
        reward = _reward(reacts, rc, baseline)
        age_h = int(round((_age_days(p.get("posted_at"), now) or 0) * 24))
        append_jsonl(ENGAGEMENT_LOG, {
            "message_id": mid, "sweep_at": _now_iso(), "age_h": age_h,
            "reactions": reacts, "reply_count": rc, "reward": round(reward, 3),
        })
        swept += 1

    print(f"[engagement] swept {swept}/{len(targets)} posts in the 24h–8d window; "
          f"active_members_7d={baseline_raw} (baseline={baseline}).")
    _prune(now)
    return 0


def main() -> int:
    return sweep()


if __name__ == "__main__":
    raise SystemExit(main())
