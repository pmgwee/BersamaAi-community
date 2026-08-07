"""One-off migration (2026-08-07): relocate already-posted news cards that the OLD
single-bucket `coding` routing dropped into #ai-llm-tools / #earn-money-with-ai, to the
newly-added finer-grained channels (#ai-company-investment, #ai-cybersecurity-bypass,
#earn-money-with-ai, #research-with-ai) per the owner's corrected calibration set.

How it works (faithful + correct-badge):
  1. MOVE_SET is the owner-supplied list of (headline_substring, dest_topic_key) for the
     ~36 misrouted cards. We match each against state/posted_log.jsonl to recover the
     original message_id + channel_id + source_url.
  2. For each match we READ the original message (Bot GET) and reconstruct a NewsItem from
     its embed (title / "Why it matters" body / url / image / category-from-badge).
  3. We REBUILD the payload with build_news_payload(item, topic=DEST) so the moved card
     carries the NEW topic's badge (e.g. "Security · Cyber"), not the stale "Coding & Agents".
  4. POST the rebuilt card to the destination webhook, then DELETE the original.
     A post failure leaves the original in place (no data loss).

Usage:
  python migrate_routing.py --dry-run     # print the move-list, touch nothing
  python migrate_routing.py               # do the moves (post + delete)

Needs in .env: DISCORD_TOKEN (read + delete originals), the destination webhooks
(DISCORD_COMPANY_INVESTMENT_WEBHOOK_URL / DISCORD_CYBERSECURITY_WEBHOOK_URL /
 DISCORD_FINANCE_WEBHOOK_URL / DISCORD_EDUCATION_WEBHOOK_URL), and the source webhooks
for webhook-delete fallback. Run from bersama-ai-pipeline/ with the venv active.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

from pipeline import news as N
from pipeline.stateutil import POSTED_LOG, read_jsonl

load_dotenv()

# Windows consoles default to cp1252 and choke on the emoji in card text / our labels.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

API = "https://discord.com/api/v10"
TOKEN = os.environ.get("DISCORD_TOKEN", "")
HEADERS = {"Authorization": f"Bot {TOKEN}"} if TOKEN else {}

# (lowercase distinctive headline substring, destination topic key).
# Source channel is whatever posted_log recorded (almost all #ai-dev-tools; the AI-fund
# outlier was in #earn-money-with-ai). Destinations per the owner's corrected calibration.
MOVE_SET: list[tuple[str, str]] = [
    # ── → #ai-company-investment (AI industry money/strategy/policy) ─────────────
    ("amd acquires taalas", "company_investment"),
    ("hassabis", "company_investment"),
    ("in-house ai chip", "company_investment"),
    ("high bandwidth flash", "company_investment"),
    ("20,000 nvidia", "company_investment"),
    ("alphafold team", "company_investment"),
    ("rtx gpu prices", "company_investment"),
    ("official position on open-weights", "company_investment"),
    ("major investment in", "company_investment"),            # Nvidia invests in SSI (r/singularity)
    ("safe superintelligence to scale", "company_investment"),  # Nvidia-Sutskever SSI (official)
    ("open secure ai alliance", "company_investment"),
    ("isolating anthropic", "company_investment"),
    ("publicly backs open-weight", "company_investment"),
    ("sign open-weight ai letter", "company_investment"),
    ("urge us to protect open-weight", "company_investment"),
    ("keep chinese open-weight", "company_investment"),
    ("ai fund down 67", "company_investment"),                # outlier originally in #earn-money-with-ai
    ("jensen huang's first x post", "company_investment"),
    # ── → #ai-cybersecurity-bypass (AI security as subject) ─────────────────────
    ("hacked another company", "cybersecurity"),
    ("1 in 3 threats", "cybersecurity"),
    ("inserting malicious code", "cybersecurity"),
    ("broke out of cybersecurity evals", "cybersecurity"),
    ("screenshot injection", "cybersecurity"),
    ("other ai agents escaped containment", "cybersecurity"),
    ("failed to stop a hugging face", "cybersecurity"),
    ("patch more chrome bugs", "cybersecurity"),
    ("breached three external companies", "cybersecurity"),
    ("nerv-break", "cybersecurity"),
    ("cryptographer weighs in", "cybersecurity"),
    ("claude mythos finds", "cybersecurity"),    # the cryptographic-vuln FINDING (not the Qwythos distill)
    ("mythos found crypto", "cybersecurity"),
    ("mai-cyber", "cybersecurity"),
    ("flash cyber for vulnerability", "cybersecurity"),  # the dedicated Cyber launch (not the broad 3.6 Flash release)
    # ── → #earn-money-with-ai (individual/builder money) ────────────────────────
    ("ran a real business autonomously", "finance"),
    # ── → #research-with-ai (papers / studies) ─────────────────────────────────
    ("barely publishing their research", "research_study"),
    ("long policy documents fail", "research_study"),
    ("kimi linear", "research_study"),
]

_EMOJI_TO_CAT = {v: k for k, v in N.CATEGORY_EMOJI.items()}


def _channel_to_webhook(channel_label: str) -> str:
    """Source-channel webhook URL (for webhook-delete fallback) by matching a Topic's
    channel label. Returns '' if none."""
    for t in N.TOPICS:
        if t.channel == channel_label:
            return N._topic_webhook(t)
    return ""


def _read_message(channel_id: str, message_id: str) -> dict | None:
    try:
        r = requests.get(f"{API}/channels/{channel_id}/messages/{message_id}",
                         headers=HEADERS, timeout=15)
        if r.status_code == 200:
            return r.json()
        print(f"   read {channel_id}/{message_id} -> HTTP {r.status_code} {r.text[:120]}")
    except Exception as e:  # noqa: BLE001
        print(f"   read failed: {e}")
    return None


def _delete_message_bot(channel_id: str, message_id: str) -> bool:
    try:
        r = requests.delete(f"{API}/channels/{channel_id}/messages/{message_id}",
                            headers=HEADERS, timeout=15)
        if r.status_code in (200, 204):
            return True
        print(f"   bot-delete -> HTTP {r.status_code} {r.text[:120]}")
    except Exception as e:  # noqa: BLE001
        print(f"   bot-delete failed: {e}")
    return False


def _delete_message_webhook(webhook_url: str, message_id: str) -> bool:
    """Fallback: delete a webhook message via its webhook token (no Manage Messages needed)."""
    try:
        r = requests.delete(f"{webhook_url}/messages/{message_id}", timeout=15)
        if r.status_code in (200, 204):
            return True
        print(f"   webhook-delete -> HTTP {r.status_code} {r.text[:120]}")
    except Exception as e:  # noqa: BLE001
        print(f"   webhook-delete failed: {e}")
    return False


def _embed_to_item(embed: dict, dest_topic: str) -> N.NewsItem | None:
    """Reconstruct a NewsItem from the original card's embed, retargeted to dest_topic."""
    title = (embed.get("title") or "").strip()
    desc = embed.get("description") or ""
    src = embed.get("url") or ""
    img = ((embed.get("image") or {}).get("url") or "")
    # body: strip the "**Why it matters**\n" lead and the trailing "*🔥 …*" heat line
    body = desc
    heat = ""
    if "**Why it matters**" in body:
        body = body.split("**Why it matters**", 1)[1].lstrip("\n").strip()
    if "*🔥" in body:
        body, _, heat = body.partition("*🔥")
        body = body.rstrip().rstrip("*").rstrip()
        heat = heat.rstrip("*").strip()
    # category: reverse-map the emoji in the badge
    badge = (embed.get("author") or {}).get("name", "")
    emoji = badge.split()[0] if badge else ""
    cat = _EMOJI_TO_CAT.get(emoji, "UPDATE")
    if not title:
        return None
    return N.NewsItem(topic=dest_topic, category=cat, headline=title,
                      body=body or "(no body)", source_url=src, heat_reason=heat), img


def build_moves() -> list[dict]:
    """Match MOVE_SET against posted_log -> list of move records (one per matched row)."""
    rows = list(read_jsonl(POSTED_LOG))
    moves: list[dict] = []
    for matcher, dest in MOVE_SET:
        for row in rows:
            headline = (row.get("headline") or "").lower()
            if matcher not in headline:
                continue
            mid = str(row.get("message_id") or "")
            cid = str(row.get("channel_id") or "")
            if not mid or not cid:
                continue
            # skip if already in (or heading to) the same dest channel
            dest_channel = N.TOPIC_BY_KEY[dest].channel
            if (row.get("channel") or "") == dest_channel:
                continue
            moves.append({
                "matcher": matcher, "dest": dest,
                "dest_channel": dest_channel,
                "headline": row.get("headline", ""),
                "source_channel": row.get("channel", ""),
                "channel_id": cid, "message_id": mid,
                "source_url": row.get("source_url", ""),
                "posted_at": row.get("posted_at", ""),
            })
    # de-dup identical (channel_id, message_id) keeps
    seen = set()
    uniq = []
    for m in moves:
        k = (m["channel_id"], m["message_id"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(m)
    return uniq


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print the move-list, change nothing")
    args = ap.parse_args()

    if not TOKEN:
        print("DISCORD_TOKEN not set — cannot read/delete originals.")
        return 2

    moves = build_moves()
    if not moves:
        print("No matching posted_log rows for MOVE_SET — nothing to move.")
        return 0

    # group counts
    from collections import Counter
    by_dest = Counter(m["dest"] for m in moves)
    print(f"MOVE-SET matched {len(moves)} card(s): " +
          ", ".join(f"{k}={v}" for k, v in by_dest.items()))
    unmatched = [m for m, _ in MOVE_SET if not any(mm["matcher"] == m for mm in moves)]
    if unmatched:
        print("⚠️ matchers with NO posted_log hit (skipped): " + ", ".join(unmatched))

    print("\nmove-list:")
    for m in moves:
        print(f"  [{m['source_channel']} -> {m['dest_channel']}] {m['headline'][:78]}")

    if args.dry_run:
        print(f"\nDRY-RUN: would repost+delete {len(moves)} card(s). Re-run without --dry-run to execute.")
        return 0

    print(f"\nExecuting {len(moves)} move(s)...")
    done = skipped = 0
    for m in moves:
        print(f"\n• {m['headline'][:78]}")
        msg = _read_message(m["channel_id"], m["message_id"])
        if not msg:
            print("   skip: could not read original (already deleted? permissions?)")
            skipped += 1
            continue
        embeds = msg.get("embeds") or []
        if not embeds:
            print("   skip: original has no embed to reconstruct")
            skipped += 1
            continue
        rec = _embed_to_item(embeds[0], m["dest"])
        if not rec:
            print("   skip: could not reconstruct item from embed")
            skipped += 1
            continue
        item, img = rec
        dest_topic = N.TOPIC_BY_KEY[m["dest"]]
        wh = N._topic_webhook(dest_topic)
        if not wh:
            print(f"   skip: no webhook for {m['dest']} ({dest_topic.webhook_env} unset)")
            skipped += 1
            continue
        payload = N.build_news_payload(item, image=img)
        try:
            posted = N._post_resilient(wh, payload)
        except Exception as e:  # noqa: BLE001
            print(f"   skip: post failed ({e}) — original left in place")
            skipped += 1
            continue
        if not posted or not posted.get("id"):
            print("   skip: post returned no message id — original left in place")
            skipped += 1
            continue
        print(f"   reposted -> {dest_topic.channel}")
        # delete the original: bot first, then webhook-token fallback
        ok = _delete_message_bot(m["channel_id"], m["message_id"])
        if not ok:
            src_wh = _channel_to_webhook(m["source_channel"])
            if src_wh:
                ok = _delete_message_webhook(src_wh, m["message_id"])
        print("   original deleted" if ok else
              "   ⚠️ reposted but could NOT delete original — clean up manually")
        done += 1 if ok else 0
    print(f"\nDone: {done} moved, {skipped} skipped, {len(moves)} total.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
