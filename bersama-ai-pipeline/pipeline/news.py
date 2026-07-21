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
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import requests
from openai import OpenAI

from .github_trending import fetch_trending

STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "news_seen.json"
HEADERS = {"User-Agent": "BersamaAi-news/1.0 (community bot)"}

HN_TOPN = 30
LOCAL_LIMIT = 40          # max candidates sent to the judge per run
MAX_POST_PER_RUN = 6


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


# Per-source quotas so the judge sees all three worlds each run. Without this,
# GitHub's tens-of-thousands star counts dominate a raw-score sort and crowd
# Reddit/HN out of the candidate pool (the judge ends up seeing almost only GitHub).
REDDIT_QUOTA = 15
HN_QUOTA = 10
GITHUB_QUOTA = 15


def gather_candidates() -> list[dict]:
    """Pull Reddit (LIVE topics' subs) + HN + GitHub (LIVE topics' keywords),
    with per-source quotas so the judge sees all three worlds (not just GitHub)."""
    subs = list({s for t in LIVE_TOPICS for s in t.reddit_subs})
    token = os.environ.get("GITHUB_TOKEN", "")
    github: list[dict] = []
    for t in LIVE_TOPICS:
        github += fetch_trending(t.github_keywords, min_stars=t.github_min_stars, token=token)

    def _ai(c: dict) -> bool:
        return _looks_ai(c["title"]) or _looks_ai(c["snippet"])

    def _top(items: list[dict], n: int) -> list[dict]:
        return sorted([c for c in items if _ai(c)],
                      key=lambda c: c.get("score", 0), reverse=True)[:n]

    cand = _top(fetch_reddit(subs), REDDIT_QUOTA) + _top(fetch_hn(), HN_QUOTA) + _top(github, GITHUB_QUOTA)
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
                "type": "array", "maxItems": 10,
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


def judge(candidates: list[dict], *, api_key: str, model: str, base_url: str) -> list[NewsItem]:
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
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
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


def _key(item: NewsItem, candidate_by_url: dict) -> str:
    c = candidate_by_url.get(item.source_url)
    base = (c["discussion"] if c else item.source_url) or item.headline
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


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


def _metric(cand: dict | None) -> str:
    """Reliable heat label from the candidate's real score (don't trust LLM flavor text)."""
    if not cand:
        return ""
    score = int(cand.get("score") or 0)
    src = cand.get("source") or ""
    if src == "github":
        return f"⭐ {_fmt(score)} stars"
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


def build_news_payload(item: NewsItem, thumbnail: str = "") -> dict:
    """One embed = one card: color-bar frame + the rich body in the description +
    the source preview as the thumbnail (GitHub avatar / Reddit image)."""
    emoji = CATEGORY_EMOJI.get(item.category, "📡")
    cat = item.category.replace("_", " ").title()
    topic_lbl = TOPIC_LABEL.get(item.topic, item.topic)
    byline = f"\n*🔥 {item.heat_reason}*" if item.heat_reason else ""
    content = (
        f"**{emoji} {cat} · {topic_lbl}**\n\n"
        f"**{item.headline}**\n"
        f"🔗 {item.source_url}"
        f"{byline}\n\n"
        f"**Why it matters**\n{item.body}"
    )
    return {"username": "BersamaAi", "embeds": [{
        "description": content[:4096],
        "color": BRAND_COLOR,
        "thumbnail": {"url": thumbnail} if thumbnail else None,
    }]}



def _post(webhook_url: str, payload: dict) -> None:
    r = requests.post(webhook_url, json=payload, timeout=15)
    if r.status_code not in (200, 204):
        raise RuntimeError(f"{r.status_code} {r.text[:200]}")


# ── orchestration ────────────────────────────────────────────────────────────

def run_news(*, dry_run: bool, stub: bool,
             api_key: str, model: str, base_url: str,
             webhook_url: str = "", alert_fn=None) -> list[str]:
    """Full news run. Returns a list of one-line status strings."""
    print("\n=== news run ===")
    if not LIVE_TOPICS:
        return ["NEWS_NO_LIVE_TOPICS"]

    candidates = gather_candidates()
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
            items = judge(candidates, api_key=api_key, model=model, base_url=base_url)
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
    for item in items:
        key = _key(item, by_url)
        if key in seen:
            results.append(f"NEWS_DEDUPED {item.headline[:40]}")
            continue
        topic = TOPIC_BY_KEY[item.topic]
        # webhook: the topic's env var, else the passed fallback for live topics
        wh = os.environ.get(topic.webhook_env, "") or (webhook_url if topic.live else "")
        if not wh:
            results.append(f"NEWS_NO_WEBHOOK {item.topic} {item.headline[:40]}")
            continue
        cand = by_url.get(item.source_url)
        metric = _metric(cand)
        if metric:
            item.heat_reason = metric  # real stars/upvotes/points, not LLM flavor text
        payload = build_news_payload(item, thumbnail=(cand or {}).get("thumbnail", ""))
        if dry_run:
            print(f"\n[discord DRY-RUN] -> {topic.channel}\n{payload}\n")
        else:
            try:
                _post(wh, payload)
            except Exception as e:  # noqa: BLE001
                if alert_fn:
                    alert_fn(f"news post failed: {item.headline[:60]}: {e}", dry_run)
                results.append(f"NEWS_POST_FAILED {item.headline[:40]}")
                continue
        seen.add(key)
        posted += 1
        results.append(f"NEWS_POSTED {item.topic} {item.headline[:40]}")
        if posted >= MAX_POST_PER_RUN:
            break

    if not dry_run:
        _save_seen(seen)
    print(f"\nposted {posted} item(s)")
    return results
