"""X / Twitter account → Discord daily digest (e.g. @EconomyApp → #stock-invest).

X has no free official timeline API and blocks anonymous scrapers; the one
cookieless path that still works (~2026-07) is a Nitter instance's
`/<screen>/rss` feed — RSS 2.0 with the tweet id in `<guid>`, `pubDate`, the
full tweet text in `<title>`, and the card image in the `<description>` CDATA.
yt-dlp has NO profile-timeline extractor (only individual tweets), and the
public RSSHub Twitter route is dead, so Nitter RSS is the realistic free path.

Nitter instances die on a rolling basis, so we try a config list in order and
take the first that returns real items; if every instance is down we post a
health warning to #staff-chat (same posture as the news run posting 0 cards).

No LLM: each new tweet becomes ONE verbatim card — the account's own words,
faithful to its financial figures, in the source language (no translation).
Runs daily on GitHub Actions (.github/workflows/stock-digest.yml). The on-demand
`/share` path never imports this module, so a change here needs no VM pull.
"""
from __future__ import annotations

import html as html_mod
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote

import requests

# Generic posting / safety / health guards shared with the news engine.
from .news import _post, _is_staff_webhook, _staff_alert, BRAND_COLOR

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
HEADERS = {"User-Agent": "Mozilla/5.0 (BersamaAi-x-digest/1.0; +rss)"}

# Tried in order; first returning real items wins. Rotate (one-line edit) when
# an instance dies. Verified 2026-07-25: nitter.net works; the rest are currently
# Cloudflare-blocked / "RSS reader not yet whitelisted" but may rotate back in.
NITTER_INSTANCES = [
    "nitter.net",
    "xcancel.com",
    "nitter.poast.org",
    "nitter.privacydev.net",
]

# (screen_name, webhook env var, channel label). Add more accounts here.
X_SUBSCRIPTIONS = [
    ("EconomyApp", "DISCORD_STOCK_INVEST_WEBHOOK_URL", "#stock-invest"),
]

MAX_PER_RUN = 8        # posts per account per run
MAX_AGE_DAYS = 7       # drop tweets older than this (archive, not "new")
FIRST_RUN_MAX = 3      # bound day-1 volume so the first run isn't a wall of old cards
SEEN_CAP = 500         # keep the newest N tweet ids per account (insertion order)
REQUEST_TIMEOUT = 15

# `/status/123…` in a nitter <link> or guid; the bare snowflake form is also accepted.
_STATUS_ID = re.compile(r"/status(?:es)?/(\d+)")
# Nitter prefixes a reply's <title> with "R to @user:" — strip it so a thread
# continuation reads as its own clean card, not a reply artifact.
_REPLY_PREFIX = re.compile(r"^R to @\w+:\s*")
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


# ── RSS helpers (inlined + minimal, so this module doesn't reach into news.py's
#    private `_rss_*` — keeps it decoupled and light to import) ────────────────

def _rss_field(el, names: set[str]) -> str:
    """First non-empty text/href of a child whose local tag is in `names`."""
    for child in el:
        if child.tag.split("}")[-1] in names:
            t = (child.text or child.get("href") or "").strip()
            if t:
                return t
    return ""


def _parse_date(raw: str):
    """RFC-822 <pubDate> or ISO-8601 → aware datetime, or None."""
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _tweet_id(guid: str, link: str) -> str:
    """Bare snowflake from `<guid>` (or the trailing id of a status URL)."""
    for raw in (guid, link):
        if not raw:
            continue
        if raw.strip().isdigit():
            return raw.strip()
        m = _STATUS_ID.search(raw)
        if m:
            return m.group(1)
    return ""


def _first_img_src(html_frag: str) -> str:
    m = re.search(r"<img[^>]+src=[\"']([^\"']+)[\"']", html_frag or "", re.IGNORECASE)
    return m.group(1) if m else ""


def _rewrite_image(src: str) -> str:
    """`nitter.net/pic/<urlencoded x>` → `pbs.twimg.com/<decoded x>` so Discord
    fetches Twitter's durable CDN directly (survives a Nitter outage). Non-nitter
    URLs are passed through unchanged."""
    if not src:
        return ""
    m = re.search(r"/pic/(.+)$", src)
    if not m:
        return src
    return "https://pbs.twimg.com/" + unquote(m.group(1))


def _headline(text: str, desc_html: str) -> str:
    """Lead line of the tweet — the first `<br>`-delimited segment of the CDATA
    (e.g. "$TSLA Tesla's Q2 FY26 visualized."), else a word-boundary truncation
    of the full text. Becomes the embed title."""
    segs = re.split(r"<br\s*/?>", desc_html or "", maxsplit=1)
    lead = html_mod.unescape(re.sub(r"<[^>]+>", "", segs[0])).strip() if segs else ""
    if lead:
        return lead[:110]
    t = (text or "").strip()
    if len(t) <= 110:
        return t
    cut = t[:110].rsplit(" ", 1)[0]
    return cut + "…"


def _clean(text: str) -> str:
    return html_mod.unescape(re.sub(r"\s+", " ", text or "")).strip()


def _fetch_instance_feed(inst: str, screen: str) -> str:
    """One Nitter instance's RSS body (""). Honors 429 Retry-After once."""
    url = f"https://{inst}/{screen}/rss"
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code == 429:
            wait = min(max(int(r.headers.get("Retry-After") or 15), 10), 60)
            print(f"[x] {inst} 429 — retrying in {wait}s")
            time.sleep(wait)
            r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            print(f"[x] {inst}/{screen} -> HTTP {r.status_code}")
            return ""
        return r.text
    except Exception as e:  # noqa: BLE001 — one dead instance must not kill the run
        print(f"[x] {inst}/{screen} failed: {e}")
        return ""


def fetch_x_posts(screen_name: str) -> tuple[list[dict], str | None]:
    """Return (posts_newest_first, used_instance). used_instance is None when
    every instance failed (caller raises a staff-chat alert)."""
    for inst in NITTER_INSTANCES:
        body = _fetch_instance_feed(inst, screen_name)
        if not body:
            continue
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            print(f"[x] {inst}/{screen_name} parse failed: {e}")
            continue
        items = [el for el in root.iter() if el.tag.split("}")[-1] == "item"]
        if not items:
            print(f"[x] {inst}/{screen_name} -> 0 items")
            continue
        posts: list[dict] = []
        for el in items:
            tid = _tweet_id(_rss_field(el, {"guid"}), _rss_field(el, {"link"}))
            title = _REPLY_PREFIX.sub("", _clean(_rss_field(el, {"title"})))
            if not tid or not title:
                continue
            desc_html = _rss_field(el, {"description"})
            posts.append({
                "id": tid,
                "text": title,
                "headline": _headline(title, desc_html),
                "url": f"https://x.com/{screen_name}/status/{tid}",
                "image": _rewrite_image(_first_img_src(desc_html)),
                "created_at": _parse_date(_rss_field(el, {"pubDate"})),
            })
        if posts:
            posts.sort(key=lambda p: p["created_at"] or _EPOCH, reverse=True)
            print(f"[x] {inst}/{screen_name} -> {len(posts)} items")
            return posts, inst
        # items existed but none parsed (e.g. an instance's "not whitelisted"
        # page) — fall through to the next instance.
    return [], None


# ── dedup state ──────────────────────────────────────────────────────────────

def _state_file(screen: str) -> Path:
    return STATE_DIR / f"x_seen_{screen.lower()}.json"


def _load_seen(screen: str) -> list[str]:
    f = _state_file(screen)
    if not f.exists():
        return []
    try:
        return [str(x) for x in json.loads(f.read_text(encoding="utf-8"))]
    except Exception:  # noqa: BLE001 — a corrupt state file shouldn't kill the run
        return []


def _save_seen(screen: str, ids: list[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state_file(screen).write_text(
        json.dumps(ids[-SEEN_CAP:], ensure_ascii=False, indent=2), encoding="utf-8")


# ── card + orchestration ─────────────────────────────────────────────────────

def build_x_card(post: dict, screen_name: str) -> dict:
    """One embed = one card. The @handle leads as the author badge, the tweet's
    lead line is the clickable title, the full tweet is the body, the tweet's
    card image is the bottom image. embed.title forces full-width rendering."""
    badge = f"📈 @{screen_name} · X"
    return {"username": "BersamaAi", "embeds": [{
        "author": {"name": badge[:256]},
        "title": post["headline"][:256],
        "url": post["url"] or None,
        "description": post["text"][:4096],
        "color": BRAND_COLOR,
        "image": {"url": post["image"]} if post["image"] else None,
    }]}


def run_x_digest(*, dry_run: bool = False, alert_fn=None) -> list[str]:
    """Daily X digest. For each subscription, fetch the account's latest tweets,
    drop already-posted ones, and post each new tweet as a card. Returns one-line
    status strings. In dry-run, cards are printed (not posted) and no state is
    written — so a dry run is reproducible and needs no webhook."""
    print("\n=== x-digest run ===")
    results: list[str] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

    for screen, webhook_env, label in X_SUBSCRIPTIONS:
        print(f"\n--- @{screen} -> {label} ---")
        posts, inst = fetch_x_posts(screen)
        if inst is None:
            msg = (f"⚠️ **x-digest: every Nitter instance failed for @{screen}** — no X "
                   f"posts fetched. Rotate `NITTER_INSTANCES` in pipeline/x_digest.py "
                   f"(tried: {', '.join(NITTER_INSTANCES)}).")
            print(f"[x] {msg}")
            if alert_fn:
                alert_fn(msg, dry_run)
            results.append(f"X_FETCH_FAILED {screen}")
            continue

        seen = _load_seen(screen)
        seen_set = set(seen)
        first_run = not seen_set

        def _is_recent(p: dict) -> bool:
            return p["created_at"] is None or p["created_at"] >= cutoff

        fresh = [p for p in posts if p["id"] not in seen_set and _is_recent(p)]
        cap = FIRST_RUN_MAX if first_run else MAX_PER_RUN
        to_post = fresh[:cap]
        print(f"[x] @{screen}: {len(posts)} fetched via {inst}, {len(fresh)} fresh, "
              f"posting {len(to_post)} (first_run={first_run})")

        # Posting target — only resolved for a real (non-dry) run.
        wh = os.environ.get(webhook_env, "")
        if not dry_run:
            if not wh:
                msg = f"x-digest: `{webhook_env}` unset — @{screen} cards can't post"
                print(f"[x] {msg}")
                if alert_fn:
                    alert_fn(msg, dry_run)
                results.append(f"X_NO_WEBHOOK {screen}")
                continue
            if _is_staff_webhook(wh):
                msg = (f"x-digest: `{webhook_env}` is misconfigured → #staff-chat "
                       f"(@{screen}); cards skipped")
                print(f"[x] {msg}")
                if alert_fn:
                    alert_fn(msg, dry_run)
                results.append(f"X_WEBHOOK_IS_STAFF {screen}")
                continue

        posted = 0
        for p in to_post:
            payload = build_x_card(p, screen)
            if dry_run:
                print(f"\n[x DRY-RUN] -> {label}\n"
                      f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n")
                posted += 1
                results.append(f"X_DRY {screen} {p['headline'][:40]}")
                continue
            try:
                _post(wh, payload)
            except Exception as e:  # noqa: BLE001 — one failed post shouldn't abort the run
                if alert_fn:
                    alert_fn(f"x-digest post failed @{screen}: {p['headline'][:60]}: {e}",
                             dry_run)
                results.append(f"X_POST_FAILED {screen} {p['headline'][:40]}")
                continue
            posted += 1
            results.append(f"X_POSTED {screen} {p['headline'][:40]}")

        # Record EVERY fetched id as seen (never re-post an old tweet), even the
        # ones we didn't post this run (age-capped / over the cap). Dry-run keeps
        # its hands off state so repeated local tests are reproducible.
        if not dry_run:
            merged = seen + [i for i in (p["id"] for p in posts) if i not in seen_set]
            _save_seen(screen, merged)

        if posted == 0:
            results.append(f"X_NO_NEW {screen}")

    return results
