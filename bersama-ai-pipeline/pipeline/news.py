"""Topic-routed trending-AI news → Discord channels.

Sources: Reddit (per-topic subs — OAuth JSON when REDDIT_CLIENT_ID/SECRET are set,
else public .rss; reddit.com 403s unauthenticated .json since ~2026-07) + Hacker
News + GitHub Trending (Search API) + HuggingFace trending + official blog RSS.
A GLM judge tags each candidate with a TOPIC + HEAT + card; each item routes to
its topic's channel webhook. Dedup (state/news_seen.json) drops already-posted
stories BEFORE quotas + the judge, so every judged slot is a fresh story.

Topics are configured in TOPICS below; only `live=True` topics gather + post
(all 7 are live today — trust the TOPICS table).

Heat bar = viral / popular (GitHub star velocity, Reddit upvotes/hot-rank, HN
points), NOT brand recognition — a fresh startup or community repo blowing up
qualifies. Health: a real (non-dry) run that posts 0 cards raises a warning in
#staff-chat via DISCORD_STAFF_CHAT_WEBHOOK_URL — silent runs are visible.
"""
from __future__ import annotations

import base64
import hashlib
import html as html_mod
import json
import os
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urljoin, unquote

import requests
from openai import OpenAI

from .github_trending import fetch_trending
from .stateutil import append_jsonl, POSTED_LOG, POSTED_LOG_SHARE
from .preferences import load_preferences, MIN_EVENTS

STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "news_seen.json"
HEADERS = {"User-Agent": "BersamaAi-news/1.0 (community bot)"}

HN_TOPN = 30
LOCAL_LIMIT = 50          # max candidates sent to the judge per run
MAX_POST_PER_RUN = 15     # global safety cap (rarely hit; per-topic cap below governs)
MAX_PER_TOPIC = 3         # max posts per channel per run — every channel gets a turn


# ── topics ───────────────────────────────────────────────────────────────────

@dataclass
class Topic:
    key: str
    channel: str
    webhook_env: str
    reddit_subs: list[str]
    github_keywords: list[str]
    github_min_stars: int = 200
    live: bool = False


TOPICS: list[Topic] = [
    Topic("coding", "#ai-dev-tools", "DISCORD_DEVTOOLS_WEBHOOK_URL",
          reddit_subs=["LocalLLaMA", "ClaudeAI", "OpenAI", "ChatGPTCoding"],
          github_keywords=["ai agent", "coding agent", "agentic", "llm", "mcp", "code review"],
          github_min_stars=150, live=True),
    Topic("creative_image", "#image-creation", "DISCORD_IMAGE_CREATION_WEBHOOK_URL",
          reddit_subs=["StableDiffusion"],
          github_keywords=["stable diffusion", "image generation", "flux", "comfyui"],
          github_min_stars=200, live=True),
    Topic("creative_video", "#video-creation-aigc-tvc", "DISCORD_VIDEO_CREATION_WEBHOOK_URL",
          reddit_subs=["aivideo"],
          github_keywords=["video generation", "text to video", "ai video editor", "sora", "veo"],
          github_min_stars=200, live=True),
    Topic("creative_voice", "#voice-studio", "DISCORD_VOICE_STUDIO_WEBHOOK_URL",
          reddit_subs=["SunoAI", "elevenlabs"],   # ElevenLabs = top AI voice company
          github_keywords=["text to speech", "voice clone", "tts", "suno", "elevenlabs", "music generation"],
          github_min_stars=150, live=True),
    Topic("research_study", "#education", "DISCORD_EDUCATION_WEBHOOK_URL",
          reddit_subs=["learnmachinelearning", "artificial"],   # r/ArtificialIntelligence 404s
          github_keywords=["learn ai", "ai course", "ml tutorial", "ai from scratch", "ai book"],
          github_min_stars=100, live=True),
    Topic("research_productivity", "#education", "DISCORD_EDUCATION_WEBHOOK_URL",
          reddit_subs=["Productivity", "ChatGPT"],
          github_keywords=["deep research", "research agent", "ai notes", "knowledge graph"],
          github_min_stars=150, live=True),
    Topic("finance", "#earn-money-with-ai", "DISCORD_FINANCE_WEBHOOK_URL",
          reddit_subs=["algotrading", "QuantFinance", "quant"],
          github_keywords=["fintech", "trading bot", "algorithmic trading", "quant", "ai finance"],
          github_min_stars=150, live=True),
]
TOPIC_BY_KEY = {t.key: t for t in TOPICS}
LIVE_TOPICS = [t for t in TOPICS if t.live]

# Renamed webhook env-vars: new name -> legacy name (still read as a fallback so a
# half-migrated .env keeps posting). Add future renames here.
_LEGACY_WEBHOOK = {"DISCORD_DEVTOOLS_WEBHOOK_URL": "DISCORD_NEWS_WEBHOOK_URL"}


def _topic_webhook(topic: Topic) -> str:
    """Resolve a topic's posting webhook from its `webhook_env`, falling back to the
    legacy var name if this topic's webhook was renamed and only the old one is set."""
    wh = os.environ.get(topic.webhook_env, "")
    if not wh:
        legacy = _LEGACY_WEBHOOK.get(topic.webhook_env)
        if legacy:
            wh = os.environ.get(legacy, "")
    return wh

# Broad local pre-filter (cheap, before the LLM) — keep anything AI-relevant.
AI_KEYWORDS = (
    "ai", "llm", "gpt", "chatgpt", "claude", "anthropic", "openai", "gemini",
    "deepmind", "grok", "xai", "kimi", "moonshot", "deepseek", "qwen", "glm",
    "z.ai", "zhipu", "mistral", "llama", "muse", "sora", "veo", "runway",
    "model", "agent", "agentic", "mcp", "coding", "copilot", "cursor", "devin",
    "replit", "hermes", "openclaw", "open source", "open-source", "api",
    "pricing", "benchmark", "swe-bench", "stable diffusion", "flux", "comfyui",
    "midjourney", "suno", "elevenlabs", "tts", "video gen", "image gen",
    "research", "study", "tutorial",
)


class NewsError(Exception):
    pass


@dataclass
class NewsItem:
    topic: str
    category: str
    headline: str
    body: str
    source_url: str
    heat_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── sources ──────────────────────────────────────────────────────────────────

def _looks_ai(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in AI_KEYWORDS)


_REDDIT_TOKEN = ""   # process-lifetime cache for the app-only OAuth token


def _reddit_oauth_token() -> str:
    """App-only OAuth token (client_credentials) when REDDIT_CLIENT_ID/SECRET are
    set — restores the full JSON API (real upvote counts). Empty string = no creds
    or token fetch failed; caller falls back to RSS."""
    global _REDDIT_TOKEN
    cid = os.environ.get("REDDIT_CLIENT_ID", "")
    sec = os.environ.get("REDDIT_CLIENT_SECRET", "")
    if not cid or not sec:
        return ""
    if _REDDIT_TOKEN:
        return _REDDIT_TOKEN
    try:
        r = requests.post("https://www.reddit.com/api/v1/access_token",
                          auth=(cid, sec), data={"grant_type": "client_credentials"},
                          headers=HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"[reddit] oauth token failed: HTTP {r.status_code} — falling back to RSS")
            return ""
        _REDDIT_TOKEN = r.json().get("access_token") or ""
    except Exception as e:  # noqa: BLE001
        print(f"[reddit] oauth token failed: {e} — falling back to RSS")
        return ""
    return _REDDIT_TOKEN


def _reddit_json_sub(sub: str, token: str) -> list[dict]:
    """One sub via the OAuth JSON API (full fidelity: upvote scores + thumbnails)."""
    url = f"https://oauth.reddit.com/r/{sub}/hot?limit=20"
    out = []
    try:
        r = requests.get(url, headers={**HEADERS, "Authorization": f"bearer {token}"}, timeout=15)
        if r.status_code != 200:
            print(f"[reddit] r/{sub} JSON -> HTTP {r.status_code}")
            return []
        children = (r.json().get("data") or {}).get("children") or []
    except Exception as e:  # noqa: BLE001
        print(f"[reddit] r/{sub} JSON failed: {e}")
        return []
    for c in children:
        d = c.get("data") or {}
        title = (d.get("title") or "").strip()
        if not title:
            continue
        ext = d.get("url") or ""
        perma = f"https://www.reddit.com{(d.get('permalink') or '')}"
        link = ext if ext and "reddit.com" not in ext else perma
        thumb = d.get("thumbnail") or ""
        out.append({
            "title": title, "url": link, "discussion": perma,
            "source": f"r/{sub}", "score": int(d.get("score") or 0),
            "snippet": (d.get("selftext") or "")[:300],
            "thumbnail": thumb if thumb.startswith("http") else "",
        })
    return out


def _reddit_rss_multi(subs: list[str]) -> list[dict]:
    """A group of subs via ONE public multireddit Atom feed (r/a+b+c/hot.rss) —
    the no-auth path that still works (the .json endpoints 403 unauthenticated),
    and one request per topic keeps us far under the ~10 req/min anonymous limit.
    Each entry's <category term> names its sub; per-sub hot rank is recovered as
    the occurrence index (a global hot sort preserves each sub's own order). The
    feed hides upvote counts, so `score` stays 0 and `rank` carries the heat; a
    link post's external target comes from the [link] anchor in the entry HTML."""
    import xml.etree.ElementTree as ET
    url = f"https://www.reddit.com/r/{'+'.join(subs)}/hot.rss?limit={min(20 * len(subs), 100)}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 429:   # burst-limited — honor Retry-After, retry once
            wait = min(max(int(r.headers.get("Retry-After") or 15), 15), 60)
            print(f"[reddit] r/{'+'.join(subs)} RSS -> 429, retrying in {wait}s")
            time.sleep(wait)
            r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"[reddit] r/{'+'.join(subs)} RSS -> HTTP {r.status_code}")
            return []
        root = ET.fromstring(r.content)
    except Exception as e:  # noqa: BLE001
        print(f"[reddit] r/{'+'.join(subs)} RSS failed: {e}")
        return []
    out, per_sub_rank = [], {}
    for el in root.iter():
        if el.tag.split("}")[-1] != "entry":
            continue
        title = perma = content = thumb = sub = ""
        for child in el:
            tag = child.tag.split("}")[-1]
            if tag == "title":
                title = (child.text or "").strip()
            elif tag == "link":
                perma = child.get("href") or ""
            elif tag == "content":
                content = child.text or ""
            elif tag == "thumbnail":
                thumb = child.get("url") or ""
            elif tag == "category":
                sub = child.get("term") or ""
        if not title:
            continue
        sub = sub or subs[0]
        rank = per_sub_rank[sub] = per_sub_rank.get(sub, 0) + 1
        lm = re.search(r'<a href="([^"]+)">\s*\[link\]', content)
        ext = html_mod.unescape(lm.group(1)) if lm else ""
        link = ext if ext and "reddit.com" not in ext else perma
        snippet = re.sub(r"<[^>]+>", " ", content)
        snippet = html_mod.unescape(re.sub(r"\s+", " ", snippet)).strip()
        snippet = re.sub(r"submitted by\s+/u/\S+.*$", "", snippet).strip()[:300]
        out.append({
            "title": title, "url": link, "discussion": perma,
            "source": f"r/{sub}", "score": 0, "rank": rank,
            "snippet": snippet,
            "thumbnail": thumb if thumb.startswith("http") else "",
        })
    return out


def fetch_reddit(subs: list[str]) -> list[dict]:
    token = _reddit_oauth_token()
    if not token:
        out = _reddit_rss_multi(subs)
        if not out:
            print(f"[reddit] r/{'+'.join(subs)} -> 0 items (RSS)")
        time.sleep(2.0)   # space topic-group requests
        return out
    out = []
    for sub in subs:
        items = _reddit_json_sub(sub, token) or _reddit_rss_multi([sub])
        if not items:
            print(f"[reddit] r/{sub} -> 0 items from every path")
        out += items
        time.sleep(0.5)   # OAuth allows 100 req/min
    return out


def fetch_hn(topn: int = HN_TOPN) -> list[dict]:
    out = []
    try:
        ids = requests.get("https://hacker-news.firebaseio.com/v0/topstories.json",
                           headers=HEADERS, timeout=15).json()[:topn]
    except Exception:  # noqa: BLE001
        return out
    for i in ids:
        try:
            it = requests.get(f"https://hacker-news.firebaseio.com/v0/item/{i}.json",
                              headers=HEADERS, timeout=10).json()
        except Exception:  # noqa: BLE001
            continue
        if not it or it.get("type") != "story":
            continue
        title = (it.get("title") or "").strip()
        if not title or not _looks_ai(title):
            continue
        out.append({
            "title": title,
            "url": it.get("url") or f"https://news.ycombinator.com/item?id={i}",
            "discussion": f"https://news.ycombinator.com/item?id={i}",
            "source": "HN", "score": int(it.get("score") or 0), "snippet": "",
        })
    return out


# HuggingFace is technical/LLM-narrow, so we reserve it for GENUINELY viral launches
# (Kimi K3 / GLM-5.2 / GPT-5.6 class). Models below this 7-day-like bar are skipped —
# GitHub carries the broad cross-field breadth; HF only flags the boombastic drops.
HF_VIRAL_LIKES = 200


def fetch_hf_trending() -> list[dict]:
    """HuggingFace models trending by 7-day likes — but ONLY the boombastic ones
    (>= HF_VIRAL_LIKES). Captures downloads + task so the card reads richly."""
    out = []
    try:
        r = requests.get("https://huggingface.co/api/models",
                         params={"sort": "likes7d", "direction": "-1", "limit": 30},
                         headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return out
        items = r.json() or []
    except Exception:  # noqa: BLE001
        return out
    for it in items:
        likes = int(it.get("likes") or 0)
        if likes < HF_VIRAL_LIKES:
            continue   # not boombastic enough — skip HF's long technical tail
        rid = it.get("id") or it.get("modelId") or ""
        if not rid:
            continue
        out.append({
            "title": rid, "url": f"https://huggingface.co/{rid}",
            "discussion": f"https://huggingface.co/{rid}", "source": "huggingface",
            "score": likes, "downloads": int(it.get("downloads") or 0),
            "snippet": (it.get("pipeline_tag") or "")[:120],
        })
    return out


# Official company blog RSS/Atom feeds — earliest signal for "Kimi K3 / Grok Build
# dropped", hours before Reddit. Add verified feed URLs here; 404/parse errors skip.
OFFICIAL_RSS = [
    "https://openai.com/news/rss.xml",
    "https://www.anthropic.com/news/rss.xml",
    "https://blog.google/technology/ai/rss/",
    "https://huggingface.co/blog/feed.xml",
]


def _rss_field(el, names: set[str]) -> str:
    """First non-empty text/href of a child whose local tag is in `names` (namespace-agnostic)."""
    for child in el:
        if child.tag.split("}")[-1] in names:
            t = (child.text or child.get("href") or "").strip()
            if t:
                return t
    return ""


def fetch_rss(feeds: list[str] = OFFICIAL_RSS) -> list[dict]:
    """Poll official blog RSS/Atom feeds (RSS 2.0 + Atom). Defensive: 404/parse → skip."""
    import xml.etree.ElementTree as ET
    out = []
    for feed in feeds:
        try:
            r = requests.get(feed, headers=URL_FETCH_HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.content)
        except Exception:  # noqa: BLE001
            continue
        for el in root.iter():
            if el.tag.split("}")[-1] not in ("item", "entry"):
                continue
            title = _rss_field(el, {"title"})
            link = _rss_field(el, {"link"})
            if not title:
                continue
            out.append({
                "title": title, "url": link, "discussion": link,
                "source": "official", "score": 0,
                "snippet": _rss_field(el, {"description", "summary", "content"})[:300],
            })
    return out


# Per-TOPIC quotas: each live topic contributes its own top Reddit+GitHub candidates
# to the pool, so coding (hotter/more numerous) can't crowd creative/research out
# before the judge sees them. Shared sources (HN/HF/RSS) get their own quotas.
PER_TOPIC_QUOTA = 8
HN_QUOTA = 8
HF_QUOTA = 3
RSS_QUOTA = 8
VELOCITY_THRESHOLD = 150   # GitHub repos gaining > this many stars/day bypass the score cut


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ── engagement loop: dynamic quotas (bandit actuator) ────────────────────────
# Active only when prefs.enabled (PREFS_ENABLED=true AND n_events>=MIN_EVENTS);
# otherwise callers use the static constants above (byte-identical to pre-loop).
# Each center equals today's static default so a NEUTRAL preference score changes
# nothing, and every transform keeps a floor so each arm stays pulled (ε-greedy
# exploration — a topic can recover when taste shifts). See ENGAGEMENT-LOOP-PLAN §4.

def _quota_for_topic(pref: float) -> int:
    """Per-topic candidate quota: center 8, floor 4, ceil 14. pref=0 → 8."""
    return _clamp(8 + round(2 * pref), 4, 14)


def _quota_for_shared(base: int, pref: float) -> int:
    """Shared-source quota (HN/HF/RSS): swings ±2 around its base, floor 1."""
    return _clamp(base + round(2 * pref), max(1, base - 2), base + 2)


def _cap_for_topic(pref: float) -> int:
    """Per-channel post cap (MAX_PER_TOPIC): center 3, range 1–4. pref=0 → 3."""
    return _clamp(3 + round(pref), 1, 4)


def _cand_key(c: dict) -> str:
    """Stable posted-story key for a candidate — the same key space `_keys()` writes
    to news_seen.json (sha1 of discussion-or-source URL), so gather can drop already
    -posted stories before they burn a quota slot or a judge token."""
    base = c.get("discussion") or c.get("url") or c.get("title") or ""
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def gather_candidates(prefs=None, seen: set | None = None, stats: dict | None = None) -> list[dict]:
    """Per-topic quotas: each live topic contributes its top Reddit+GitHub candidates
    (so every channel's domain reaches the judge — coding no longer crowds out creative),
    plus shared HN/HuggingFace/RSS for the judge to classify by topic.

    `seen` (posted-story keys from news_seen.json) is filtered out UP FRONT so every
    quota slot and judge token goes to a story that can actually post — this is what
    stops back-to-back runs re-judging the same pool into a wall of NEWS_DEDUPED.
    `stats` (optional dict) is filled with per-source gather counts + `skipped_seen`
    for run-health reporting."""
    token = os.environ.get("GITHUB_TOKEN", "")
    seen = seen or set()
    stats = stats if stats is not None else {}

    def _ai(c: dict) -> bool:
        return _looks_ai(c["title"]) or _looks_ai(c["snippet"])

    def _fresh(items: list[dict], source: str) -> list[dict]:
        kept = [c for c in items if _cand_key(c) not in seen]
        stats[source] = stats.get(source, 0) + len(items)
        stats["skipped_seen"] = stats.get("skipped_seen", 0) + len(items) - len(kept)
        return kept

    def _top(items: list[dict], n: int) -> list[dict]:
        return sorted([c for c in items if _ai(c)], key=lambda c: c.get("score", 0), reverse=True)[:n]

    cand: list[dict] = []
    on = bool(prefs and prefs.enabled)   # bandit actuator active only with enough signal
    quota_log: dict[str, int] = {}
    for t in LIVE_TOPICS:
        reddit_raw = _fresh(fetch_reddit(t.reddit_subs), "reddit")
        gh_raw = _fresh(fetch_trending(t.github_keywords, min_stars=t.github_min_stars, token=token), "github")
        q = _quota_for_topic(prefs.score("topic", t.key)) if on else PER_TOPIC_QUOTA
        quota_log[f"topic:{t.key}"] = q
        # Reddit's hot list is itself a popularity ranking, and the RSS fallback has
        # no upvote counts (score=0) — so reserve half the topic quota for Reddit in
        # hot order; pure score-sorting would silently starve it behind GitHub stars.
        reddit_ai = sorted([c for c in reddit_raw if _ai(c)],
                           key=lambda c: (-(c.get("score") or 0), c.get("rank") or 999))
        r_take = reddit_ai[: q // 2]
        top = r_take + _top(gh_raw + reddit_ai[len(r_take):], q - len(r_take))
        # velocity bypass: a fast-rising repo in this topic still reaches the judge
        in_top = {c.get("url") for c in top}
        top += [c for c in gh_raw
                if int(c.get("star_velocity") or 0) >= VELOCITY_THRESHOLD and c.get("url") not in in_top]
        cand += top
    # shared cross-topic sources — the judge tags these by topic
    hn_q = _quota_for_shared(HN_QUOTA, prefs.score("source", "HN")) if on else HN_QUOTA
    hf_q = _quota_for_shared(HF_QUOTA, prefs.score("source", "huggingface")) if on else HF_QUOTA
    rss_q = _quota_for_shared(RSS_QUOTA, prefs.score("source", "official")) if on else RSS_QUOTA
    quota_log.update({"source:HN": hn_q, "source:huggingface": hf_q, "source:official": rss_q})
    cand += (_top(_fresh(fetch_hn(), "HN"), hn_q)
             + _top(_fresh(fetch_hf_trending(), "huggingface"), hf_q)
             + _top(_fresh(fetch_rss(), "official"), rss_q))
    print("[sources] " + ", ".join(f"{k}={v}" for k, v in stats.items() if k != "skipped_seen")
          + f"; {stats.get('skipped_seen', 0)} already-posted skipped pre-judge")
    # Drift visibility (plan §4.3): always log which quotas governed this run.
    if on:
        print(f"[prefs] actuator ON (n_events={prefs.n_events}); dynamic quotas: {quota_log}")
    else:
        nevt = getattr(prefs, "n_events", 0)
        print(f"[prefs] actuator dormant (n_events={nevt} < {MIN_EVENTS} or PREFS_ENABLED=false); "
              f"static quotas — byte-identical to pre-loop.")

    picked_urls, dedup = set(), []
    for c in cand:
        k = c.get("url") or c.get("title")
        if k and k not in picked_urls:
            picked_urls.add(k)
            dedup.append(c)
    return dedup[:LOCAL_LIMIT]


# ── LLM judge ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are the news editor for BersamaAi's topic channels. You get AI candidates
from Reddit, Hacker News, and GitHub Trending. Pick only the HOTTEST items
(viral / popular — high GitHub stars, Reddit upvotes, or HN points; NOT brand
recognition — a fresh startup or community repo blowing up absolutely counts).
For each, assign a TOPIC and write a sober, no-hype card.

TOPICS (assign exactly one):
- coding — AI coding agents / agentic / dev tools / LLM releases / chat & assistants.
  ANY maker: bigco + startup + community/open-source; US + Chinese. e.g. Claude Code,
  Cursor, Cline, Aider, Windsurf, Copilot, Replit, pi coding, command code, Hermes,
  OpenClaw, Devin; ChatGPT/Claude/Gemini/Perplexity; Kimi, DeepSeek, Qwen, GLM,
  Meta Muse Spark, Grok Build / Grok 4.5.
- creative_image — image generation (Flux, SD, Midjourney, Ideogram).
- creative_video — video generation + AI editing (Sora, Veo, Runway, Kling, opencut, palmier-pro).
- creative_voice — voice / audio / TTS / music (ElevenLabs, Suno, voicebox).
- research_study — study / learning AI tools.
- research_productivity — research / productivity AI tools (deep-research, notes, knowledge work).
- finance — fintech / AI trading / quant / algorithmic trading / making money with AI
  (algotrading, trading bots, robo-advisors, AI side-income tools). The AI × money overlap.

For each item return:
- topic (one of the above)
- category: LAUNCH | RELEASE | PRICING | BENCHMARK | OPEN_SOURCE | DEAL | UPDATE
- headline (<= 110 chars; the news itself; no clickbait)
- body (1-2 sentences: what + why it matters to a busy developer/creator; grounded
  in the candidate text; never invent facts or numbers)
- source_url (pass through unchanged)
- heat_reason (one line on why it's hot, e.g. "12k stars in 3 days", "top of r/LocalLLaMA", "#1 on HN")

OFFICIAL-SOURCE items (source: official = a company blog announcement; huggingface = a new
model) are LAUNCH/RELEASE signals on their own — post them even with zero score; an official
announcement IS the heat. Don't bury them just because they have no upvotes yet.

Reddit items may carry hot_rank=#N (their position on the subreddit's hot list) instead of
an upvote score — the feed hides scores. A top-10 hot_rank on an active sub is a genuine
popularity signal; treat it like high upvotes, not like zero.

This is a TRENDING tracker, NOT a balance exercise. Pick purely by what's hottest/viral right
now. If the whole market is one topic this week (e.g. a run of coding-LLM launches), fill the
slots with that topic — do NOT pad with weaker items from other topics just for spread. The
candidate pool already guarantees you SEE every category each run; your only job is to post
what's genuinely hot, whatever category it lands in (and 0 from a quiet category is correct).

Be selective — only the genuinely hot. LANGUAGE RULE: write each card (headline + body)
in the SAME language as the source item — do NOT translate. English source → English card;
Chinese source → Chinese card; Malay → Malay; and so on. If nothing qualifies, items: [].
"""

EMIT_NEWS_TOOL = {
    "name": "emit_news",
    "description": "Emit the selected hot items, each tagged with its topic. Call exactly once.",
    "input_schema": {
        "type": "object",
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array", "maxItems": 15,
                "items": {
                    "type": "object",
                    "required": ["topic", "category", "headline", "body", "source_url"],
                    "properties": {
                        "topic": {"type": "string", "enum": [t.key for t in TOPICS]},
                        "category": {"type": "string",
                                     "enum": ["LAUNCH", "RELEASE", "PRICING", "BENCHMARK",
                                              "OPEN_SOURCE", "DEAL", "UPDATE"]},
                        "headline": {"type": "string", "maxLength": 200},
                        "body": {"type": "string", "maxLength": 600},
                        "source_url": {"type": "string"},
                        "heat_reason": {"type": "string", "maxLength": 160},
                    },
                },
            }
        },
    },
}


def _build_judge_user_message(candidates: list[dict]) -> str:
    lines = [f"CANDIDATES ({len(candidates)}):"]
    for i, c in enumerate(candidates, 1):
        heat = (f"hot_rank=#{c['rank']}" if c.get("rank") and not c.get("score")
                else f"score={c['score']}")
        lines.append(
            f"\n[{i}] source={c['source']} {heat}\n"
            f"title: {c['title']}\nurl: {c['url']}"
            + (f"\nexcerpt: {c['snippet']}" if c["snippet"] else "")
        )
    lines.append("\nPick the hottest. Call emit_news with each tagged by topic (or items: [] if none).")
    return "\n".join(lines)


def judge(candidates: list[dict], *, api_key: str, model: str, base_url: str,
          prefs_section: str = "") -> list[NewsItem]:
    if not api_key:
        raise NewsError("ZAI_API_KEY is missing — cannot judge news.")
    client = OpenAI(api_key=api_key, base_url=base_url)
    tool = {"type": "function", "function": {
        "name": EMIT_NEWS_TOOL["name"], "description": EMIT_NEWS_TOOL["description"],
        "parameters": EMIT_NEWS_TOOL["input_schema"],
    }}
    try:
        resp = client.chat.completions.create(
            model=model, max_completion_tokens=2048,  # Z.ai GLM ignores max_tokens
            messages=[{"role": "system", "content": SYSTEM_PROMPT + prefs_section},
                      {"role": "user", "content": _build_judge_user_message(candidates)}],
            tools=[tool], tool_choice="required",
        )
    except Exception as e:  # noqa: BLE001
        raise NewsError(f"GLM API call failed: {e}") from e

    tc = getattr(resp.choices[0].message, "tool_calls", None)
    if not tc:
        return []
    try:
        data = json.loads(tc[0].function.arguments or "{}")
    except (json.JSONDecodeError, TypeError) as e:
        raise NewsError(f"emit_news returned invalid JSON: {e}") from e

    out = []
    for raw in (data.get("items") or [])[:MAX_POST_PER_RUN]:
        topic = str(raw.get("topic", "")).strip()
        if topic not in TOPIC_BY_KEY:
            continue
        out.append(NewsItem(
            topic=topic,
            category=str(raw.get("category", "UPDATE")).strip(),
            headline=str(raw.get("headline", "")).strip(),
            body=str(raw.get("body", "")).strip(),
            source_url=str(raw.get("source_url", "")).strip(),
            heat_reason=str(raw.get("heat_reason", "")).strip(),
        ))
    return out


# ── dedup state ──────────────────────────────────────────────────────────────

def _load_seen() -> list[str]:
    """Posted-story keys in file order (insertion order going forward)."""
    if not STATE_FILE.exists():
        return []
    try:
        return list(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return []


def _save_seen(seen_list: list[str]) -> None:
    """Keep the newest 500 keys by INSERTION order. (The old `sorted(...)[-500:]`
    evicted lexicographically — random hashes, including fresh ones — which could
    resurface a just-posted story once the ledger passed 500 entries.)"""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(seen_list[-500:], ensure_ascii=False, indent=2),
                          encoding="utf-8")


def _keys(item: NewsItem, candidate_by_url: dict) -> set:
    """Dedup keys for an item: the source/discussion URL key PLUS a normalized-headline
    key — so the same story surfacing on two sources (e.g. Reddit + HN, different URLs)
    still dedupes instead of posting twice."""
    c = candidate_by_url.get(item.source_url)
    keys: set = set()
    url_base = (c["discussion"] if c else "") or item.source_url
    if url_base:
        keys.add(hashlib.sha1(url_base.encode("utf-8")).hexdigest()[:16])
    norm = re.sub(r"[^a-z0-9]", "", (item.headline or "").lower())
    if len(norm) >= 12:   # only dedup on a headline long enough to be meaningful
        keys.add("h" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16])
    return keys


# ── posting ──────────────────────────────────────────────────────────────────

CATEGORY_EMOJI = {"LAUNCH": "🚀", "RELEASE": "🆕", "PRICING": "💰", "BENCHMARK": "📊",
                  "OPEN_SOURCE": "🔓", "DEAL": "🎁", "UPDATE": "🔁"}
BRAND_COLOR = 0x5865F2


def _fmt(n: int) -> str:
    if n >= 1_000_000:
        s = f"{n / 1_000_000:.1f}M"
    elif n >= 1_000:
        s = f"{n / 1_000:.1f}k"
    else:
        return str(n)
    return s.replace(".0k", "k").replace(".0M", "M")   # 12.0k -> 12k, keep 1.2k


def _age_days(created_at) -> int | None:
    """Days since a repo's created_at (ISO), or None."""
    if not created_at:
        return None
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        return max((datetime.now(timezone.utc) - dt).days, 0)
    except Exception:  # noqa: BLE001
        return None


def _official_company(url: str) -> str:
    """Best-effort company name from an official-blog URL (for the heat label)."""
    from urllib.parse import urlparse
    host = (urlparse(url or "").netloc or "").lower()
    for frag, name in (("openai.com", "OpenAI"), ("anthropic.com", "Anthropic"),
                       ("blog.google", "Google"), ("huggingface.co", "HuggingFace")):
        if frag in host:
            return name
    return ""


def _metric(cand: dict | None) -> str:
    """Plain-English heat label for a card footer — tells a NON-technical reader, in
    human terms, why this item is trending/attractive right now. Built from the
    candidate's real signals (overwrites the LLM's flavor text in run_news). Every
    piece of jargon is glossed inline (stars=likes, forks=copies, HN=a tech forum)."""
    if not cand:
        return ""
    score = int(cand.get("score") or 0)
    src = cand.get("source") or ""

    if src == "github":
        forks = int(cand.get("forks") or 0)
        vel = int(cand.get("star_velocity") or 0)
        delta = int(cand.get("star_delta") or 0)
        age = _age_days(cand.get("created_at"))
        # "Exploding" only when we've measured real run-over-run growth, else "Trending"
        hook = "Exploding on GitHub" if (vel > 0 or delta > 0) else "Trending on GitHub"
        parts = [f"{_fmt(score)} stars (likes)"]
        if vel > 0:
            parts.append(f"+{_fmt(vel)} new stars/day")
        elif delta > 0:
            parts.append(f"+{_fmt(delta)} new stars since our last check")
        parts.append(f"{_fmt(forks)} forks (copies)")
        if age is not None:
            parts.append(f"only {age} {'day' if age == 1 else 'days'} old")
        return f"⭐ {hook}: " + ", ".join(parts)

    if src == "huggingface":
        dl = int(cand.get("downloads") or 0)
        base = f"🤗 {_fmt(score)} likes"
        if dl > 0:
            base += f" and {_fmt(dl)} downloads"
        return base + " on HuggingFace (the AI model hub)"

    if src == "official":
        company = _official_company(cand.get("url") or cand.get("discussion") or "")
        who = f"{company}'s " if company else "the company's "
        return f"📢 Straight from {who}official blog — the source itself, not secondhand news"

    if src == "HN":
        return f"📰 {_fmt(score)} upvotes on Hacker News — front page of a top tech-news forum"

    if src.startswith("r/"):
        if score > 0:   # OAuth JSON path: real upvote count
            return f"👍 {_fmt(score)} upvotes on {src} — one of the hottest posts there right now"
        rank = int(cand.get("rank") or 0)   # RSS path: hot-list position, no score
        if rank:
            return f"📈 Trending #{rank} on {src} — one of the hottest posts in that forum right now"
        return f"📈 Trending on {src} — one of the hottest posts in that forum right now"

    return ""


TOPIC_LABEL = {
    "coding": "Coding & Agents",
    "creative_image": "Creative · Image",
    "creative_video": "Creative · Video",
    "creative_voice": "Creative · Voice",
    "research_study": "Research · Study",
    "research_productivity": "Research · Productivity",
    "finance": "Finance · Earn with AI",
}


_OG_IMG = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)(?::secure_url)?["\']',
    re.IGNORECASE,
)
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
# Filenames/attributes that mark an <img> as decorative, not a content image.
_BAD_IMG = re.compile(
    r"(logo|icon|avatar|sprite|blank|placeholder|pixel|\b1x1\b|tracker|gravatar|"
    r"favicon|spinner|loader|btn|button|badge|tracking|beacon|noscript)",
    re.IGNORECASE,
)
_TRACKER_HOSTS = ("facebook.com/tr", "google-analytics.com", "googletagmanager",
                  "doubleclick.net", "bat.bing", "connect.facebook.net", "redditmedia", "taboola")


def _usable_image(url: str) -> bool:
    """A URL Discord can render as an embed thumbnail: http(s), not SVG (Discord
    doesn't render SVG), not a data/javascript URI."""
    if not url or not url.startswith(("http://", "https://")):
        return False
    return not url.lower().split("?", 1)[0].endswith(".svg")


def _resolve(src: str, base: str) -> str:
    """Resolve a (possibly relative, HTML-entity-encoded) image URL against the
    page base. Decodes &amp; → & (OpenAI's og:image ships encoded, which breaks
    Discord fetches) and decodes Next.js _next/image?url= proxy URLs."""
    if not src:
        return ""
    src = html_mod.unescape(src).strip()
    if src.startswith(("data:", "javascript:")):
        return ""
    nm = re.search(r"_next/image\?url=([^&\"']+)", src)
    if nm:  # Next.js image proxy → the real CDN url
        src = unquote(nm.group(1))
    try:
        return urljoin(base, src)
    except Exception:  # noqa: BLE001
        return ""


def _is_content_image(src: str, tag: str) -> bool:
    """Filter out tracking pixels, icons, logos, avatars, and tiny UI images so
    the 'first <img>' is a real hero/content image (e.g. skip the 1×1 pixel before
    the real hero on ads.openai.com)."""
    low = src.lower()
    if src.startswith(("data:", "javascript:")) or low.endswith(".svg"):
        return False
    if any(t in low for t in _TRACKER_HOSTS):
        return False
    for dim in ("width", "height"):
        dm = re.search(rf'\b{dim}\s*=\s*["\'](\d+)', tag, re.IGNORECASE)
        if dm and int(dm.group(1)) < 50:  # icon-sized
            return False
    return not (_BAD_IMG.search(low) or _BAD_IMG.search(tag.lower()))


def _extract_first_image(html_text: str, base_url: str) -> str:
    """Find the best thumbnail in server-rendered HTML, in priority order:
    1) og:image / twitter:image   2) <link rel=image_src>   3) JSON-LD "image"
    4) first plausible <img> (icons/logos/pixels skipped). SVGs are always skipped
    (Discord can't render them). Returns '' if the HTML has no usable image."""
    m = _OG_IMG.search(html_text)
    if m:
        u = _resolve(m.group(1) or m.group(2) or "", base_url)
        if _usable_image(u):
            return u
    lm = (re.search(r'<link\b[^>]*rel=["\']image_src["\'][^>]*href=["\']([^"\']+)["\']', html_text, re.I)
          or re.search(r'<link\b[^>]*href=["\']([^"\']+)["\'][^>]*rel=["\']image_src["\']', html_text, re.I))
    if lm:
        u = _resolve(lm.group(1), base_url)
        if _usable_image(u):
            return u
    for jm in re.finditer(r'"image"\s*:\s*\[?\s*"([^"]+)"', html_text):
        u = _resolve(jm.group(1), base_url)
        if _usable_image(u):
            return u
    for tag in _IMG_TAG.findall(html_text):
        sm = re.search(r'src\s*=\s*["\']([^"\']+)["\']', tag, re.IGNORECASE)
        if sm and _is_content_image(sm.group(1), tag):
            u = _resolve(sm.group(1), base_url)
            if _usable_image(u):
                return u
    return ""


def _microlink_image(url: str) -> str:
    """Fallback for JS-rendered pages (Threads, X) whose server HTML has NO image
    at all — microlink.io renders the page server-side and returns its real preview
    image. Best-effort; free tier (~50/day, ample for our volume). The URL is
    already public (a source the bot is posting about)."""
    try:
        r = requests.get("https://api.microlink.io/", params={"url": url}, timeout=25)
        if r.status_code != 200:
            return ""
        img = (r.json().get("data") or {}).get("image") or {}
        u = img.get("url") if isinstance(img, dict) else img
        return u if _usable_image(u or "") else ""
    except Exception:  # noqa: BLE001
        return ""


def _fetch_image(url: str) -> str:
    """Best-effort thumbnail for a card: the page's own HTML (og:image → first
    content <img>), then a rendered preview for JS-only sites (Threads/X). Strict
    rule — returns '' only when the source genuinely has no usable image."""
    if not url:
        return ""
    ua = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124 Safari/537.36"}
    try:
        r = requests.get(url, headers=ua, timeout=10)
        if r.status_code == 200:
            found = _extract_first_image(r.text, url)
            if found:
                return found
    except Exception:  # noqa: BLE001
        pass
    return _microlink_image(url)  # JS shell or fetch failed → rendered preview


def build_news_payload(item: NewsItem, image: str = "") -> dict:
    """One embed = one full-width card: the category·topic badge leads as the author
    (top), the headline is the clickable embed title (hyperlink, no raw URL), the
    body sits in the description, and the source preview is the bottom image.
    embed.title forces the card to 100% width (description-only embeds render narrow)."""
    emoji = CATEGORY_EMOJI.get(item.category, "📡")
    cat = item.category.replace("_", " ").title()
    topic_lbl = TOPIC_LABEL.get(item.topic, item.topic)
    badge = f"{emoji} {cat} · {topic_lbl}"
    heat = f"\n\n*🔥 {item.heat_reason}*" if item.heat_reason else ""
    return {"username": "BersamaAi", "embeds": [{
        "author": {"name": badge[:256]},
        "title": item.headline[:256],
        "url": item.source_url or None,
        "description": (f"**Why it matters**\n{item.body}{heat}")[:4096],
        "color": BRAND_COLOR,
        "image": {"url": image} if image else None,
    }]}



def _staff_alert(text: str, dry_run: bool = False) -> None:
    """Run-health warning → #staff-chat (DISCORD_STAFF_CHAT_WEBHOOK_URL). Best-effort:
    never raises; prints to the log when the webhook is unset or in dry-run."""
    wh = os.environ.get("DISCORD_STAFF_CHAT_WEBHOOK_URL", "")
    if dry_run or not wh:
        tag = "dry-run" if dry_run else "no DISCORD_STAFF_CHAT_WEBHOOK_URL"
        print(f"[staff-alert {tag}] {text}")
        return
    try:
        r = requests.post(wh, json={"username": "BersamaAi Health", "embeds": [{
            "description": text[:4096], "color": 0xE67E22,
        }]}, timeout=15)
        if r.status_code not in (200, 204):
            print(f"[staff-alert] failed: HTTP {r.status_code} {r.text[:200]}")
    except Exception as e:  # noqa: BLE001
        print(f"[staff-alert] failed: {e}")


def _is_staff_webhook(wh: str) -> bool:
    """True if `wh` is the #staff-chat webhook. A topic card must NEVER post to
    staff-chat (that channel is for health warnings only) — so both posting paths
    refuse when a topic's webhook secret was misconfigured to the staff-chat URL
    (e.g. DISCORD_FINANCE_WEBHOOK_URL accidentally given the staff-chat URL)."""
    staff = os.environ.get("DISCORD_STAFF_CHAT_WEBHOOK_URL", "")
    return bool(staff and wh) and wh.rstrip("/").lower() == staff.rstrip("/").lower()


def _post(webhook_url: str, payload: dict) -> dict | None:
    """POST a webhook payload with ?wait=true so Discord returns the created
    message object (HTTP 200 + {id, channel_id}) instead of 204 No Content.
    Returns that message dict (used to log telemetry), or None if Discord 204'd
    despite wait=true — the post still succeeded, but there's no id to sweep so
    the caller skips logging. Raises on any other status (caller handles)."""
    sep = "&" if "?" in webhook_url else "?"
    r = requests.post(webhook_url + sep + "wait=true", json=payload, timeout=15)
    if r.status_code == 200:
        try:
            return r.json()
        except ValueError:
            return None
    if r.status_code == 204:
        return None
    raise RuntimeError(f"{r.status_code} {r.text[:200]}")


def _log_posted(msg: dict, item: NewsItem, cand: dict | None, topic: Topic,
                origin: str = "auto", log_path: Path = POSTED_LOG) -> None:
    """Append one row per posted card to state/posted_log.jsonl — the engagement
    sweep's list of messages to read reactions from. Telemetry collects
    unconditionally (even while the actuator is dormant) so the model has data
    the day the owner flips PREFS_ENABLED on. `origin` = "auto" (news digest) or
    "share" (owner hand-picked via /share) so the engine's strongest taste signal
    can be told apart / weighted later; preferences ignores unknown fields until
    you opt in, so adding it changes nothing today."""
    from datetime import datetime, timezone
    append_jsonl(log_path, {
        "message_id": str(msg.get("id", "")),
        "channel_id": str(msg.get("channel_id", "")),
        "channel": topic.channel,
        "topic": item.topic,
        "category": item.category,
        "source": (cand or {}).get("source", ""),
        "source_url": item.source_url,
        "headline": item.headline,
        "score": int((cand or {}).get("score") or 0),
        "star_velocity": int((cand or {}).get("star_velocity") or 0),
        "origin": origin,
        "posted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


def _push_share_shard() -> None:
    """Best-effort: push the VM's /share telemetry shard (posted_log_share.jsonl) to the
    repo so the GitHub Actions engagement sweep + preferences see /share cards. The VM is
    the SOLE writer of this file → no merge conflicts with GH Actions' posted_log.jsonl.
    Fire-and-forget on a daemon thread; the file is durable on disk, so a failed push is
    retried on the next /share (the whole file is re-pushed, so no row is ever lost)."""
    import threading
    def _run() -> None:
        token = os.environ.get("GITHUB_TOKEN", "")
        repo = os.environ.get("GITHUB_REPOSITORY", "pmgwee/BersamaAi-community")
        if not token:
            print("[share-shard] no GITHUB_TOKEN — /share telemetry stays local; won't reach the sweep")
            return
        if not POSTED_LOG_SHARE.exists():
            return
        content = POSTED_LOG_SHARE.read_bytes()
        url = f"https://api.github.com/repos/{repo}/contents/state/posted_log_share.jsonl"
        h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        try:
            for _ in range(3):   # retry transient failures + sha mismatches
                sha = None
                g = requests.get(url, headers=h, timeout=15)
                if g.status_code == 200:
                    sha = g.json().get("sha")
                p = requests.put(url, headers=h, timeout=20, json={
                    "message": "chore: /share telemetry shard",
                    "content": base64.b64encode(content).decode(),
                    "sha": sha,           # None on first create; current sha on update
                    "branch": "main",
                })
                if p.status_code in (200, 201):
                    print("[share-shard] synced to repo")
                    return
                if p.status_code != 409:   # 409 = sha mismatch → re-fetch sha + retry
                    print(f"[share-shard] push failed: {p.status_code} {p.text[:160]}")
                    return
        except Exception as e:  # noqa: BLE001
            print(f"[share-shard] push failed: {e}")
    threading.Thread(target=_run, daemon=True).start()


# ── share (Threads / link human-in-the-loop) ─────────────────────────────────
# The owner is the taste algorithm for social sources (Threads/X) the engine
# can't/shouldn't auto-scrape. This turns ONE chosen public URL into a posted card:
# fetch og: meta -> GLM writes a topic-tagged card -> post to that topic's channel.

URL_FETCH_HEADERS = {"User-Agent": "Mozilla/5.0 (BersamaAi-news/1.0; +share)"}


def _meta_content(html: str, prop: str) -> str:
    """Value of <meta name/property=prop content=...> (attribute-order-insensitive)."""
    pat = re.compile(rf"<meta[^>]+(?:name|property)=[\"']({re.escape(prop)})[\"'][^>]*content=[\"']([^\"']*)",
                     re.IGNORECASE)
    m = pat.search(html)
    if m:
        return m.group(2)
    pat2 = re.compile(rf"<meta[^>]+content=[\"']([^\"']*)[\"'][^>]*(?:name|property)=[\"']({re.escape(prop)})[\"']",
                      re.IGNORECASE)
    m = pat2.search(html)
    return m.group(1) if m else ""


def _html_title(html: str) -> str:
    m = re.search(r"<title[^>]*>([^<]*)</title>", html, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def fetch_url_meta(url: str) -> dict:
    """Fetch a public URL; extract {title, description, image}. The image uses the
    robust extractor (og:image → first content <img>) with a microlink fallback for
    JS-only sites like Threads, so a shared post always gets a thumbnail when one
    exists. Title/description stay og:-based."""
    try:
        r = requests.get(url, headers=URL_FETCH_HEADERS, timeout=15)
        if r.status_code != 200:
            return {}
        html = r.text
    except Exception:  # noqa: BLE001
        return {}
    image = _extract_first_image(html, url) or _microlink_image(url)
    return {
        "title": _meta_content(html, "og:title") or _html_title(html),
        "description": _meta_content(html, "og:description") or _meta_content(html, "description"),
        "image": image,
        "url": url,
    }


SINGLE_CARD_PROMPT = """\
You are the BersamaAi news editor. The owner shared ONE item (a Threads/X post,
article, or product/repo link) they judged worth the community's attention. Write
ONE news card — sober, no hype, grounded strictly in the given text.

If a VIDEO TRANSCRIPT is included, base the body on what the video actually shows or
demonstrates — the caption is often promotional ("comment X to get Y"), so trust the
transcript for the real content and pick the topic from what the video is about.

Assign exactly one TOPIC:
- coding — AI coding agents / agentic / dev tools / LLM / chat & assistants
- creative_image — image generation
- creative_video — video generation + AI editing
- creative_voice — voice / audio / TTS / music
- research_study — study / learning AI tools
- research_productivity — research / productivity AI tools
- finance — fintech / AI trading / quant / making money with AI

Return: topic, category (LAUNCH|RELEASE|PRICING|BENCHMARK|OPEN_SOURCE|DEAL|UPDATE),
headline (<=110 chars, the news itself), body (1-2 sentences: what + why it matters),
source_url (pass through unchanged). LANGUAGE: write the card (headline + body) in the
SAME language as the source item — do NOT translate (Chinese source → Chinese card, etc.).
"""

EMIT_ONE_TOOL = {
    "name": "emit_card",
    "description": "Emit the one news card for the shared item. Call exactly once.",
    "input_schema": {
        "type": "object",
        "required": ["topic", "category", "headline", "body", "source_url"],
        "properties": {
            "topic": {"type": "string", "enum": [t.key for t in TOPICS]},
            "category": {"type": "string",
                         "enum": ["LAUNCH", "RELEASE", "PRICING", "BENCHMARK",
                                  "OPEN_SOURCE", "DEAL", "UPDATE"]},
            "headline": {"type": "string", "maxLength": 200},
            "body": {"type": "string", "maxLength": 600},
            "source_url": {"type": "string"},
        },
    },
}


def post_url_as_news(url: str, *, api_key: str, model: str, base_url: str,
                     dry_run: bool = False, alert_fn=None) -> str:
    """Threads/link HITL: fetch a public URL -> GLM writes a topic-tagged card -> post."""
    if not api_key:
        return "SHARE_NO_API_KEY"
    from . import social  # lazy: yt-dlp is a heavier import
    if social.is_social_video_url(url):
        # IG reels / XHS / TikTok: JS-shell pages → yt-dlp gets the real metadata +
        # cover frame, and ASR transcribes the audio so the card is accurate (the
        # caption is often engagement-bait, not a description of the video).
        meta = social.fetch_social_video(url)
    else:
        meta = fetch_url_meta(url)
    if not meta.get("title") and not meta.get("transcript"):
        print(f"[share] could not fetch content for {url}")
        return "SHARE_FETCH_FAILED"
    client = OpenAI(api_key=api_key, base_url=base_url)
    tool = {"type": "function", "function": {
        "name": EMIT_ONE_TOOL["name"], "description": EMIT_ONE_TOOL["description"],
        "parameters": EMIT_ONE_TOOL["input_schema"],
    }}
    parts = [f"title: {meta.get('title', '')}", f"description: {meta.get('description', '')}"]
    if meta.get("transcript"):
        # Prefer what the video actually says over the (often promotional) caption.
        parts.append(
            f"video transcript (what is actually said/shown — trust this over the caption, "
            f"which may be promotional): {meta['transcript'][:1500]}"
        )
    parts.append(f"url: {url}")
    user_msg = "\n".join(parts)
    try:
        resp = client.chat.completions.create(
            model=model, max_completion_tokens=1024,
            messages=[{"role": "system", "content": SINGLE_CARD_PROMPT},
                      {"role": "user", "content": user_msg}],
            tools=[tool], tool_choice="required",
        )
    except Exception as e:  # noqa: BLE001
        return f"SHARE_LLM_FAILED {e}"
    tc = getattr(resp.choices[0].message, "tool_calls", None)
    if not tc:
        return "SHARE_LLM_EMPTY"
    try:
        data = json.loads(tc[0].function.arguments or "{}")
    except (json.JSONDecodeError, TypeError):
        return "SHARE_LLM_BADJSON"
    topic = str(data.get("topic", "")).strip()
    if topic not in TOPIC_BY_KEY:
        return f"SHARE_BAD_TOPIC {topic}"
    item = NewsItem(
        topic=topic,
        category=str(data.get("category", "UPDATE")).strip(),
        headline=str(data.get("headline", "")).strip(),
        body=str(data.get("body", "")).strip(),
        source_url=str(data.get("source_url", url)).strip() or url,
        heat_reason="📣 shared by the owner",
    )
    t = TOPIC_BY_KEY[topic]
    wh = _topic_webhook(t)
    if not wh:
        return f"SHARE_NO_WEBHOOK {topic}"
    if _is_staff_webhook(wh):
        if alert_fn:
            alert_fn(f"/share topic {topic} webhook is misconfigured -> #staff-chat "
                     f"(secret {t.webhook_env}); card not posted", dry_run)
        return f"SHARE_WEBHOOK_IS_STAFF {topic}"
    payload = build_news_payload(item, image=meta.get("image", ""))
    if dry_run:
        print(f"\n[share DRY-RUN] -> {t.channel}\n{payload}\n")
        return f"SHARED_DRY {topic} {item.headline[:40]}"
    try:
        msg = _post(wh, payload)
    except Exception as e:  # noqa: BLE001
        if alert_fn:
            alert_fn(f"share post failed: {item.headline[:60]}: {e}", dry_run)
        return f"SHARE_POST_FAILED {e}"
    # Log owner-curated cards so their reactions/replies feed the engagement loop
    # (the engine's strongest taste signal). cand=None: /share has no scrape row,
    # so source/score/star_velocity write as ""/0 — _log_posted null-guards it.
    if msg and msg.get("id"):
        _log_posted(msg, item, cand=None, topic=t, origin="share", log_path=POSTED_LOG_SHARE)
        _push_share_shard()   # best-effort: sync the VM shard to the repo so the GH Actions sweep sees it
    return f"SHARED {topic} {item.headline[:40]}"


# ── orchestration ────────────────────────────────────────────────────────────

def run_news(*, dry_run: bool, stub: bool,
             api_key: str, model: str, base_url: str,
             webhook_url: str = "", alert_fn=None) -> list[str]:
    """Full news run. Returns a list of one-line status strings."""
    print("\n=== news run ===")
    if not LIVE_TOPICS:
        return ["NEWS_NO_LIVE_TOPICS"]

    prefs = load_preferences()
    seen_list = _load_seen()
    seen = set(seen_list)
    stats: dict = {}
    candidates = gather_candidates(prefs, seen=seen, stats=stats)
    skipped = stats.get("skipped_seen", 0)
    print(f"gathered {len(candidates)} fresh candidates ({skipped} already-posted skipped) "
          "across live topic(s): " + ", ".join(t.key for t in LIVE_TOPICS))
    if not candidates:
        _staff_alert(
            "⚠️ **News digest: 0 fresh candidates this run.**\n"
            f"Sources returned {sum(v for k, v in stats.items() if k != 'skipped_seen')} items, "
            f"{skipped} already posted before. If a source shows 0 in the run log "
            "(`[sources] …` / `[reddit] …` lines), it may be down or blocked.", dry_run)
        return ["NEWS_NO_CANDIDATES"]

    if stub:
        items = [NewsItem(LIVE_TOPICS[0].key, "LAUNCH", "[STUB] a hot item",
                          "Canned item for local testing (no API key).",
                          candidates[0]["url"], "stub")]
    else:
        try:
            items = judge(candidates, api_key=api_key, model=model, base_url=base_url,
                          prefs_section=prefs.profile_section)
        except NewsError as e:
            if alert_fn:
                alert_fn(f"news judge failed: {e}", dry_run)
            return [f"NEWS_JUDGE_FAILED {e}"]

    if not items:
        print("judge returned no items worth posting")
        _staff_alert(
            "ℹ️ **News digest posted 0 cards** — the judge found nothing hot enough among "
            f"{len(candidates)} fresh candidates ({skipped} already-posted were skipped). "
            "A quiet market window is normal once in a while; several in a row is not.", dry_run)
        return ["NEWS_NOTHING_TO_POST"]

    by_url = {c["url"]: c for c in candidates}
    posted, results = 0, []
    per_topic: dict = {}
    staff_misconfig: set = set()   # topics whose webhook wrongly == staff-chat (alert once)
    for item in items:
        keys = _keys(item, by_url)
        if seen & keys:   # any key already seen -> same story already posted this run
            results.append(f"NEWS_DEDUPED {item.headline[:40]}")
            continue
        topic = TOPIC_BY_KEY[item.topic]
        # per-channel cap: every channel gets up to its cap posts (no one channel
        # starves the others); once a channel is full it's skipped for the rest of
        # the run. Cap is dynamic when the actuator is on, else MAX_PER_TOPIC.
        cap = _cap_for_topic(prefs.score("topic", topic.key)) if prefs.enabled else MAX_PER_TOPIC
        if per_topic.get(topic.key, 0) >= cap:
            results.append(f"NEWS_TOPIC_CAPPED {topic.key}")
            continue
        # webhook: the topic's env var, else the passed fallback for live topics
        wh = _topic_webhook(topic) or (webhook_url if topic.live else "")
        if not wh:
            results.append(f"NEWS_NO_WEBHOOK {item.topic} {item.headline[:40]}")
            continue
        if _is_staff_webhook(wh):
            # A topic webhook must never resolve to #staff-chat. Its webhook secret is
            # misconfigured (points at the staff-chat URL) — skip + alert once per topic.
            results.append(f"NEWS_WEBHOOK_IS_STAFF {topic.key} {topic.webhook_env}")
            if topic.key not in staff_misconfig:
                staff_misconfig.add(topic.key)
                _staff_alert(
                    f"🚫 **Topic `{topic.key}` webhook is misconfigured → points at #staff-chat** "
                    f"(secret `{topic.webhook_env}` == `DISCORD_STAFF_CHAT_WEBHOOK_URL`). Cards "
                    f"skipped to avoid polluting staff-chat. Fix the `{topic.webhook_env}` "
                    f"GitHub secret to the **{topic.channel}** webhook URL.", dry_run)
            continue
        cand = by_url.get(item.source_url)
        metric = _metric(cand)
        if metric:
            item.heat_reason = metric  # real stars/upvotes/points, not LLM flavor text
        img = _fetch_image(item.source_url) or (cand or {}).get("thumbnail", "")
        payload = build_news_payload(item, image=img)
        if dry_run:
            print(f"\n[discord DRY-RUN] -> {topic.channel}\n{payload}\n")
        else:
            try:
                msg = _post(wh, payload)
            except Exception as e:  # noqa: BLE001
                if alert_fn:
                    alert_fn(f"news post failed: {item.headline[:60]}: {e}", dry_run)
                results.append(f"NEWS_POST_FAILED {item.headline[:40]}")
                continue
            # Telemetry: record the posted card so the engagement sweep can later
            # read its reactions. Skip silently if ?wait=true returned no message id.
            if msg and msg.get("id"):
                _log_posted(msg, item, cand, topic)
        for k in keys:
            if k not in seen:
                seen.add(k)
                seen_list.append(k)
        posted += 1
        per_topic[topic.key] = per_topic.get(topic.key, 0) + 1
        results.append(f"NEWS_POSTED {item.topic} {item.headline[:40]}")
        if posted >= MAX_POST_PER_RUN:
            break  # global safety cap

    if not dry_run:
        _save_seen(seen_list)
    print(f"\nposted {posted} item(s)")
    if posted == 0:
        tally: dict[str, int] = {}
        for r in results:
            k = r.split(maxsplit=1)[0]
            tally[k] = tally.get(k, 0) + 1
        detail = ", ".join(f"{k} ×{v}" for k, v in sorted(tally.items())) or "no outcomes"
        _staff_alert(
            "⚠️ **News digest posted 0 cards this run.**\n"
            f"Judge picked {len(items)} item(s) from {len(candidates)} fresh candidates "
            f"({skipped} already-posted skipped pre-judge), but none went out: {detail}.\n"
            "NEWS_POST_FAILED / NEWS_NO_WEBHOOK = broken webhook — fix now. "
            "NEWS_DEDUPED = same story via a second source (occasional is fine).", dry_run)
    return results
