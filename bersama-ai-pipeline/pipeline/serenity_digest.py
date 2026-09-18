"""Serenity (@aleabitoreddit on X) post tracker → Discord digest → #serenity-x-posts.

Serenity is the AI-hardware/semis stock-picker whose reconstructed portfolio,
themes and daily commentary drive the Stocks Page in the sibling
subscription-agent project. This module streams HIS new X posts into
#serenity-x-posts as cards that clone that page's tagging: every post
auto-tagged with 1-4 topics (bullet chips) plus a pill row of every $TICKER
it mentions.

Source: **FxTwitter's public profile-timeline API** — a documented, keyless,
cookieless feed carrying the direct X link, full post text, cashtags, dates and
media in one response. It replaced trackserenity.com's signals.json after that
mirror stopped updating on 2026-09-02. The Bluesky account
(aleabitoreddit.bsky.social) was already rejected because it stopped updating
2026-07-21. FxTwitter is a third-party free service, so the x-digest staleness
doctrine still applies: a reachable feed whose newest post is too old raises a
staff alert instead of failing silently.

Tagging: the LLM (the pipeline's own neutral LLM_* config) picks 1-3 topics from the
19-area taxonomy ported from subscription-agent's `lib/serenity/topics.ts`,
unioned with the deterministic keyword rules (the same fallback the Stocks
Page uses when no LLM credential is present — here it also covers an LLM outage, and
the card still posts). No stance signal — deliberately dropped per owner.

Runtime: pipeline VM cron (01:07 UTC, staggered after the 01:00 EconomyApp
x-digest; uses the VM's OpenRouter API key). No LLM ⇒ keyword topics only. The
on-demand `/share` path never imports this module.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

from .llm import structured_call

# Generic posting / safety / health guards shared with the news engine, plus
# the x-digest plumbing this module deliberately reuses (HTTP, state, dates).
from .news import _post_resilient, _is_staff_webhook, _staff_alert, BRAND_COLOR
from .x_digest import (_http_get, _clean_multiline, _md_escape, _parse_date,
                       _load_seen, _save_seen)

TIMELINE_URL = "https://api.fxtwitter.com/2/profile/aleabitoreddit/statuses?count=100"
WEBHOOK_ENV = "DISCORD_SERENITY_X_POSTS_WEBHOOK_URL"
SCREEN = "Serenity"          # dedup key → state/x_seen_serenity.json (x-digest family)
BADGE = "📈 @Serenity · X"
LABEL = "#serenity-x-posts"

# He posts ~5.3/day (80 posts / 24 days, measured 2026-08-17); 12 covers a
# catch-up day so a burst doesn't get silently age-dropped over the cap.
MAX_PER_RUN = 12
MAX_AGE_DAYS = 7       # drop posts older than this (archive, not "new")
FIRST_RUN_MAX = 3      # bound day-1 volume so the first run isn't a wall of old cards
# (seen-id cap is x_digest.SEEN_CAP — _save_seen does the truncating)
# Largest observed gap between posts is 2.0 days → alert at 2×, well inside
# MAX_AGE_DAYS=7 (where posts start being dropped silently instead).
STALE_AFTER_DAYS = 4
LLM_TIMEOUT_S = 90     # per tagging call; the SDK default (600s ×3) could
LLM_MAX_RETRIES = 1    # stall a 12-post run for hours on a hung endpoint.
# Reasoning turns may think before answering; 90s keeps one bad tag from
# stalling the digest. The caller falls back to deterministic keyword topics.
TAG_MAX_OUTPUT_TOKENS = 4096

# ── topic taxonomy (ported 1:1 from subscription-agent lib/serenity/topics.ts) ─
# Canonical display order; the LLM is told to use these EXACT strings.
TOPICS = [
    "Mega-Cap Tech & Semiconductors",
    "Memory, Storage & Servers",
    "Optics / Silicon Photonics",
    "Advanced Packaging & Semicap",
    "Networking",
    "Data Center & Cloud",
    "Energy, Nuclear & Battery",
    "Rare Earth Materials",
    "Drones & Airbus",
    "Space & Satellites",
    "Robot / Humanoid",
    "Quantum",
    "Fintech & Crypto",
    "SaaS",
    "Medical / Healthcare",
    "Cybersecurity",
    "Meme",
    "Core / Keystone",
    "Macro & Market Analysis",
]
TOPIC_ORDER = {t: i for i, t in enumerate(TOPICS)}
MAX_TOPICS = 4  # chips on the card — the reference design shows four

# Deterministic fallback — the always-on, no-LLM topic tagger. Same rules as
# the Stocks Page's; a post can match several, first match per topic wins.
KEYWORD_RULES = [
    ("Optics / Silicon Photonics", re.compile(
        r"photon|optical|laser|\binp\b|co-packaged|\bcpo\b|transceiv|silicon photonic|waveguide|dfb|lumentum|aaoi|\blite\b", re.I)),
    ("Memory, Storage & Servers", re.compile(
        r"memory|\bdram\b|\bnand\b|\bhbm\b|micron|hynix|sandisk|\bsndk\b|\bmu\b|storage|ssd|server rack", re.I)),
    ("Advanced Packaging & Semicap", re.compile(
        r"packag|\bosat\b|amkor|towe|towa|glass.?core|interpos|\bxfab\b|tower semi|substrate|metrology|aehr", re.I)),
    ("Networking", re.compile(
        r"network|switch|router|\bbroadcom\b|\bmarvell\b|mrvl|astera|alab|ethernet|infiniband|400g|800g", re.I)),
    ("Data Center & Cloud", re.compile(
        r"data.?center|neocloud|hyperscaler|coreweave|\bnbis\b|cloud|aws|azure|\bgcp\b|stargate", re.I)),
    ("Mega-Cap Tech & Semiconductors", re.compile(
        r"nvidia|\bnvda\b|jensen|tesla|apple|\baapl\b|google|alphabet|\bgoogl\b|\bmeta\b|amd|\barm\b|tsm|\btsmc\b|intel|\bintc\b|jensen huang|gpu|cpu|accelerator|rubin|blackwell", re.I)),
    ("Energy, Nuclear & Battery", re.compile(
        r"power|grid|nuclear|reactor|smr|uranium|battery|ev battery|gallium nitride|\bgan\b|silicon carbid|\bsic\b|\bxlu\b|transformer|data.?center power|energy", re.I)),
    ("Rare Earth Materials", re.compile(
        r"rare earth|稀土|gallium|germanium|lithium|cobalt|nickel|graphite|tungsten|magnesium|materials|mining", re.I)),
    ("Drones & Airbus", re.compile(
        r"drone|uav|aerial|airbus|boeing|aerovironment|\bavav\b|defense platform|fighter|aircraft", re.I)),
    ("Space & Satellites", re.compile(
        r"space|satellite|rocket|rocket lab|\brklb\b|ast space|\basts\b|blue origin|starlink|orbit|spacex", re.I)),
    ("Robot / Humanoid", re.compile(
        r"robot|humanoid|agility|apptronik|boston dynamics|atlas|optimus|actuator|harmonic|\bfigure ai\b|1x|\btesla optimus\b", re.I)),
    ("Quantum", re.compile(
        r"quantum|qubit|ionq|\brgti\b|d-wave|rigetti|\bqs\b", re.I)),
    ("Fintech & Crypto", re.compile(
        r"crypto|bitcoin|\bbtc\b|ethereum|coinbase|\bcoin\b|fintech|\bhood\b|robinhood|circle|\bcrcl\b|stablecoin|defi", re.I)),
    ("SaaS", re.compile(
        r"\bsaas\b|cloud software|enterprise software|crm|snowflake|databricks|palantir|plt", re.I)),
    ("Medical / Healthcare", re.compile(
        r"biotech|pharma|fda|clinical|drug|medical|healthcare|medtech|diagnostic|medicube", re.I)),
    ("Cybersecurity", re.compile(
        r"cyber|security|crowdstrike|\bcrwd\b|palo alto|sentinelone|zero trust", re.I)),
    ("Meme", re.compile(
        r"\bmeme\b|gamestop|\bgme\b|amc|\bdog\b|doge|shib|wallstreetbets|wsb", re.I)),
    ("Macro & Market Analysis", re.compile(
        r"market|selloff|sell.?off|rally|\bdip\b|drawdown|crash|correction|\bfed\b|interest rate|\bcpi\b|inflation|recession|tariff|macro|sentiment|valuation|\bpe\b multiple|earnings|guidance|bull market|bear market|capitulation", re.I)),
]

# Every $CASHTAG in the text (e.g. "$NVDA, $LITE, $AAOI, $COHR, $AXTI") —
# uppercase-only by X cashtag convention; a leading letter stops "$5M" matches.
_TICKER_RE = re.compile(r"\$([A-Z][A-Z0-9]{1,9})\b")
# X's t.co shorteners: noise on the card — the bold lead line already links the
# post (fxtwitter text carries none anyway; this guards the mirror fallback).
_TCO_RE = re.compile(r"https?://t\.co/\w+")
# Post body shown on the card before cutting; longer → word-boundary cut plus an
# explicit read-the-full-post link (the mirror's own ~300-char truncation is the
# reason the full text is pulled from fxtwitter at all — see _fetch_fxtweet).
BODY_LIMIT = 1000


def _extract_tickers(text: str, cashtags: list) -> list[str]:
    """Every ticker the post mentions: the mirror's own `cashtags[]` ∪ a
    $-cashtag scan of the text (either can miss one the other catches), in
    order of first appearance, uppercase, deduped."""
    out: list[str] = []
    for tk in [*cashtags, *_TICKER_RE.findall(text or "")]:
        tk = str(tk).strip().upper().rstrip(".")
        if tk and tk not in out:
            out.append(tk)
    return out


def _keyword_topics(text: str) -> list[str]:
    """Deterministic topic tags — the no-LLM baseline (and the union partner
    for the LLM path), in canonical order. Never empty: defaults to Macro."""
    hits = {topic for topic, rx in KEYWORD_RULES if rx.search(text or "")}
    return _order_topics(hits)


def _order_topics(hits: set) -> list[str]:
    if not hits:
        hits = {"Macro & Market Analysis"}
    return sorted((t for t in hits if t in TOPIC_ORDER), key=TOPIC_ORDER.get)[:MAX_TOPICS]


# ── LLM topic tagger (via the pipeline's provider-neutral adapter) ─────────────────────

_SYSTEM_PROMPT = (
    "You tag investing posts by Serenity (@aleabitoreddit), an AI-hardware / semiconductor "
    "supply-chain stock picker. Return ONLY a JSON object: "
    '{"topics": [1-3 strings from the allowed topic list]}\n'
    "Allowed topics (use these EXACT strings):\n- " + "\n- ".join(TOPICS) + "\n"
    "Rules:\n"
    "- pick the 1-3 topics that best fit the post\n"
    '- add "Macro & Market Analysis" when the post is about the broad market, rates, '
    "sentiment, a selloff/rally, or valuations"
)

EMIT_TOPICS_TOOL = {
    "name": "emit_topics",
    "description": "Report the topic tags for one Serenity post.",
    "input_schema": {
        "type": "object",
        "properties": {
            "topics": {
                "type": "array",
                "items": {"type": "string", "enum": TOPICS},
                "minItems": 1,
                "maxItems": 3,
            },
        },
        "required": ["topics"],
    },
}


def _llm_topics(text: str, *, api_key: str, model: str, base_url: str) -> list[str]:
    """Ask the model for 1-3 topics. Tool-call-forced JSON (the news judge's
    proven idiom). Raises on any failure — the caller falls back to
    `_keyword_topics` and still posts the card."""
    data = structured_call(
        system=_SYSTEM_PROMPT,
        user=f'Post:\n"""{text}"""',
        tool=EMIT_TOPICS_TOOL,
        api_key=api_key, model=model, base_url=base_url,
        max_output_tokens=TAG_MAX_OUTPUT_TOKENS,
        timeout=LLM_TIMEOUT_S, max_retries=LLM_MAX_RETRIES,
    )
    return _order_topics({str(t) for t in (data.get("topics") or [])})


def _tag_topics(text: str, *, api_key: str, model: str, base_url: str) -> list[str]:
    """LLM topics ∪ keyword topics (the Stocks Page merges the same way),
    canonical order, capped for the card. LLM failure ⇒ keyword-only."""
    det = _keyword_topics(text)
    if not api_key:
        return det
    try:
        return _order_topics(set(_llm_topics(text, api_key=api_key, model=model, base_url=base_url)) | set(det))
    except Exception as e:  # noqa: BLE001 — tagging must never kill the card
        print(f"[serenity] LLM topic tag failed -> keyword fallback: {e}")
        return det


# ── feed parsing + images ────────────────────────────────────────────────────

def _parse_serenity_posts(payload: dict) -> list[dict]:
    """Pure FxTwitter-v2 parser: normalize own posts newest-first."""
    posts: list[dict] = []
    ids: set[str] = set()  # a duplicated id in one payload must not double-post
    for tw in (payload or {}).get("results") or []:
        if (not isinstance(tw, dict) or tw.get("type") != "status"
                or tw.get("reposted_by")):
            continue
        text = _clean_multiline(str(tw.get("text") or ""))
        pid = str(tw.get("id") or "").strip()
        if not text or not pid or pid in ids:
            continue
        ids.add(pid)
        facets = ((tw.get("raw_text") or {}).get("facets")) or []
        cashtags = [str(f.get("original") or "") for f in facets
                    if isinstance(f, dict) and f.get("type") == "symbol"
                    and f.get("original")]
        media = tw.get("media") or {}
        photos = media.get("photos") or []
        videos = media.get("videos") or []
        image = ""
        if photos:
            image = str(photos[0].get("url") or "").strip()
        elif videos:
            image = str(videos[0].get("thumbnail_url") or "").strip()
        if not image.startswith(("http://", "https://")):
            image = ""
        posts.append({
            "id": pid,
            "text": text,
            "url": str(tw.get("url") or f"https://x.com/aleabitoreddit/status/{pid}"),
            "cashtags": cashtags,
            "created_at": _parse_date(str(tw.get("created_at") or "")),
            "body": text,
            "image": image,
            "source_cut": False,
        })
    posts.sort(key=lambda p: p["created_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return posts


# ── card ─────────────────────────────────────────────────────────────────────

def build_serenity_card(post: dict, topics: list[str], tickers: list[str]) -> dict:
    """One embed = one post, cloning the reference design: @Serenity · X badge,
    the lead line as a bold hyperlink (title+link in one, x-digest style), the
    FULL post body (word-boundary cut + a bold read-more link when it exceeds
    BODY_LIMIT — a silent mid-sentence cut is never acceptable), then the topic
    pills and ticker pills (both inline-code chips so they read as tags, not
    body bullets), with the post's photo as the bottom image."""
    lead, _, rest = post["body"].partition("\n")
    lead, rest = lead.strip(), rest.strip("\n")
    head = (f"**[{_md_escape(lead)}]({post['url']})**" if lead and post["url"]
            else (f"**{lead}**" if lead else ""))
    # Cut honestly when the full FxTwitter text exceeds Discord's card budget.
    cut = post.get("source_cut", False)
    if len(rest) > BODY_LIMIT:
        rest = rest[:BODY_LIMIT].rsplit(" ", 1)[0].rstrip(",;:") + " …"
        cut = True
    if cut:
        rest += f"\n\n**[read the full post on X →]({post['url']})**"
    topic_row = "  ".join(f"`• {t}`" for t in topics)
    ticker_row = "  ".join(f"`${tk}`" for tk in tickers)
    desc = "\n\n".join(p for p in (head, rest, topic_row, ticker_row) if p)
    embed: dict = {
        "author": {"name": BADGE},
        "description": desc[:4096],
        "color": BRAND_COLOR,
    }
    if post.get("image"):
        embed["image"] = {"url": post["image"]}
    if post.get("created_at"):
        embed["timestamp"] = post["created_at"].isoformat()
    return {"username": "BersamaAi", "embeds": [embed]}


# ── orchestration ────────────────────────────────────────────────────────────

def run_serenity_digest(*, dry_run: bool = False, alert_fn=None,
                        api_key: str = "", model: str = "", base_url: str = "") -> list[str]:
    """Daily Serenity digest. Fetch the public timeline, drop already-posted/old posts,
    tag each new one (topics via LLM∪keywords, tickers via cashtags∪regex),
    attach its photo when it has one, and post the card. Returns one-line
    status strings. In dry-run, cards are printed (not posted) and no state is
    written — so a dry run is reproducible and needs no webhook."""
    print("\n=== serenity digest run ===")
    results: list[str] = []
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)

    r = _http_get(TIMELINE_URL)
    if r is None:
        _staff_alert(
            f"⚠️ **serenity digest: FxTwitter timeline fetch failed** — no posts fetched "
            f"(`{TIMELINE_URL}`).", dry_run)
        return ["SERENITY_FETCH_FAILED"]

    try:
        feed_payload = r.json()
        posts = _parse_serenity_posts(feed_payload)
    except Exception as e:  # noqa: BLE001 — malformed body → alert + skip
        _staff_alert(f"⚠️ **serenity digest: FxTwitter timeline parse failed**: {e}", dry_run)
        return ["SERENITY_PARSE_FAILED"]
    print(f"[serenity] {TIMELINE_URL} -> {len(posts)} own posts "
          f"(of {len(feed_payload.get('results') or [])} items)")

    # Third-party mirror ⇒ the x-digest staleness doctrine: a reachable feed
    # that stops advancing is the main failure mode, and it looks like a quiet
    # news day unless it gets its own alert.
    newest = next((p["created_at"] for p in posts if p["created_at"]), None)
    if not posts:
        _staff_alert(
            f"⚠️ **serenity digest: feed is reachable but EMPTY** (`{TIMELINE_URL}`). "
            f"Timeline unavailable, or the payload shape changed?", dry_run)
        results.append("SERENITY_EMPTY")
    elif newest is None:
        # Posts exist but NOT ONE timestamp parsed — a payload-shape change, which
        # otherwise disarms both the STALE check and the age filter silently.
        _staff_alert(
            f"⚠️ **serenity digest: {len(posts)} posts but no parsable created_at** "
            f"(`{TIMELINE_URL}`) — did FxTwitter change its date format?", dry_run)
        results.append("SERENITY_NO_DATES")
    elif (now - newest).days >= STALE_AFTER_DAYS:
        age = (now - newest).days
        _staff_alert(
            f"⚠️ **serenity digest: feed looks STALE** — newest Serenity post is "
            f"{age} days old (threshold {STALE_AFTER_DAYS}d). FxTwitter may have "
            f"stopped returning current posts.", dry_run)
        results.append(f"SERENITY_STALE {age}d")

    seen = _load_seen(SCREEN)
    seen_set = set(seen)
    # Day-1 (or a source switch): no numeric tweet id in state yet.
    first_run = not any(i.isdigit() for i in seen)

    def _is_recent(p: dict) -> bool:
        return p["created_at"] is None or p["created_at"] >= cutoff

    fresh = [p for p in posts if p["id"] not in seen_set and _is_recent(p)]
    cap = FIRST_RUN_MAX if first_run else MAX_PER_RUN
    to_post = fresh[:cap]
    print(f"[serenity] {len(fresh)} fresh, posting {len(to_post)} (first_run={first_run})")

    wh = os.environ.get(WEBHOOK_ENV, "")
    if not dry_run and not wh:
        print(f"[serenity] {WEBHOOK_ENV} unset — cards can't post")
        results.append("SERENITY_NO_WEBHOOK")
        return results
    if not dry_run and _is_staff_webhook(wh):
        _staff_alert(
            f"serenity digest: `{WEBHOOK_ENV}` is misconfigured → #staff-chat; "
            f"cards skipped", dry_run)
        results.append("SERENITY_WEBHOOK_IS_STAFF")
        return results

    posted = 0
    # Seen ids are committed AFTER EACH successful post, not once at the end:
    # a mid-run kill (VM reboot, Ctrl-C on a hung LLM call) must not re-post
    # cards already sent.
    saved: list[str] = list(seen)
    for p in to_post:
        p["body"] = _clean_multiline(_TCO_RE.sub("", p["body"]))
        topics = _tag_topics(p["body"], api_key=api_key, model=model, base_url=base_url)
        tickers = _extract_tickers(p["body"], p["cashtags"])
        payload = build_serenity_card(p, topics, tickers)
        if dry_run:
            print(f"\n[serenity DRY-RUN] -> {LABEL}\n"
                  f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n")
            posted += 1
            results.append(f"SERENITY_DRY {p['text'].splitlines()[0][:40]}")
            continue
        try:
            _post_resilient(wh, payload)
        except Exception as e:  # noqa: BLE001 — one failed post shouldn't abort the run
            if alert_fn:
                alert_fn(f"serenity digest post failed: {p['text'].splitlines()[0][:60]}: {e}", dry_run)
            results.append(f"SERENITY_POST_FAILED {p['text'].splitlines()[0][:40]}")
            continue
        posted += 1
        results.append(f"SERENITY_POSTED {p['text'].splitlines()[0][:40]}")
        if not dry_run:
            saved.append(p["id"])
            _save_seen(SCREEN, saved)

    # Record EVERY fetched id as seen (never re-post an old item), including
    # ones not posted this run (age-capped / over the cap). Dry-run keeps its
    # hands off state so repeated local tests are reproducible.
    if not dry_run:
        saved_set = set(saved)
        _save_seen(SCREEN, saved + [i for i in (p["id"] for p in posts)
                                    if i not in seen_set and i not in saved_set])

    if posted == 0:
        results.append("SERENITY_NO_NEW")
    return results
