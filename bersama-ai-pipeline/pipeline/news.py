"""Topic-routed trending-AI news → Discord channels.

Sources: Reddit (per-topic subs) + Hacker News + GitHub Trending (Search API).
A GLM judge tags each candidate with a TOPIC + HEAT + card; each item routes to
its topic's channel webhook. Per-topic dedup (state/news_seen.json).

Topics are configured in TOPICS below; only `live=True` topics gather + post.
**Coding (#ai-dev-tools) is live now.** Creative (image/video/voice) + research
(study/productivity) are wired but OFF until their webhooks are added — set the
env var (topic.webhook_env) + flip live=True to turn one on.

Heat bar = viral / popular (GitHub star velocity, Reddit upvotes, HN points),
NOT brand recognition — a fresh startup or community repo blowing up qualifies.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import requests
from openai import OpenAI

from .github_trending import fetch_trending
from .stateutil import append_jsonl, POSTED_LOG
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
    Topic("coding", "#ai-dev-tools", "DISCORD_NEWS_WEBHOOK_URL",
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
          reddit_subs=["SunoAI"],
          github_keywords=["text to speech", "voice clone", "tts", "suno", "music generation"],
          github_min_stars=150, live=True),
    Topic("research_study", "#education", "DISCORD_EDUCATION_WEBHOOK_URL",
          reddit_subs=["learnmachinelearning", "ArtificialIntelligence"],
          github_keywords=["learn ai", "ai course", "ml tutorial", "ai from scratch", "ai book"],
          github_min_stars=100, live=True),
    Topic("research_productivity", "#education", "DISCORD_EDUCATION_WEBHOOK_URL",
          reddit_subs=["Productivity", "ChatGPT"],
          github_keywords=["deep research", "research agent", "ai notes", "knowledge graph"],
          github_min_stars=150, live=True),
]
TOPIC_BY_KEY = {t.key: t for t in TOPICS}
LIVE_TOPICS = [t for t in TOPICS if t.live]

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


def fetch_reddit(subs: list[str]) -> list[dict]:
    out = []
    for sub in subs:
        url = f"https://www.reddit.com/r/{sub}/hot.json?limit=20"
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            children = (r.json().get("data") or {}).get("children") or []
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
        except Exception:  # noqa: BLE001
            continue
        time.sleep(0.5)
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


def gather_candidates(prefs=None) -> list[dict]:
    """Per-topic quotas: each live topic contributes its top Reddit+GitHub candidates
    (so every channel's domain reaches the judge — coding no longer crowds out creative),
    plus shared HN/HuggingFace/RSS for the judge to classify by topic."""
    token = os.environ.get("GITHUB_TOKEN", "")

    def _ai(c: dict) -> bool:
        return _looks_ai(c["title"]) or _looks_ai(c["snippet"])

    def _top(items: list[dict], n: int) -> list[dict]:
        return sorted([c for c in items if _ai(c)], key=lambda c: c.get("score", 0), reverse=True)[:n]

    cand: list[dict] = []
    on = bool(prefs and prefs.enabled)   # bandit actuator active only with enough signal
    quota_log: dict[str, int] = {}
    for t in LIVE_TOPICS:
        raw = fetch_reddit(t.reddit_subs) + fetch_trending(t.github_keywords, min_stars=t.github_min_stars, token=token)
        q = _quota_for_topic(prefs.score("topic", t.key)) if on else PER_TOPIC_QUOTA
        quota_log[f"topic:{t.key}"] = q
        top = _top(raw, q)
        # velocity bypass: a fast-rising repo in this topic still reaches the judge
        in_top = {c.get("url") for c in top}
        top += [c for c in raw
                if int(c.get("star_velocity") or 0) >= VELOCITY_THRESHOLD and c.get("url") not in in_top]
        cand += top
    # shared cross-topic sources — the judge tags these by topic
    hn_q = _quota_for_shared(HN_QUOTA, prefs.score("source", "HN")) if on else HN_QUOTA
    hf_q = _quota_for_shared(HF_QUOTA, prefs.score("source", "huggingface")) if on else HF_QUOTA
    rss_q = _quota_for_shared(RSS_QUOTA, prefs.score("source", "official")) if on else RSS_QUOTA
    quota_log.update({"source:HN": hn_q, "source:huggingface": hf_q, "source:official": rss_q})
    cand += _top(fetch_hn(), hn_q) + _top(fetch_hf_trending(), hf_q) + _top(fetch_rss(), rss_q)
    # Drift visibility (plan §4.3): always log which quotas governed this run.
    if on:
        print(f"[prefs] actuator ON (n_events={prefs.n_events}); dynamic quotas: {quota_log}")
    else:
        nevt = getattr(prefs, "n_events", 0)
        print(f"[prefs] actuator dormant (n_events={nevt} < {MIN_EVENTS} or PREFS_ENABLED=false); "
              f"static quotas — byte-identical to pre-loop.")

    seen, dedup = set(), []
    for c in cand:
        k = c.get("url") or c.get("title")
        if k and k not in seen:
            seen.add(k)
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

This is a TRENDING tracker, NOT a balance exercise. Pick purely by what's hottest/viral right
now. If the whole market is one topic this week (e.g. a run of coding-LLM launches), fill the
slots with that topic — do NOT pad with weaker items from other topics just for spread. The
candidate pool already guarantees you SEE every category each run; your only job is to post
what's genuinely hot, whatever category it lands in (and 0 from a quiet category is correct).

Be selective — only the genuinely hot. English only. If nothing qualifies, items: [].
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
        lines.append(
            f"\n[{i}] source={c['source']} score={c['score']}\n"
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

def _load_seen() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return set()


def _save_seen(seen: set[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(sorted(seen)[-500:], ensure_ascii=False, indent=2),
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
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


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


def _metric(cand: dict | None) -> str:
    """Reliable heat label from the candidate's real score (don't trust LLM flavor text)."""
    if not cand:
        return ""
    score = int(cand.get("score") or 0)
    src = cand.get("source") or ""
    if src == "github":
        vel = int(cand.get("star_velocity") or 0)
        delta = int(cand.get("star_delta") or 0)
        age = _age_days(cand.get("created_at"))
        base = f"⭐ {_fmt(score)} stars"
        bits = []
        if vel > 0:
            bits.append(f"▲ {_fmt(vel)}/day")
        elif delta > 0:
            bits.append(f"▲ {_fmt(delta)} since last run")
        if age is not None:
            bits.append(f"{age}d old")
        if bits:
            base += " · " + " · ".join(bits)
        return base
    if src == "huggingface":
        dl = int(cand.get("downloads") or 0)
        base = f"🤗 {_fmt(score)} likes"
        if dl > 0:
            base += f" · {_fmt(dl)} downloads"
        return base
    if src == "official":
        return "📢 official blog"
    if src == "HN":
        return f"{_fmt(score)} HN points"
    if src.startswith("r/"):
        return f"▲ {_fmt(score)} upvotes on {src}"
    return ""


TOPIC_LABEL = {
    "coding": "Coding & Agents",
    "creative_image": "Creative · Image",
    "creative_video": "Creative · Video",
    "creative_voice": "Creative · Voice",
    "research_study": "Research · Study",
    "research_productivity": "Research · Productivity",
}


_OG_IMG = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)(?::secure_url)?["\']',
    re.IGNORECASE,
)


def _fetch_image(url: str) -> str:
    """Best-effort: fetch the URL and return its og:image / twitter:image for the
    card. '' on any failure (timeout, non-200, no tag). Used so items without a
    built-in thumbnail still get an informative banner image at the bottom."""
    if not url:
        return ""
    try:
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) BersamaAi-news/1.0"},
            timeout=8,
        )
        if r.status_code != 200:
            return ""
        m = _OG_IMG.search(r.text[:60000])  # og:image lives in <head>
        return ((m.group(1) or m.group(2) or "")).strip() if m else ""
    except Exception:  # noqa: BLE001
        return ""


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


def _log_posted(msg: dict, item: NewsItem, cand: dict | None, topic: Topic) -> None:
    """Append one row per posted card to state/posted_log.jsonl — the engagement
    sweep's list of messages to read reactions from. Telemetry collects
    unconditionally (even while the actuator is dormant) so the model has data
    the day the owner flips PREFS_ENABLED on."""
    from datetime import datetime, timezone
    append_jsonl(POSTED_LOG, {
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
        "posted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


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
    """Fetch a public URL; extract {title, description, image} via og: meta tags."""
    try:
        r = requests.get(url, headers=URL_FETCH_HEADERS, timeout=15)
        if r.status_code != 200:
            return {}
        html = r.text
    except Exception:  # noqa: BLE001
        return {}
    return {
        "title": _meta_content(html, "og:title") or _html_title(html),
        "description": _meta_content(html, "og:description") or _meta_content(html, "description"),
        "image": _meta_content(html, "og:image"),
        "url": url,
    }


SINGLE_CARD_PROMPT = """\
You are the BersamaAi news editor. The owner shared ONE item (a Threads/X post,
article, or product/repo link) they judged worth the community's attention. Write
ONE news card — sober, no hype, grounded strictly in the given text.

Assign exactly one TOPIC:
- coding — AI coding agents / agentic / dev tools / LLM / chat & assistants
- creative_image — image generation
- creative_video — video generation + AI editing
- creative_voice — voice / audio / TTS / music
- research_study — study / learning AI tools
- research_productivity — research / productivity AI tools

Return: topic, category (LAUNCH|RELEASE|PRICING|BENCHMARK|OPEN_SOURCE|DEAL|UPDATE),
headline (<=110 chars, the news itself), body (1-2 sentences: what + why it matters),
source_url (pass through unchanged). English only.
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
    meta = fetch_url_meta(url)
    if not meta.get("title"):
        print(f"[share] could not fetch content for {url}")
        return "SHARE_FETCH_FAILED"
    client = OpenAI(api_key=api_key, base_url=base_url)
    tool = {"type": "function", "function": {
        "name": EMIT_ONE_TOOL["name"], "description": EMIT_ONE_TOOL["description"],
        "parameters": EMIT_ONE_TOOL["input_schema"],
    }}
    user_msg = (f"title: {meta['title']}\n"
                f"description: {meta.get('description', '')}\n"
                f"url: {url}")
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
    wh = os.environ.get(t.webhook_env, "")
    if not wh:
        return f"SHARE_NO_WEBHOOK {topic}"
    payload = build_news_payload(item, image=meta.get("image", ""))
    if dry_run:
        print(f"\n[share DRY-RUN] -> {t.channel}\n{payload}\n")
        return f"SHARED_DRY {topic} {item.headline[:40]}"
    try:
        _post(wh, payload)
    except Exception as e:  # noqa: BLE001
        if alert_fn:
            alert_fn(f"share post failed: {item.headline[:60]}: {e}", dry_run)
        return f"SHARE_POST_FAILED {e}"
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
    candidates = gather_candidates(prefs)
    print(f"gathered {len(candidates)} candidates across live topic(s): "
          + ", ".join(t.key for t in LIVE_TOPICS))
    if not candidates:
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
        return ["NEWS_NOTHING_TO_POST"]

    seen = _load_seen()
    by_url = {c["url"]: c for c in candidates}
    posted, results = 0, []
    per_topic: dict = {}
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
        wh = os.environ.get(topic.webhook_env, "") or (webhook_url if topic.live else "")
        if not wh:
            results.append(f"NEWS_NO_WEBHOOK {item.topic} {item.headline[:40]}")
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
        seen |= keys
        posted += 1
        per_topic[topic.key] = per_topic.get(topic.key, 0) + 1
        results.append(f"NEWS_POSTED {item.topic} {item.headline[:40]}")
        if posted >= MAX_POST_PER_RUN:
            break  # global safety cap

    if not dry_run:
        _save_seen(seen)
    print(f"\nposted {posted} item(s)")
    return results
