"""X / Twitter account → Discord daily digest (e.g. @EconomyApp → #stock-invest).

X blocks ALL free ANONYMOUS scraping from datacenter IPs: every Nitter and
public RSS-Bridge instance fails from a server, and X's own "syndication" embed
endpoint — genuinely cookieless — only serves a popularity cache months out of
date. Authenticated RSSHub (a logged-in `auth_token` cookie) does work, but the
cookie expires, so that job rots silently every few weeks.

So the feed comes from a **Bluesky mirror of the X account**, read through the
public AT Protocol appview (`app.bsky.feed.getAuthorFeed`). That API is free,
keyless, cookieless and IP-agnostic — verified working from the GCP VM — so
there is nothing to expire and no RSSHub to host.

The mirror is addressed by its immutable **DID, never its handle**: a mirror
handle is just a domain (`economyapp.extwitter.link`, which doesn't even resolve
as a website) and can lapse, while the DID is permanent.

Runtime: anywhere. This no longer needs the VM (no `localhost:1200`), so it runs
as a VM cron OR a GitHub Actions job. The on-demand `/share` path never imports
this module. No LLM: the post text IS the card (faithful to the figures, no
translation, no summarizing).

The trade for losing the cookie is a third-party dependency: if the mirror
operator stops, the feed goes STALE rather than erroring — a worse failure mode,
because nothing raises. `STALE_AFTER_DAYS` closes that hole: a reachable feed
whose newest post is too old raises a staff alert instead of failing silently.

Two source shapes are supported per subscription:
  • `did=` → the Bluesky JSON API (default, preferred — carries images).
  • `rss=` → any RSS 2.0 / Atom feed, e.g. the account's own Substack
    (`appeconomyinsights.com/feed`) as the documented fallback. Note Bluesky's
    own RSS carries NO media, which is why the JSON API is preferred over it.
"""
from __future__ import annotations

import hashlib
import html as html_mod
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote

import requests

# Generic posting / safety / health guards shared with the news engine.
from .news import _post_resilient, _is_staff_webhook, _staff_alert, BRAND_COLOR

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
HEADERS = {"User-Agent": "Mozilla/5.0 (BersamaAi-x-digest/2.0; +bsky)"}

# Bluesky's public appview: no key, no cookie, no account, works from any IP.
# Override only to point at a different appview instance.
BSKY_API_DEFAULT = "https://public.api.bsky.app"
# Posts + the author's OWN threads; excludes replies to other people.
BSKY_FILTER = "posts_and_author_threads"
BSKY_LIMIT = 30
# Bluesky ids are `bsky:<rkey>`; the retired RSSHub source stored bare tweet
# snowflakes. The prefix keeps the two schemes apart in one state file so a
# source switch is detectable (see `_first_run`) instead of dumping a wall of
# cards on the changeover run.
BSKY_ID_PREFIX = "bsky:"


class XSource(NamedTuple):
    """One watched account. Exactly one source is used per subscription: `did`
    (a Bluesky mirror, preferred) wins over `rss` (any RSS 2.0 / Atom feed)."""
    screen: str
    webhook_env: str
    label: str
    did: str = ""
    rss: str = ""


X_SUBSCRIPTIONS = [
    XSource(
        screen="EconomyApp",
        webhook_env="DISCORD_STOCK_INVEST_WEBHOOK_URL",
        label="#stock-invest",
        # Mirror of https://x.com/EconomyApp (handle: economyapp.extwitter.link).
        # Verified 2026-07-25: 1032 posts since 2024-02-06, ~1.27/day, largest
        # gap 4 days, 96/100 posts carry an image or link-preview embed.
        did="did:plc:kio5ffqovakoioxtxbuat6mr",
        # Documented fallback if that mirror ever dies. NOT equivalent: it's the
        # Substack articles only, so the tweet-only bullet posts are lost.
        # rss="https://www.appeconomyinsights.com/feed",
    ),
]

MAX_PER_RUN = 8        # posts per account per run
MAX_AGE_DAYS = 7       # drop posts older than this (archive, not "new")
FIRST_RUN_MAX = 3      # bound day-1 volume so the first run isn't a wall of old cards
SEEN_CAP = 500         # keep the newest N post ids per account (insertion order)
REQUEST_TIMEOUT = 20
# Warn when a reachable feed's newest post is this old. Measured largest gap on
# @EconomyApp is 4 days, and MAX_AGE_DAYS=7 starts silently age-dropping posts,
# so 6 fires inside that window. A false alarm costs one staff-chat line; a dead
# mirror nobody notices costs the whole channel.
STALE_AFTER_DAYS = 6

_STATUS_ID = re.compile(r"/status(?:es)?/(\d+)")
# Nitter prefixed a reply's <title> with "R to @user:" — strip it on any source
# that still carries the marker so a thread continuation reads as its own card.
_REPLY_PREFIX = re.compile(r"^R to @\w+:\s*")
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _bsky_feed_url(did: str) -> str:
    base = os.environ.get("X_BSKY_API_BASE", BSKY_API_DEFAULT).rstrip("/")
    return (f"{base}/xrpc/app.bsky.feed.getAuthorFeed"
            f"?actor={did}&limit={BSKY_LIMIT}&filter={BSKY_FILTER}")


def _feed_url(sub: XSource) -> str:
    """The URL this subscription actually reads — for logs and staff alerts."""
    return _bsky_feed_url(sub.did) if sub.did else sub.rss


def _stale_after_days() -> int:
    try:
        return max(int(os.environ.get("X_STALE_DAYS", "") or STALE_AFTER_DAYS), 1)
    except ValueError:
        return STALE_AFTER_DAYS


# ── shared HTTP ──────────────────────────────────────────────────────────────

def _http_get(url: str) -> requests.Response | None:
    """GET with one 429 back-off retry. None on any failure (non-200/network),
    which the caller reports as "feed unreachable" rather than "feed empty"."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code == 429:
            wait = min(max(int(r.headers.get("Retry-After") or 15), 10), 60)
            print(f"[x] 429 — retrying in {wait}s")
            time.sleep(wait)
            r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            print(f"[x] {url} -> HTTP {r.status_code}")
            return None
        return r
    except Exception as e:  # noqa: BLE001 — network/DNS/TLS → alert + skip
        print(f"[x] {url} failed: {e}")
        return None


# ── text helpers ─────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    return html_mod.unescape(re.sub(r"\s+", " ", text or "")).strip()


def _clean_multiline(text: str) -> str:
    """Unescape + trim but KEEP newlines. These posts are bullet lists
    (`• Revenue +25% Y/Y to $16.1B`) and Discord renders the line breaks, so
    collapsing them the way `_clean` does would wreck the card."""
    t = html_mod.unescape(text or "")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return "\n".join(line.rstrip() for line in t.splitlines()).strip()


def _shorten(line: str, limit: int = 110) -> str:
    line = line.strip()
    return line if len(line) <= limit else line[:limit].rsplit(" ", 1)[0] + "…"


def _first_line(text: str) -> str:
    """Lead line of a post — Bluesky text carries real newlines, so the first
    non-empty line is the natural title (`$TSLA Tesla's Q2 FY26 visualized.`)."""
    for line in (text or "").splitlines():
        if line.strip():
            return _shorten(line)
    return ""


def _strip_tags(html_frag: str) -> str:
    return re.sub(r"<[^>]+>", " ", html_frag or "")


def _headline(text: str, desc_html: str) -> str:
    """Lead line of an RSS item — the first `<br>`-delimited segment of the HTML
    description when present (Nitter shape), else a word-boundary truncation of
    the full text near 110 chars. Becomes the embed title."""
    segs = re.split(r"<br\s*/?>", desc_html or "", maxsplit=1)
    lead = html_mod.unescape(re.sub(r"<[^>]+>", "", segs[0])).strip() if segs else ""
    return _shorten(lead) if lead else _shorten((text or "").strip())


# ── Bluesky JSON source (preferred) ──────────────────────────────────────────

def _bsky_media(embed, key: str) -> str:
    """Pull one field out of a post embed, recursing through `recordWithMedia`
    (a quote-post that also carries media nests the media one level down).
    `key="image"` → best picture; `key="uri"` → the linked article."""
    if not isinstance(embed, dict):
        return ""
    if embed.get("$type", "").startswith("app.bsky.embed.recordWithMedia"):
        return _bsky_media(embed.get("media") or {}, key)

    def _ok(u) -> str:
        u = (u or "").strip()
        return u if u.startswith(("http://", "https://")) else ""

    ext = embed.get("external") if isinstance(embed.get("external"), dict) else {}
    if key == "uri":
        return _ok(ext.get("uri"))
    for im in embed.get("images") or []:
        if isinstance(im, dict):
            u = _ok(im.get("fullsize")) or _ok(im.get("thumb"))
            if u:
                return u
    return _ok(ext.get("thumb"))


def _parse_bsky_posts(payload: dict, did: str) -> list[dict]:
    """Pure parser: a `getAuthorFeed` payload → normalized post dicts, newest
    first. Reposts are skipped — a repost is not the account's own post.
    Factored out so it can be exercised against a saved payload offline."""
    posts: list[dict] = []
    for item in (payload or {}).get("feed") or []:
        if not isinstance(item, dict) or item.get("reason"):
            continue
        post = item.get("post") or {}
        rec = post.get("record") or {}
        rkey = str(post.get("uri") or "").rsplit("/", 1)[-1]
        text = _clean_multiline(rec.get("text") or "")
        if not rkey or not text:
            continue
        embed = post.get("embed") or {}
        # Prefer the article the post links to (Substack etc.) as the card's
        # destination — more useful than the mirror post itself; fall back to
        # the Bluesky permalink, which always exists.
        posts.append({
            "id": BSKY_ID_PREFIX + rkey,
            "text": text,
            "headline": _first_line(text),
            "url": (_bsky_media(embed, "uri")
                    or f"https://bsky.app/profile/{did}/post/{rkey}"),
            "image": _bsky_media(embed, "image"),
            "created_at": _parse_date(str(rec.get("createdAt") or "")),
        })
    posts.sort(key=lambda p: p["created_at"] or _EPOCH, reverse=True)
    return posts


# ── RSS fallback source (source-agnostic: RSS 2.0 + Atom) ────────────────────

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
    (bare <img> in the description is the fallback path)."""
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


def _fallback_id(guid: str, link: str) -> str:
    """Stable id for a feed with no status snowflake — the Substack fallback,
    whose guids are article URLs. Without this those items would be dropped."""
    raw = (guid or link or "").strip()
    if not raw:
        return ""
    return "u:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _first_img_src(html_frag: str) -> str:
    m = re.search(r"<img[^>]+src=[\"']([^\"']+)[\"']", html_frag or "", re.IGNORECASE)
    return m.group(1) if m else ""


def _rewrite_image(src: str) -> str:
    """If this came from a Nitter proxy (`…/pic/<urlencoded x>`), rewrite it to
    pbs.twimg.com so Discord fetches Twitter's durable CDN. Any other URL
    (a media URL already on pbs.twimg.com, a Substack CDN URL…) passes through."""
    if not src:
        return ""
    m = re.search(r"/pic/(.+)$", src)
    if not m:
        return src
    return "https://pbs.twimg.com/" + unquote(m.group(1))


def _parse_posts(root, screen_name: str) -> list[dict]:
    """Pure parser: an RSS 2.0 / Atom root → normalized post dicts (newest first).
    Factored out of fetch_x_posts so it can be exercised against a synthetic feed
    without a live source."""
    items = [el for el in root.iter() if el.tag.split("}")[-1] in ("item", "entry")]
    posts: list[dict] = []
    for el in items:
        guid = _rss_field(el, {"guid", "id"})
        link = _rss_field(el, {"link"})
        tid = _tweet_id(guid, link) or _fallback_id(guid, link)
        desc_html = _rss_field(el, {"description", "summary", "content"})
        title = _REPLY_PREFIX.sub("", _clean(_rss_field(el, {"title"})))
        if not title:  # some feeds put the post only in the description body
            title = _REPLY_PREFIX.sub("", _clean(_strip_tags(desc_html)))
        if not tid or not title:
            continue
        img = _media_image(el) or _rewrite_image(_first_img_src(desc_html))
        if _STATUS_ID.search(link):
            post_url = link
        elif tid.isdigit():  # a real tweet snowflake — never a _fallback_id hash
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


def fetch_x_posts(sub: XSource) -> tuple[list[dict], str | None]:
    """Fetch one account's posts: the Bluesky JSON API when the subscription has
    a DID (preferred — carries images), else its RSS fallback feed. Returns
    (posts_newest_first, source). `source` is the feed URL on a successful fetch
    — even if it yielded 0 items — and None when the FETCH itself failed, so the
    caller can tell "feed empty" from "feed unreachable"."""
    url = _feed_url(sub)
    if not url:
        print(f"[x] @{sub.screen}: no did= and no rss= configured")
        return [], None
    r = _http_get(url)
    if r is None:
        return [], None
    try:
        posts = (_parse_bsky_posts(r.json(), sub.did) if sub.did
                 else _parse_posts(ET.fromstring(r.text), sub.screen))
    except Exception as e:  # noqa: BLE001 — malformed JSON/XML → alert + skip
        print(f"[x] {url} parse failed: {e}")
        return [], None
    print(f"[x] {sub.screen} via {url} -> {len(posts)} items")
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


def _first_run(seen: list[str], sub: XSource) -> bool:
    """True when state holds no id from THIS source's id scheme — a brand-new
    account, or a source switch (the retired RSSHub source stored bare tweet
    snowflakes; Bluesky stores `bsky:<rkey>`). Either way the batch is capped at
    FIRST_RUN_MAX, so changing source can't dump a week of cards at once."""
    mine = [i for i in seen
            if i.startswith(BSKY_ID_PREFIX) == bool(sub.did)]
    return not mine


# ── card + orchestration ─────────────────────────────────────────────────────

def _md_escape(text: str) -> str:
    """Escape the characters that would break a markdown link label."""
    return re.sub(r"([\[\]\\])", r"\\\1", text or "")


def build_x_card(post: dict, screen_name: str) -> dict:
    """One embed = one card. The @handle leads as the author badge and the post's
    own lead line becomes a **bold hyperlink** opening the body; media is the
    bottom image.

    There is deliberately NO `embed.title`: the post text already opens with that
    lead line, so a title rendered it twice — once as the heading and again as
    the first line of the description. Dropping the title and linking the lead
    line in place keeps one copy and keeps it clickable. (The retired title also
    forced full-width rendering; in practice ~96% of these posts carry an image,
    which sets the width anyway.)"""
    badge = f"📈 @{screen_name} · X"
    lead, _, rest = post["text"].partition("\n")
    lead, rest, url = lead.strip(), rest.strip("\n"), post["url"]
    if lead:
        head = f"**[{_md_escape(lead)}]({url})**" if url else f"**{lead}**"
    else:
        head = ""
    desc = "\n\n".join(part for part in (head, rest) if part)
    return {"username": "BersamaAi", "embeds": [{
        "author": {"name": badge[:256]},
        "description": desc[:4096],
        "color": BRAND_COLOR,
        "image": {"url": post["image"]} if post["image"] else None,
    }]}


def run_x_digest(*, dry_run: bool = False, alert_fn=None) -> list[str]:
    """Daily X digest. For each subscription, fetch the account's latest posts
    from its Bluesky mirror, drop already-posted ones, and post each new one as
    a card. Returns one-line status strings. In dry-run, cards are printed (not
    posted) and no state is written — so a dry run is reproducible and needs no
    webhook."""
    print("\n=== x-digest run ===")
    results: list[str] = []
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    stale_days = _stale_after_days()

    for sub in X_SUBSCRIPTIONS:
        print(f"\n--- @{sub.screen} -> {sub.label} ---")
        posts, src = fetch_x_posts(sub)
        if src is None:
            _staff_alert(
                f"⚠️ **x-digest: feed fetch failed for @{sub.screen}** — no posts "
                f"fetched (`{_feed_url(sub)}`). Is the Bluesky mirror still "
                f"published, and is the DID still correct?", dry_run)
            results.append(f"X_FETCH_FAILED {sub.screen}")
            continue

        # The mirror can die WITHOUT erroring — the feed just stops advancing.
        # That silent-staleness case is the main risk of a third-party source,
        # so it gets its own alert rather than looking like a quiet news day.
        newest = next((p["created_at"] for p in posts if p["created_at"]), None)
        if not posts:
            _staff_alert(
                f"⚠️ **x-digest: @{sub.screen} feed is reachable but EMPTY** "
                f"(`{src}`). Mirror deleted, or the DID changed?", dry_run)
            results.append(f"X_EMPTY {sub.screen}")
        elif newest is not None and (now - newest).days >= stale_days:
            age = (now - newest).days
            _staff_alert(
                f"⚠️ **x-digest: @{sub.screen} feed looks STALE** — newest post is "
                f"{age} days old (threshold {stale_days}d). The Bluesky mirror may "
                f"have stopped updating; consider the Substack fallback "
                f"(see `STOCK-DIGEST-CHALLENGES.md`).", dry_run)
            results.append(f"X_STALE {sub.screen} {age}d")

        seen = _load_seen(sub.screen)
        seen_set = set(seen)
        first_run = _first_run(seen, sub)

        def _is_recent(p: dict) -> bool:
            return p["created_at"] is None or p["created_at"] >= cutoff

        fresh = [p for p in posts if p["id"] not in seen_set and _is_recent(p)]
        cap = FIRST_RUN_MAX if first_run else MAX_PER_RUN
        to_post = fresh[:cap]
        print(f"[x] @{sub.screen}: {len(posts)} fetched, {len(fresh)} fresh, "
              f"posting {len(to_post)} (first_run={first_run})")

        wh = os.environ.get(sub.webhook_env, "")
        if not dry_run and not wh:
            print(f"[x] {sub.webhook_env} unset — @{sub.screen} cards can't post")
            results.append(f"X_NO_WEBHOOK {sub.screen}")
            continue
        if not dry_run and _is_staff_webhook(wh):
            _staff_alert(
                f"x-digest: `{sub.webhook_env}` is misconfigured → #staff-chat "
                f"(@{sub.screen}); cards skipped", dry_run)
            results.append(f"X_WEBHOOK_IS_STAFF {sub.screen}")
            continue

        posted = 0
        for p in to_post:
            payload = build_x_card(p, sub.screen)
            if dry_run:
                print(f"\n[x DRY-RUN] -> {sub.label}\n"
                      f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n")
                posted += 1
                results.append(f"X_DRY {sub.screen} {p['headline'][:40]}")
                continue
            try:
                _post_resilient(wh, payload)
            except Exception as e:  # noqa: BLE001 — one failed post shouldn't abort the run
                if alert_fn:
                    alert_fn(f"x-digest post failed @{sub.screen}: "
                             f"{p['headline'][:60]}: {e}", dry_run)
                results.append(f"X_POST_FAILED {sub.screen} {p['headline'][:40]}")
                continue
            posted += 1
            results.append(f"X_POSTED {sub.screen} {p['headline'][:40]}")

        # Record EVERY fetched id as seen (never re-post an old item), including
        # ones we didn't post this run (age-capped / over the cap). Dry-run keeps
        # its hands off state so repeated local tests are reproducible.
        if not dry_run:
            merged = seen + [i for i in (p["id"] for p in posts) if i not in seen_set]
            _save_seen(sub.screen, merged)

        if posted == 0:
            results.append(f"X_NO_NEW {sub.screen}")

    return results
