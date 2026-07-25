"""X / Twitter account → Discord daily digest (e.g. @EconomyApp → #stock-invest).

X blocks ALL free ANONYMOUS scraping from datacenter IPs — every Nitter and
public RSS-Bridge instance fails from a server (Nitter works only from a
residential IP). The one path that survives is AUTHENTICATED access, so the feed
comes from a self-hosted RSSHub instance on the GCP VM, configured with a logged-in
X account's `auth_token` cookie (env `TWITTER_AUTH_TOKEN` on the RSSHub instance
itself — RSSHub derives ct0/gt from it). RSSHub exposes
`/twitter/user/<screen>` as RSS 2.0; this module fetches it (localhost by
default), dedups by tweet id, and posts each new tweet as ONE verbatim card.

Runtime: a VM CRON, NOT GitHub Actions — RSSHub lives on the VM, so the fetch
must run there to reach `localhost:1200`. The on-demand `/share` path never
imports this module. No LLM: the tweet text IS the card (faithful to the
figures, no translation). The parser is source-agnostic (RSS 2.0 + Atom), so it
also works unchanged against RSS.app / any other feed if `X_RSSHUB_BASE` is
repointed later.
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

# RSSHub base. Default = the self-hosted instance on the VM (localhost:1200).
# Repoint at any RSSHub — or a per-screen RSS 2.0 feed — by setting X_RSSHUB_BASE.
RSSHUB_BASE_DEFAULT = "http://localhost:1200"

# (screen_name, webhook env var, channel label). Add more accounts here.
X_SUBSCRIPTIONS = [
    ("EconomyApp", "DISCORD_STOCK_INVEST_WEBHOOK_URL", "#stock-invest"),
]

MAX_PER_RUN = 8        # posts per account per run
MAX_AGE_DAYS = 7       # drop tweets older than this (archive, not "new")
FIRST_RUN_MAX = 3      # bound day-1 volume so the first run isn't a wall of old cards
SEEN_CAP = 500         # keep the newest N tweet ids per account (insertion order)
REQUEST_TIMEOUT = 20

_STATUS_ID = re.compile(r"/status(?:es)?/(\d+)")
# Nitter prefixes a reply's <title> with "R to @user:" — strip it on any source
# that still carries the marker so a thread continuation reads as its own card.
_REPLY_PREFIX = re.compile(r"^R to @\w+:\s*")
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _feed_url(screen: str) -> str:
    base = os.environ.get("X_RSSHUB_BASE", RSSHUB_BASE_DEFAULT).rstrip("/")
    return f"{base}/twitter/user/{screen}"


# ── RSS helpers (minimal + source-agnostic: RSS 2.0 + Atom) ──────────────────

def _rss_field(el, names: set[str]) -> str:
    """First non-empty text/href of a child whose local tag is in `names`."""
    for child in el:
        if child.tag.split("}")[-1] in names:
            t = (child.text or child.get("href") or "").strip()
            if t:
                return t
    return ""


def _media_image(el) -> str:
    """First usable image from <enclosure url=…> or <media:content url=…>
    (RSSHub emits media; bare <img> in the description is the fallback path)."""
    for child in el:
        if child.tag.split("}")[-1] in ("enclosure", "content"):
            u = (child.get("url") or "").strip()
            if u.startswith(("http://", "https://")) and not u.lower().split("?")[0].endswith(".svg"):
                return u
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
    """Bare snowflake from <guid>/<id> (or the trailing id of a status URL)."""
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
    """If this came from a Nitter proxy (`…/pic/<urlencoded x>`), rewrite it to
    pbs.twimg.com so Discord fetches Twitter's durable CDN. Any other URL (an
    RSSHub media URL already on pbs.twimg.com, an RSS.app CDN URL…) passes through."""
    if not src:
        return ""
    m = re.search(r"/pic/(.+)$", src)
    if not m:
        return src
    return "https://pbs.twimg.com/" + unquote(m.group(1))


def _headline(text: str, desc_html: str) -> str:
    """Lead line of the tweet — the first `<br>`-delimited segment of the HTML
    description when present (Nitter shape), else a word-boundary truncation of
    the full text near 110 chars. Becomes the embed title."""
    segs = re.split(r"<br\s*/?>", desc_html or "", maxsplit=1)
    lead = html_mod.unescape(re.sub(r"<[^>]+>", "", segs[0])).strip() if segs else ""
    if lead:
        return lead[:110]
    t = (text or "").strip()
    if len(t) <= 110:
        return t
    return t[:110].rsplit(" ", 1)[0] + "…"


def _clean(text: str) -> str:
    return html_mod.unescape(re.sub(r"\s+", " ", text or "")).strip()


def _strip_tags(html_frag: str) -> str:
    return re.sub(r"<[^>]+>", " ", html_frag or "")


def _parse_posts(root, screen_name: str) -> list[dict]:
    """Pure parser: an RSS 2.0 / Atom root → normalized post dicts (newest first).
    Factored out of fetch_x_posts so it can be exercised against a synthetic feed
    without a live RSSHub."""
    items = [el for el in root.iter() if el.tag.split("}")[-1] in ("item", "entry")]
    posts: list[dict] = []
    for el in items:
        guid = _rss_field(el, {"guid", "id"})
        link = _rss_field(el, {"link"})
        tid = _tweet_id(guid, link)
        desc_html = _rss_field(el, {"description", "summary", "content"})
        title = _REPLY_PREFIX.sub("", _clean(_rss_field(el, {"title"})))
        if not title:  # some feeds put the tweet only in the description body
            title = _REPLY_PREFIX.sub("", _clean(_strip_tags(desc_html)))
        if not tid or not title:
            continue
        img = _media_image(el) or _rewrite_image(_first_img_src(desc_html))
        if _STATUS_ID.search(link):
            post_url = link
        elif tid:
            post_url = f"https://x.com/{screen_name}/status/{tid}"
        else:
            post_url = link or guid
        posts.append({
            "id": tid,
            "text": title,
            "headline": _headline(title, desc_html),
            "url": post_url,
            "image": img,
            "created_at": _parse_date(_rss_field(el, {"pubDate", "published", "updated"})),
        })
    posts.sort(key=lambda p: p["created_at"] or _EPOCH, reverse=True)
    return posts


def fetch_x_posts(screen_name: str) -> tuple[list[dict], str | None]:
    """Fetch one account's feed from RSSHub. Returns (posts_newest_first, source).
    `source` is the feed URL on a successful fetch (even if 0 items), or None when
    the fetch itself failed (non-200 / network / parse) — caller raises a staff
    alert in that case."""
    url = _feed_url(screen_name)
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code == 429:
            wait = min(max(int(r.headers.get("Retry-After") or 15), 10), 60)
            print(f"[x] 429 — retrying in {wait}s")
            time.sleep(wait)
            r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            print(f"[x] {url} -> HTTP {r.status_code}")
            return [], None
        root = ET.fromstring(r.text)
    except Exception as e:  # noqa: BLE001 — RSSHub down / network / parse → alert + skip
        print(f"[x] {url} failed: {e}")
        return [], None
    posts = _parse_posts(root, screen_name)
    print(f"[x] {screen_name} via {url} -> {len(posts)} items")
    return posts, url


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
    media is the bottom image. embed.title forces full-width rendering."""
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
    """Daily X digest. For each subscription, fetch the account's latest tweets
    from RSSHub, drop already-posted ones, and post each new tweet as a card.
    Returns one-line status strings. In dry-run, cards are printed (not posted)
    and no state is written — so a dry run is reproducible and needs no webhook."""
    print("\n=== x-digest run ===")
    results: list[str] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

    for screen, webhook_env, label in X_SUBSCRIPTIONS:
        print(f"\n--- @{screen} -> {label} ---")
        posts, src = fetch_x_posts(screen)
        if src is None:
            _staff_alert(
                f"⚠️ **x-digest: RSSHub fetch failed for @{screen}** — no X posts fetched "
                f"(`{_feed_url(screen)}`). Is RSSHub running on the VM, and is the "
                f"`TWITTER_AUTH_TOKEN` cookie still valid?", dry_run)
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
        print(f"[x] @{screen}: {len(posts)} fetched, {len(fresh)} fresh, "
              f"posting {len(to_post)} (first_run={first_run})")

        wh = os.environ.get(webhook_env, "")
        if not dry_run and not wh:
            print(f"[x] {webhook_env} unset — @{screen} cards can't post")
            results.append(f"X_NO_WEBHOOK {screen}")
            continue
        if not dry_run and _is_staff_webhook(wh):
            _staff_alert(
                f"x-digest: `{webhook_env}` is misconfigured → #staff-chat (@{screen}); "
                f"cards skipped", dry_run)
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

        # Record EVERY fetched id as seen (never re-post an old tweet), including
        # ones we didn't post this run (age-capped / over the cap). Dry-run keeps
        # its hands off state so repeated local tests are reproducible.
        if not dry_run:
            merged = seen + [i for i in (p["id"] for p in posts) if i not in seen_set]
            _save_seen(screen, merged)

        if posted == 0:
            results.append(f"X_NO_NEW {screen}")

    return results
