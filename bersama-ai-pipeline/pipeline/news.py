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
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from itertools import zip_longest
from pathlib import Path
from urllib.parse import urljoin, unquote, quote, urlsplit, urlunsplit

import requests
from openai import OpenAI

from .github_trending import fetch_trending
from .stateutil import append_jsonl, read_jsonl, read_posted_log, POSTED_LOG, POSTED_LOG_SHARE
from .preferences import load_preferences, MIN_EVENTS

STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "news_seen.json"
HEADERS = {"User-Agent": "BersamaAi-news/1.0 (community bot)"}

HN_TOPN = 30
# Keep >= (live topics x PER_TOPIC_QUOTA) + HN_QUOTA + HF_QUOTA + RSS_QUOTA, or the
# tail of the candidate list is sliced off before the judge. Static today: 9x8 + 8 +
# 3 + 12 = 95 (9 live topics after company_investment + cybersecurity were added).
# With the bandit actuator ON the quotas can total more; gather puts the shared
# sources FIRST so any overflow trims the lowest-preference topic tail, which is the
# intended degradation — it never starves HN/HF/RSS again.
LOCAL_LIMIT = 100         # max candidates sent to the judge per run
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


# Two DIFFERENT cost models, so the two lists follow different rules:
#
# `reddit_subs` — the whole group is ONE multireddit request (r/a+b+c/hot.rss), so
#   extra subs are free HTTP-wise. But reddit's multireddit hot-sort is GLOBAL: a
#   huge sub crowds small ones out, and only ~half a topic's quota is reserved for
#   Reddit anyway. So keep each list TIGHT (4-8) and prefer active subs over niche
#   brand subs. A sub that is misspelled/private/banned contributes 0 entries and
#   does NOT break the group (verified) — it just silently does nothing.
#
# `github_keywords` — one Search API call + 3s sleep EACH, and the query is
#   `created:<7d + stars:>min`, i.e. brand-new repos only. So these must be CATEGORY
#   terms that reliably return new repos. A product name that rarely spawns a viral
#   NEW repo (Kling, Seedance, Nano Banana…) belongs in AI_KEYWORDS + the judge
#   prompt instead — putting it here just burns 3s a run for an empty result.
TOPICS: list[Topic] = [
    Topic("coding", "#ai-llm-tools", "DISCORD_DEVTOOLS_WEBHOOK_URL",
          # channel renamed 2026-08-07 (was #ai-dev-tools); same ID + webhook. The env-var
          # name is left as DISCORD_DEVTOOLS_WEBHOOK_URL to avoid a half-migrated .env.
          reddit_subs=["LocalLLaMA", "ClaudeAI", "OpenAI", "ChatGPTCoding",
                       "ClaudeCode", "AI_Agents", "cursor", "singularity"],
          github_keywords=["ai agent", "coding agent", "agentic", "llm", "mcp",
                           "code review", "claude code", "agent skills",
                           # the agentic-era layers (see SYSTEM_PROMPT): context,
                           # tools, control loop, multi-agent topologies
                           "multi agent", "rag", "agent memory", "workflow automation",
                           "prompt engineering"],
          github_min_stars=150, live=True),
    Topic("creative_image", "#image-creation", "DISCORD_IMAGE_CREATION_WEBHOOK_URL",
          reddit_subs=["StableDiffusion", "comfyui", "midjourney", "FluxAI", "aiArt"],
          github_keywords=["stable diffusion", "image generation", "flux", "comfyui",
                           "image editing", "diffusion model", "lora training"],
          github_min_stars=200, live=True),
    Topic("creative_video", "#video-creation-aigc-tvc", "DISCORD_VIDEO_CREATION_WEBHOOK_URL",
          reddit_subs=["aivideo", "KlingAI", "runwayml", "VeoAI", "AIVideoGeneration"],
          # "sora" dropped: OpenAI discontinued the Sora app in Apr 2026 (API ends
          # Sep 2026) — it was spending a Search call a run on a dead product.
          github_keywords=["video generation", "text to video", "ai video editor",
                           "video model", "lip sync", "image to video"],
          github_min_stars=200, live=True),
    Topic("creative_voice", "#voice-studio", "DISCORD_VOICE_STUDIO_WEBHOOK_URL",
          reddit_subs=["SunoAI", "elevenlabs", "udiomusic", "AIMusic"],
          github_keywords=["text to speech", "voice clone", "tts", "music generation",
                           "speech to text", "voice agent", "audio generation"],
          github_min_stars=150, live=True),
    Topic("research_study", "#research-with-ai", "DISCORD_EDUCATION_WEBHOOK_URL",
          # r/ArtificialIntelligence 404s; r/artificial is the one that resolves.
          reddit_subs=["learnmachinelearning", "artificial", "MachineLearning", "deeplearning"],
          github_keywords=["learn ai", "ai course", "ml tutorial", "ai from scratch",
                           "ai book", "llm course"],
          github_min_stars=100, live=True),
    Topic("research_productivity", "#research-with-ai", "DISCORD_EDUCATION_WEBHOOK_URL",
          reddit_subs=["ChatGPT", "PromptEngineering", "notebooklm", "perplexity_ai", "Productivity"],
          github_keywords=["deep research", "research agent", "ai notes", "knowledge graph",
                           "second brain", "document ai"],
          github_min_stars=150, live=True),
    Topic("finance", "#earn-money-with-ai", "DISCORD_FINANCE_WEBHOOK_URL",
          # The channel is "earn money WITH AI", not just quant — the builder subs
          # carry the AI-side-income stories; _looks_ai() strips their non-AI noise.
          # Builder wing expanded (2026-07-26) with the indie/SaaS/vibe-coding subs
          # (micro_saas, StartupSoloFounder, SaaS, solopreneur) so the "I built a
          # project / SaaS and earned" beat surfaces alongside the quant half.
          reddit_subs=["algotrading", "quant", "QuantFinance", "SideProject",
                       "Entrepreneur", "indiehackers", "micro_saas",
                       "StartupSoloFounder", "SaaS", "solopreneur"],
          github_keywords=["fintech", "trading bot", "algorithmic trading", "quant",
                           "ai finance", "backtesting"],
          github_min_stars=150, live=True),
    # ── added 2026-08-07: peel two beats out of the old coding catch-all so the
    # judge can route precisely. Both subs lists are DISJOINT from every other
    # topic -> no duplicate multireddit fetch; the shared HN + official-RSS pool
    # (fetched once) + the pooled judge still catch cross-beat stories that
    # originate on coding's subs (e.g. an HN "AMD acquires Taalas" -> routed here).
    Topic("company_investment", "#ai-company-investment", "DISCORD_COMPANY_INVESTMENT_WEBHOOK_URL",
          # AI INDUSTRY money/ownership/strategy/org/policy: M&A, funding, IPO, compute
          # & chip-capacity deals, pricing, leadership/reorg, open-weight POLICY stances
          # + open-letter coalitions, AI fund/stock/earnings. Broad scope (owner-confirmed).
          # Finance subs (not AI subs) — the _looks_ai filter keeps only AI-naming posts;
          # AI-industry stories on coding's subs reach this topic via the judge, not re-fetch.
          reddit_subs=["investing", "stocks", "wallstreetbets", "technology",
                       "business", "Economics"],
          github_keywords=["ai investment", "funding tracker", "ai stock"],
          github_min_stars=50, live=True),
    Topic("cybersecurity", "#ai-cybersecurity-bypass", "DISCORD_CYBERSECURITY_WEBHOOK_URL",
          # AI SECURITY as the subject: hacking incidents, jailbreaks/safety-bypass,
          # eval escapes, red-teaming, AI-found vulns, AI cryptanalysis, supply-chain
          # intrusions, AND cyber-purpose model/tool launches (subject wins over category).
          reddit_subs=["cybersecurity", "netsec", "hacking", "security",
                       "redteamsec", "cryptography"],
          github_keywords=["jailbreak", "prompt injection", "llm security",
                           "red team", "pentest", "ai security"],
          github_min_stars=100, live=True),
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
#
# This gates EVERY candidate before it can take a quota slot, so a hot story whose
# title contains none of these is dropped before the judge ever sees it. Matching is
# plain lowercase SUBSTRING (no word boundaries), which cuts both ways: "ai" alone
# already matches "inpainting"/"hailuo"/"trained", so the filter is permissive by
# default — but a product name that shares no substring with the list is invisible.
# That was the gap: "Seedance 2.5 ships native 30s clips", "Kling 3.0 lip-sync",
# "Nano Banana Pro tops the image arena" and "Wan 2.6 open weights" all matched
# NOTHING here and were being discarded silently.
#
# Because it is substring-matched, do NOT add short generic tokens — "sol" hits
# "console", "tpu" hits "output", "rag" hits "storage", "aime" hits "claimed",
# "cline" hits "decline", "nova" hits "innovation". Prefer >=5 chars or a phrase.
# Product names go stale fast — re-check this list when the model landscape moves.
AI_KEYWORDS = (
    # generic / evergreen
    "ai", "llm", "model", "agent", "agentic", "mcp", "coding", "api", "pricing",
    "benchmark", "open source", "open-source", "open weights", "open-weight",
    "research", "study", "tutorial", "leaderboard", "arena", "inference",
    "mixture of experts", "distill", "nvidia", "blackwell",
    "swe-bench", "gpqa", "multimodal",
    # frontier labs & LLM families — FAMILY names only, never version numbers.
    # The model beat is a ladder (see MODEL RECENCY in SYSTEM_PROMPT): pinning
    # "gpt-5.6" here would go stale the week the next one ships, and "gpt" already
    # catches every future GPT. Add a name here only when a NEW family appears.
    "gpt", "chatgpt", "codex", "openai", "claude", "opus", "sonnet", "haiku",
    "fable", "anthropic", "gemini", "deepmind", "grok", "xai", "kimi", "moonshot",
    "deepseek", "qwen", "glm", "z.ai", "zhipu", "mistral", "llama", "muse",
    "minimax", "doubao", "ernie", "hunyuan", "nemotron", "olmo",
    # the model ladder itself — "X supersedes Y" / "Y retired" IS the story
    "frontier model", "state of the art", "outperform", "deprecat", "sunset",
    "open-sourced", "price cut", "supersede",
    # coding agents, harnesses & local inference
    "copilot", "cursor", "devin", "replit", "hermes", "openclaw", "claude code",
    "windsurf", "aider", "openhands", "antigravity", "jules", "lovable",
    "bolt.new", "vibe coding", "vllm", "sglang", "ollama", "gguf",
    "quantiz", "lm studio", "llama.cpp",
    # ── the agentic era: the four layers of an agent ─────────────────────────
    # 1. INSTRUCTION — the prompt itself
    "prompt engineering", "prompt optimi", "system prompt", "output format",
    # 2. CONTEXT — what gets injected this turn
    "context engineering", "context window", "retrieval", "retrieval-augmented",
    "vector db", "vector database", "embedding", "reranker", "long-term memory",
    "agent memory", "compaction", "knowledge base",
    # 3. TOOL — what actions the model can take
    "function calling", "tool calling", "tool use", "connector", "mcp server",
    "computer use", "browser use",
    # 4. CONTROL — when to act, stop, retry, ask
    "agent loop", "loop engineering", "graph engineering", "orchestration",
    "orchestrator", "task planning", "automation workflow", "state machine",
    # harness engineering — the scaffold PRODUCT wrapped around a model
    "harness", "scaffold", "sandbox", "eval", "guardrail", "checkpoint",
    "persistence", "long-running",
    # multi-agent topologies
    "multi-agent", "multi agent", "subagent", "sub-agent", "agent delegation",
    "swarm", "handoff", "blackboard", "shared context", "worker agent",
    "langgraph", "langchain", "llamaindex", "crewai", "autogen", "dspy",
    "smolagents", "agent framework",
    # unified / native multimodal — one model across text+image+audio+video
    "omni", "any-to-any", "unified model", "vision language", "vlm",
    "speech-to-speech", "realtime api", "native multimodal",
    # image
    "stable diffusion", "flux", "comfyui", "midjourney", "image gen",
    "nano banana", "nanobanana", "seedream", "imagen", "ideogram", "recraft",
    "krea", "controlnet", "sdxl", "lora", "inpaint", "upscal",
    # video
    "sora", "veo", "runway", "video gen", "text to video", "image to video",
    "kling", "seedance", "hailuo", "pika", "luma", "dream machine", "wan 2",
    "framepack", "ltx-video", "higgsfield", "lip sync", "lipsync", "aigc",
    # voice & music
    "suno", "udio", "elevenlabs", "eleven music", "tts", "voice clone",
    "whisper", "kokoro", "cartesia", "sesame", "vibevoice", "chatterbox",
    "speech", "podcast",
    # research / productivity
    "notebooklm", "deep research", "perplexity", "obsidian", "granola", "notion",
    # money
    "trading bot", "algo trading", "backtest", "polymarket", "kalshi",
    "prediction market", "side income", "monetiz", "freelance",
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


# Official blog + AI-newsroom RSS/Atom feeds — the earliest signal for "Kimi K3 /
# Grok Build dropped", hours before Reddit. Every URL below was fetched and parsed
# before being added; 404/parse errors skip the feed at runtime (and now log it).
#
# The labs whose OWN feeds don't exist are covered by the newsroom tier: Anthropic,
# xAI, Moonshot, DeepSeek, ByteDance/Seed, Kuaishou (Kling), ElevenLabs, Runway and
# Black Forest Labs publish no working RSS (checked 2026-07-24, every plausible URL
# 404s), so their launches arrive via the-decoder / TechCrunch / smol.ai / HN.
#
# Ordering matters: the quota is filled ROUND-ROBIN across feeds, so put the
# highest-signal feeds first — a feed past the quota still contributes on runs where
# earlier feeds return nothing fresh.
OFFICIAL_RSS = [
    # tier 1 — first-party lab announcements
    "https://openai.com/news/rss.xml",
    "https://blog.google/technology/ai/rss/",
    "https://deepmind.google/blog/rss.xml",
    "https://blog.google/products/gemini/rss/",
    "https://qwenlm.github.io/blog/index.xml",
    "https://mistral.ai/rss.xml",
    "https://huggingface.co/blog/feed.xml",
    # tier 2 — AI newsrooms; where Kling / Seedance / Nano Banana news actually breaks
    "https://the-decoder.com/feed/",
    "https://news.smol.ai/rss.xml",
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://venturebeat.com/category/ai/feed/",
    "https://www.artificialintelligence-news.com/feed/",
    "https://www.marktechpost.com/feed/",
    "https://www.testingcatalog.com/rss/",       # unreleased-feature sightings
    # tier 3 — creative-tool + practitioner feeds (feed the non-coding channels)
    "https://blog.fal.ai/rss/",                  # image/video model hosting
    "https://blog.comfy.org/feed.xml",           # ComfyUI -> #image-creation
    "https://www.latent.space/feed",
    "https://simonwillison.net/atom/everything/",
    "https://github.blog/changelog/feed/",
]

RSS_PER_FEED = 4          # newest items taken per feed before the round-robin merge
RSS_MAX_AGE_DAYS = 14     # older than this is archive, not "trending"


def _rss_field(el, names: set[str]) -> str:
    """First non-empty text/href of a child whose local tag is in `names` (namespace-agnostic)."""
    for child in el:
        if child.tag.split("}")[-1] in names:
            t = (child.text or child.get("href") or "").strip()
            if t:
                return t
    return ""


def _rss_date(el):
    """Best-effort item timestamp — RFC-822 <pubDate> or ISO-8601 <updated>/<published>.
    Returns an aware datetime, or None when the feed dates nothing parseable."""
    raw = _rss_field(el, {"pubDate", "published", "updated", "date"})
    if not raw:
        return None
    dt = None
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


def fetch_rss(feeds: list[str] = OFFICIAL_RSS) -> list[dict]:
    """Poll the blog/newsroom RSS+Atom feeds (RSS 2.0 + Atom). 404/parse → skip that feed.

    Each feed contributes at most RSS_PER_FEED recent items and the feeds are then
    INTERLEAVED round-robin, because the RSS quota is filled from the front of this
    list. Flat concatenation gave the whole quota to the first feed: every RSS item
    scores 0, `_top()`'s sort is stable, and openai.com/news/rss.xml alone returns
    ~1000 items — so every other lab's feed was cut off before reaching the judge.
    Items older than RSS_MAX_AGE_DAYS are dropped so a long archive feed can't spend
    a trending slot on a 2023 post.
    """
    import xml.etree.ElementTree as ET
    cutoff = datetime.now(timezone.utc) - timedelta(days=RSS_MAX_AGE_DAYS)
    per_feed: list[list[dict]] = []
    for feed in feeds:
        try:
            r = requests.get(feed, headers=URL_FETCH_HEADERS, timeout=15)
            if r.status_code != 200:
                print(f"[rss] {feed} -> HTTP {r.status_code}")
                continue
            root = ET.fromstring(r.content)
        except Exception as e:  # noqa: BLE001 — one dead feed shouldn't kill the run
            print(f"[rss] {feed} failed: {e}")
            continue
        dated: list[tuple] = []
        for el in root.iter():
            if el.tag.split("}")[-1] not in ("item", "entry"):
                continue
            title = _rss_field(el, {"title"})
            if not title:
                continue
            when = _rss_date(el)
            if when and when < cutoff:
                continue
            link = _rss_field(el, {"link"})
            snippet = re.sub(r"<[^>]+>", " ",
                             _rss_field(el, {"description", "summary", "content"}))
            snippet = html_mod.unescape(re.sub(r"\s+", " ", snippet)).strip()[:300]
            dated.append((when, {
                "title": title, "url": link, "discussion": link,
                "source": "official", "score": 0, "snippet": snippet,
            }))
        # sort newest-first when the feed dates every item; else trust feed order
        if dated and all(w for w, _ in dated):
            dated.sort(key=lambda t: t[0], reverse=True)
        if dated:
            per_feed.append([c for _, c in dated[:RSS_PER_FEED]])
    return [c for row in zip_longest(*per_feed) for c in row if c]


# Per-TOPIC quotas: each live topic contributes its own top Reddit+GitHub candidates
# to the pool, so coding (hotter/more numerous) can't crowd creative/research out
# before the judge sees them. Shared sources (HN/HF/RSS) get their own quotas.
PER_TOPIC_QUOTA = 8
HN_QUOTA = 8
HF_QUOTA = 3
RSS_QUOTA = 12   # raised with the feed list: round-robin means N slots = N labs/newsrooms
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


# ── dedup key primitives ─────────────────────────────────────────────────────
# The dedup keys hash a CANONICAL form of the URL/headline so the same story can't
# post twice just because the URL gained ?utm_* params, dropped a trailing slash,
# or the LLM reworded "launches" → "releases". All normalization happens ONLY here,
# inside key computation — item.source_url and c['url'] are NEVER mutated, because
# the candidate join (by_url[c['url']]) and the card's clickable embed link depend
# on the raw string the judge emitted.

# Tracking params stripped by _norm_url. This is an EXPLICIT list, never "anything
# that looks like tracking" — `id` (HN item), `v` (YouTube video), `context` (Reddit
# comment) are load-bearing identity and must survive, or every HN thread / YouTube
# video would collapse to one key.
_TRACK_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "utm_name", "utm_referrer", "fbclid", "gclid", "gclsrc", "dclid", "msclkid",
    "mc_cid", "mc_eid", "_hsenc", "_hsmi", "icid", "ito", "_ga", "_gl",
    "guce_referrer", "guccounter", "ref_url", "share",
})
# Hosts where `ref` / `s` are sharing trackers (not identity) and get stripped. On
# other hosts those names may be identity, so they are stripped ONLY on these hosts.
_REF_STRIP_HOSTS = frozenset({"x.com", "twitter.com", "t.co", "facebook.com",
                              "instagram.com", "threads.net", "linkedin.com", "bsky.app"})


def _norm_url(url: str) -> str:
    """Canonical URL form for dedup: lowercase scheme+host, strip tracking query params,
    drop the fragment, sort the surviving query, strip the trailing slash. So
    `blog.google/x/?utm_source=y` and `blog.google/x` both become `https://blog.google/x`.
    Identity params (YouTube ?v=, HN ?id=, Reddit ?context=) are NEVER stripped — only
    the explicit _TRACK_PARAMS set + ref/s on social hosts — so two different videos /
    threads never collapse to one key."""
    if not url:
        return ""
    s = url.strip()
    try:
        p = urlsplit(s)
    except ValueError:
        return s.lower()
    if not p.scheme or not p.netloc:           # not an absolute URL (e.g. a title) → as-is
        return s
    scheme = (p.scheme or "https").lower()
    netloc = p.netloc.lower()
    path = (p.path or "/").rstrip("/") or "/"
    host = netloc.split("@")[-1].split(":")[0]  # strip user:pass@ and :port
    if p.query:
        kept = []
        for kv in p.query.split("&"):
            if not kv:
                continue
            k = kv.split("=", 1)[0].lower()
            if k in _TRACK_PARAMS:
                continue
            if k in ("ref", "s") and host in _REF_STRIP_HOSTS:
                continue
            kept.append(kv)
        query = "&".join(sorted(kept))
    else:
        query = ""
    return urlunsplit((scheme, netloc, path, query, ""))


# Headline-signature drop set: LAUNCH-SYNONYMS ONLY. Never add deprecation/decline
# words (deprecates/delays/cuts/drops/ends) — those would merge a launch with its
# opposite ("launches o3" vs "deprecates o3"). Never add new/model/ai/update. The
# signature lets "Google launches/releases/ships X" — verb-flips, even across hosts
# (blog.google vs deepmind.google) — collapse to one key.
_LAUNCH_VERBS = frozenset({
    "launch", "launches", "launched", "launching",
    "release", "releases", "released", "releasing",
    "ship", "ships", "shipped", "shipping",
    "unveil", "unveils", "unveiled", "unveiling",
    "announce", "announces", "announced", "announcing",
    "introduce", "introduces", "introduced", "introducing",
    "debut", "debuts", "debuted", "debuting",
})


def _headline_signature(headline: str) -> str:
    """Token-set headline signature, launch-verbs removed + word order ignored, so the
    same story worded with a different verb (launches/releases/ships) or surfaced on a
    different host dedupes. Conservative: returns '' (no signature key) when the residual
    is too thin — then dedup falls back to the URL key + legacy headline key rather than
    risk merging two different short stories."""
    toks = re.findall(r"[a-z0-9]+", headline.lower())
    sig = sorted({t for t in toks if t not in _LAUNCH_VERBS and t not in {"the", "a", "an"}})
    if not any(len(t) >= 4 for t in sig):         # need ≥1 substantive token (model/proper noun)
        return ""
    if sum(len(t) for t in sig) < 12:             # mirror the legacy ≥12-char floor on the residual
        return ""
    return " ".join(sig)


def _url_dedup_keys(url: str) -> set:
    """All URL dedup keys for a story: the CANONICAL normalized key (always) plus, when
    the raw URL had tracking params / trailing slash / mixed case, the LEGACY raw key —
    so the dual-key transition still matches pre-fix news_seen.json entries (raw sha1)
    while new writes converge on the normalized key. Raw URLs aren't stored in the ledger,
    so this is what avoids orphaning pre-fix history (which would cause a repost storm)."""
    keys: set = set()
    if not url:
        return keys
    raw = url.strip()
    norm = _norm_url(raw)
    if norm:
        keys.add(hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16])
    if raw.lower() != norm:                        # had tracking/slash/case → keep legacy raw key too
        keys.add(hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16])
    return keys


def _headline_dedup_keys(headline: str) -> set:
    """Legacy whole-headline key (h-prefix, kept for transition) + new signature key
    (s-prefix) so verb-flip / cross-host dupes collapse without orphaning old h-keys."""
    keys: set = set()
    if not headline:
        return keys
    low = headline.lower()
    legacy = re.sub(r"[^a-z0-9]", "", low)
    if len(legacy) >= 12:
        keys.add("h" + hashlib.sha1(legacy.encode("utf-8")).hexdigest()[:16])
    sig = _headline_signature(low)
    if sig:
        keys.add("s" + hashlib.sha1(sig.encode("utf-8")).hexdigest()[:16])
    return keys


def _cand_keys(c: dict) -> set:
    """URL dedup keys for a candidate — the same canonical key space `_keys()` writes to
    news_seen.json, so gather can drop already-posted stories before they burn a quota
    slot or a judge token. Returns the full key SET (legacy + normalized) so the dual-key
    transition matches either form already in the ledger."""
    base = c.get("discussion") or c.get("url") or ""
    if base:
        return _url_dedup_keys(base)
    t = (c.get("title") or "").strip()             # defensive: no URL → dedup on title so we don't crash
    return {hashlib.sha1(t.encode("utf-8")).hexdigest()[:16]} if t else set()


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
        kept = [c for c in items if not (seen & _cand_keys(c))]
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
    # Shared sources go in FRONT of the topic candidates. They used to be appended
    # last and then sliced off by `dedup[:LOCAL_LIMIT]`: 7 live topics x 8 = 56 topic
    # slots already exceeded the old cap of 50, so on a healthy run HN + HuggingFace +
    # official RSS reached the judge ZERO times — the feeds documented as the
    # "earliest signal" were structurally unreachable. Their combined ceiling is
    # small (hn_q + hf_q + rss_q), so fronting them costs the topics almost nothing.
    cand = (_top(_fresh(fetch_hn(), "HN"), hn_q)
            + _top(_fresh(fetch_hf_trending(), "huggingface"), hf_q)
            + _top(_fresh(fetch_rss(), "official"), rss_q)
            + cand)
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
        k = _norm_url(c.get("url") or "") or c.get("title")
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

MODEL RECENCY — read this before judging any model story. The model beat is a LADDER,
not an archive. Readers chase whatever just landed, and the moment a newer generation
ships, the one below it stops being news:
- The NEWEST generation of any family — or a brand-new family — is hot. A model nobody
  has heard of yet is a POSITIVE signal, not a reason to skip.
- A model that a newer sibling has already superseded is NOT news, however good it is.
  Skip it, UNLESS the story is itself a change of state: a price cut, weights being
  opened, a deprecation/shutdown, or a benchmark where the older model still wins —
  those ARE the news.
- Between "Model X ships" and "10 things you can do with Model X (out for months)",
  post the first and drop the second. Recency of the SUBJECT, not just of the post.
- Version numbers in the examples below go stale fast and are illustrative only. Judge
  by whether this candidate is the newest thing in its lane RIGHT NOW — never by
  whether its name appears here. If two candidates cover the same family, keep the
  newer one and drop the older.

TOPICS (assign exactly one). The examples are the CURRENT landscape, not a whitelist —
a model or tool you don't recognise still belongs to whichever topic it fits, and a
brand-new name nobody has heard of is a POSITIVE signal, not a reason to skip it:
- coding — DEFAULT catch-all for the agent / LLM beat: frontier-model releases, coding
  agents, dev tools, harnesses, MCP / agent-skill tooling, general agent AND robotics
  PRODUCT launches, inference tooling. ANY maker: bigco + startup + community/open-source;
  US + Chinese. If a story is agentic / LLM and NOT clearly investment, cyber, money, or
  research, it lands here. e.g. Claude Code, Codex, Cursor, Cline, Aider, Windsurf,
  Copilot, Antigravity, Jules, Replit, Devin, Hermes, OpenClaw; frontier models — GPT-5.6,
  Claude Opus 4.8 / Claude 5, Gemini 3.x, Grok 4.5 / Grok Build, Kimi K3, DeepSeek,
  Qwen 3.x, GLM-5.2, Meta Muse Spark, MiniMax; local inference (llama.cpp, Ollama, vLLM,
  LM Studio); robotics products like Gemini Robotics ER 2.
- company_investment — the AI INDUSTRY's money / ownership / strategy / org / policy:
  acquisitions & M&A (a creative-AI company being acquired lands HERE — the deal is the
  story), funding rounds, IPOs, big investments, compute & chip-capacity deals (GPU
  clusters, memory standards, custom-silicon PROGRAMS), PRICING changes (even for a
  creative tool — pricing is strategy, not product), CEO / leadership changes, reorgs,
  company open-weight POLICY stances and industry open-letter coalitions, AI fund / stock
  / earnings market moves. Test: is the STORY about money, ownership, strategy, org, or
  policy? -> here. Is it about a NEW THING shipping? -> not here (coding, or a creative
  channel).
- cybersecurity — AI SECURITY as the SUBJECT: hacking incidents, jailbreaks / safety-bypass
  frameworks, models escaping cyber-eval sandboxes, red-teaming, AI-found vulnerabilities,
  AI cryptanalysis, supply-chain intrusions of AI infra, AND cybersecurity-PURPOSE model /
  tool releases (a Cyber model, a pentest agent) -> route here even though those are also
  Launches. If the thing's PURPOSE is offense / defense / security, it is cybersecurity,
  regardless of the category tag. (A deepfake-voice FRAUD incident is a security incident
  -> here, NOT creative_voice.)
- creative_image — image generation + editing: Nano Banana Pro / Nano Banana 2 (Gemini
  Image), GPT Image 2, Seedream 5.0, FLUX.2 / FLUX 3, Midjourney, Ideogram, Recraft,
  Krea, Qwen-Image, Stable Diffusion, ComfyUI workflows, LoRA / ControlNet.
- creative_video — video generation + AI editing: Seedance 2.5 (ByteDance), Kling 3.0
  (Kuaishou), Veo 3.1 (Google), Hailuo / MiniMax, Runway, Pika, Luma Dream Machine,
  Wan, LTX-Video, Grok Imagine, Higgsfield, FramePack, lip-sync + image-to-video tools.
  (OpenAI's Sora app was discontinued in 2026 — Sora news is now shutdown/migration news.)
- creative_voice — voice / audio / TTS / music: ElevenLabs + Eleven Music, Suno v5,
  Udio, Whisper, Kokoro, Cartesia, Sesame, VibeVoice, Chatterbox, voice agents, cloning.
- research_study — research papers, academic studies, science-publishing norms,
  from-scratch implementations, courses, learning material. A research FINDING or paper
  = here, even when the subject is agents or a model. Tie-break vs cybersecurity: a paper
  that STUDIES / MEASURES security = here; one that SHIPS an attack/defense artifact (a
  jailbreak repo, a pentest harness) = cybersecurity.
- research_productivity — research / productivity AI tools (deep research, NotebookLM,
  Perplexity, notes / knowledge work, document + OCR agents).
- finance — the INDIVIDUAL / BUILDER making money WITH AI: side income, indie / SaaS /
  vibe-coding revenue, autonomous-business runs, AI trading / quant / algorithmic trading,
  robo-advisors, prediction markets. NOT institutional / market flows -> those are
  company_investment. Test: can a PERSON use this to earn? -> finance; is the MARKET /
  INDUSTRY moving? -> company_investment.

ROUTING CALIBRATION — learn these patterns. SUBJECT WINS OVER CATEGORY: a story is filed
by what it is ABOUT, not by the tag (LAUNCH, RELEASE, DEAL) it carries. Four boundary
rules, each with positives and the "looks-like-X-but-goes-to-Y" trap:
1. Robotics / model PRODUCT LAUNCHES are `coding`, NOT `company_investment`. A new thing
   SHIPPING is a product story, not a money story — even from a big lab.
   POSITIVES (`coding`): "DeepMind launches Gemini Robotics ER 2"; "Anthropic releases
   Claude Opus 4.8"; "Moonshot open-sources Kimi K3 weights" (an OPEN_SOURCE model drop).
   TRAP (-> `company_investment`): a CEO change, funding round, IPO, or acquisition at
   that SAME company is a money / org story, not a product ship.
2. Cybersecurity-PURPOSE launches are `cybersecurity`, NOT `coding`, even though they are
   also Launches. If the tool's PURPOSE is offense / defense / security, route here.
   POSITIVES (`cybersecurity`): "Microsoft launches MAI-Cyber"; "Google ships Gemini Flash
   Cyber for threat analysis"; "NERV-BREAK jailbreak / safety-bypass framework"; "Anthropic
   cryptanalysis demo breaks RSA variants"; "Red team finds CVE via AI; supply-chain
   intrusion of an AI PyPI package."
   TRAP (-> `coding`): a general frontier-model release (GPT-5.6, Claude Opus 4.8, Gemini
   3.x) with NO security purpose is `coding`. A coding assistant that adds a vuln-scan
   FEATURE stays `coding` — its purpose is still development, not pentest / red-team.
3. Individual-vs-institutional money split. Can a PERSON use this to earn? -> `finance`.
   Is the MARKET / INDUSTRY moving? -> `company_investment`.
   POSITIVES (`finance`): "Indie dev makes $12k/mo from a vibe-coded SaaS"; "Autonomous
   agent runs a profitable prediction-market play"; "AI quant bot backtest beats S&P over
   90 days (open-source repo)."
   TRAPS (-> `company_investment`): "AI hedge fund collapses / mark-to-market loss";
   "Nvidia earnings beat; AI index up 4%"; a creative-AI company getting ACQUIRED (the
   deal is industry money).
4. Open-weight POLICY / letters / coalitions are `company_investment` (industry STRATEGY),
   NOT `coding` — even though "open weights" sounds like a release. PRICING announcements
   are `company_investment` even when the product is creative (pricing = strategy).
   POSITIVES (`company_investment`): "Jensen Huang founds an open-weight alliance"; "Google
   / Meta / Nvidia / IBM sign open letter on AI openness"; "AI founders lobby EU on
   open-source rules"; "OpenAI $40B funding round closes; valuation $500B"; "AMD acquires
   AI chip startup; CEO replaced in reorg"; "Midjourney raises prices."
   TRAP (-> `coding`): a lab actually DROPPING model weights ("Moonshot open-sources Kimi
   K3") is an OPEN_SOURCE product release -> `coding`, not a policy stance.
When two rules could both apply, the PURPOSE test breaks the tie: security purpose ->
`cybersecurity`; a product shipping -> `coding`; a money / market / org / policy story ->
`company_investment`; a person earning -> `finance`; a paper studying / measuring ->
`research_study`.

AGENTIC-AI SUBJECT MATTER — this era's beats. A genuine move in any of these is as
newsworthy as a model release, and they are all `coding` UNLESS the ROUTING CALIBRATION
above sends them elsewhere — clearly image / video / voice; a money story (finance vs
company_investment); a security incident or cyber-purpose tool (cybersecurity); an
industry deal / strategy / policy / funding / leadership story (company_investment); or
a research paper (research_study). An agent is four layers, and each is its own beat:
1. INSTRUCTION — the prompt: prompt engineering, prompt optimisation, system-prompt
   design, role/rules/output-format control.
2. CONTEXT — what gets injected this turn: RAG and retrieval, short- and long-term
   memory, and context engineering — history compaction, truncating/compressing tool
   results, context-window strategy.
3. TOOL — what actions are available: MCP and connectors, function/tool calling,
   computer use, browser use.
4. CONTROL — when to act, stop, retry, ask: the agent loop, task planning and
   execution, orchestration, "loop engineering" and its successor "graph engineering",
   AI automation workflows.
Plus two things built ON those layers:
- HARNESS ENGINEERING — the scaffold PRODUCT wrapped around a model: persistence,
  scheduling, warmup, evals, permissions, sandboxing (Claude Code, Codex, OpenClaw,
  Hermes, Replit, Lovable). A harness release counts as much as a model release.
- MULTI-AGENT TOPOLOGIES — orchestrator-worker (a task split into related sub-tasks
  run in parallel, then synthesised into one result); executor-advisor (an advisor
  injected when a round needs stronger judgement); handoff (async relay across
  sessions, to outlive a single context window); blackboard / shared-context (agents
  self-organise off shared state, no fixed leader); swarm (N independent identical
  tasks, no synthesis step).
And one cross-cutting frontier: UNIFIED MULTIMODAL models — one model natively
reasoning over text + image + audio + video and generating in more than one of them
(the Gemini Omni / Omni Flash class). Route those by what the story is mainly ABOUT:
a general unified-model release is `coding`; "it now does video" is `creative_video`.

ONE STORY, ONE CARD — the most important selection rule. Many candidates each run
are different sources reporting the SAME underlying event (a company blog, a Hacker
News discussion, a Reddit thread, and a newsletter all recapping one release). Emit
AT MOST ONE card per real-world event. Collapse them to the single best source, in
this priority: official company blog / HuggingFace / GitHub primary  >  Hacker News
discussion  >  Reddit thread  >  third-party rehash (news blogs, newsletters, weekly
roundups). Drop the losers — do not emit them.

The test is whether the NEWS EVENT is the same, NOT whether the product/entity name
is the same. A re-report of an already-covered event is a duplicate even if its URL
and headline are completely different. But a DIFFERENT event involving the same
entity IS still news — keep it. Examples (Moonshot's Kimi K3, late July 2026):
- SAME event → keep at most one (the primary source): "Moonshot open-sources Kimi
  K3" / "Kimi K3 weights released" / "Kimi K3 open-weights, 2.8T MoE, 1M context" /
  "Moonshot releases Kimi K3… on Hugging Face". One release, re-reported across
  sources — post only the primary/official one, drop the rehashes.
- DIFFERENT event → keep each: "Kimi K3 now available via Telnyx Inference API"
  (new distribution/availability), "Kimi K3 team open-sources AgentENV" (a separate
  product), "Sebastian Raschka's architecture analysis of Kimi K3" (independent
  analysis), "UK AISI publishes cyber-capability assessment of Kimi K3" (a safety
  eval). These share the Kimi K3 name but are genuinely new storylines — do NOT
  collapse them into the release card.
Any story listed under "RECENTLY POSTED" (below the candidates) is already covered.
Do NOT re-emit a re-report of that same event this run, even from a new source. A
real CHANGE-OF-STATE since the original post (price cut, shutdown/deprecation, new
benchmark win, independent analysis, new platform availability) is still news; a
rehash of the same fact is not.

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


RECENT_STORY_WINDOW_H = 48   # a story posted within this window is "already covered"
RECENT_STORY_MAX = 30        # cap headlines injected into the judge prompt (token budget)


def _recent_posted_headlines(window_h: int = RECENT_STORY_WINDOW_H,
                             limit: int = RECENT_STORY_MAX) -> list[dict]:
    """Recent posted cards (newest first) that the judge should treat as ALREADY COVERED.

    Why this exists: the dedup ledger (news_seen.json) only matches an exact URL or a
    character-identical headline (``_keys``). It cannot tell that a brand-new source
    re-reporting yesterday's release is the SAME story. Feeding the judge the last ~48h
    of posted headlines lets the ONE STORY, ONE CARD rule work ACROSS runs, not just
    within one run's candidate pool — so the 4th Reddit rehash of a release 12h later
    is suppressed, while a genuinely different storyline (e.g. the same model landing
    on a new API provider) still posts. Reads auto-news + owner /share cards alike."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=window_h)
    out: list[dict] = []
    for row in read_posted_log():
        ts = str(row.get("posted_at") or "")
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when < cutoff:
            continue
        out.append({"headline": row.get("headline", ""),
                    "topic": row.get("topic", ""),
                    "channel": row.get("channel", ""),
                    "age_h": max(0, int((now - when).total_seconds() // 3600))})
    out.sort(key=lambda r: r["age_h"])   # newest (smallest age) first
    return out[:limit]


def _build_judge_user_message(candidates: list[dict], recent: list[dict] | None = None) -> str:
    lines: list[str] = []
    if recent:
        lines.append(
            f"RECENTLY POSTED (last {RECENT_STORY_WINDOW_H}h — these stories are ALREADY covered. "
            "Apply ONE STORY, ONE CARD: do NOT re-emit a re-report of the same event, even via a "
            "different source/URL/headline):")
        for r in recent:
            lines.append(f"  - [topic={r.get('topic', '?')}] {r.get('headline', '')}  "
                         f"({r.get('age_h', 0)}h ago, {r.get('channel', '')})")
        lines.append("")
    lines.append(f"CANDIDATES ({len(candidates)}):")
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
          prefs_section: str = "", recent: list[dict] | None = None) -> list[NewsItem]:
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
                      {"role": "user", "content": _build_judge_user_message(candidates, recent=recent)}],
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


def _share_seen_keys() -> set:
    """Dedup keys for cards the owner posted via /share (the VM's posted_log_share.jsonl
    shard), unioned into `seen` at the start of each digest run so the auto-digest skips a
    story the owner already shared. This is the split-brain-safe direction: the digest
    SOLE-owns news_seen.json (git-committed by GH Actions); /share never writes it.
    Instead the digest reads the /share shard each run. Returns {} when the shard is
    absent on this machine (e.g. the GH Actions runner before the VM's shard syncs)."""
    keys: set = set()
    if not POSTED_LOG_SHARE.exists():
        return keys
    for row in read_jsonl(POSTED_LOG_SHARE):
        keys |= _url_dedup_keys(row.get("source_url") or "")
        keys |= _headline_dedup_keys(row.get("headline") or "")
    return keys


def _keys(item: NewsItem, candidate_by_url: dict) -> set:
    """Dedup keys for an item — URL keys PLUS headline keys, each in BOTH legacy and
    normalized/signature form (dual-key transition). The same story surfacing on two
    sources (Reddit + HN, different URLs), with two wordings (launches vs ships), or on
    two hosts (blog.google vs deepmind.google) collapses instead of posting twice.
    Normalization is applied ONLY here — never to item.source_url / c['url'] — the
    candidate join and the card's clickable embed link depend on the raw string."""
    c = candidate_by_url.get(item.source_url)
    url_base = (c["discussion"] if c else "") or item.source_url
    return _url_dedup_keys(url_base) | _headline_dedup_keys(item.headline or "")


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
    "company_investment": "Investment · Industry",
    "cybersecurity": "Security · Cyber",
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


def _clean_url_for_discord(url: str) -> str:
    """Return a URL Discord will accept in an embed url/image field, or '' to drop it.

    Discord rejects the WHOLE embed (HTTP 400 `{"embeds":["0"]}`) for any malformed
    URL field, and that terse error names NO field — so a single bad scraped
    og:image or a model-mangled source_url silently kills the entire card. We:
      • encode ALL internal whitespace (spaces, \\n, \\r, \\t — GLM sometimes
        line-wraps a URL) as %20; raw control chars make Discord reject the URL;
      • require an http(s) scheme + a host;
      • percent-encode non-ASCII path/query (and punycode the host) instead of
        dropping the URL — this community shares Chinese sources whose og:image /
        canonical URLs contain non-ASCII path chars, and Discord accepts the
        percent-encoded form (dropping it would silently strip the link/thumbnail).
    Anything still structurally invalid → '' so the caller drops just that field
    instead of losing the whole post.
    """
    if not url or not isinstance(url, str):
        return ""
    u = re.sub(r"\s+", "%20", url.strip())   # encode whitespace runs (incl. \n/\t/\r)
    if not u:
        return ""
    ps = urlsplit(u)
    if ps.scheme not in ("http", "https") or not ps.netloc:
        return ""
    netloc = ps.netloc
    try:
        netloc.encode("ascii")
    except UnicodeEncodeError:
        try:                                  # IDN host, e.g. 中文.com → xn--...
            netloc = netloc.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            return ""
    try:
        path = quote(ps.path, safe="/%:+@,~")
        query = quote(ps.query, safe="=&%:+@,~/?")
        fragment = quote(ps.fragment, safe="%:+@,~/?")
    except Exception:  # noqa: BLE001
        return ""
    return urlunsplit((ps.scheme, netloc, path, query, fragment))


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
    # Clean every untrusted URL field before it can poison the embed: a malformed
    # url or image (spaces, no scheme, non-ASCII) makes Discord reject embed #0
    # with the field-agnostic {"embeds":["0"]}. Drop the field rather than the card.
    src = _clean_url_for_discord(item.source_url)
    img = _clean_url_for_discord(image)
    # author.name ≤256, title ≤256, description ≤4096; their sum (≤4608) is always
    # under Discord's 6000-char embed total, so clamping description is sufficient.
    return {"username": "BersamaAi", "embeds": [{
        "author": {"name": badge[:256]},
        "title": item.headline[:256],
        "url": src or None,
        "description": (f"**Why it matters**\n{item.body}{heat}")[:4096],
        "color": BRAND_COLOR,
        "image": {"url": img} if img else None,
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


def _flatten_discord_errors(node, prefix: str = "") -> list[str]:
    """Walk Discord's nested `errors` tree to readable 'path: message' strings.

    Discord's richer 400s look like {"errors": {"embeds": {"0": {"url": {"_errors":
    [{"code":..., "message":...}]}}}}}; recurse until each "_errors" leaf, joining
    path segments with '.' so the alert names the actual field (embeds.0.url)."""
    out: list[str] = []
    if isinstance(node, dict):
        if isinstance(node.get("_errors"), list):
            msgs = ", ".join(
                str(e.get("message") or e.get("code") or "")
                for e in node["_errors"] if isinstance(e, dict)
            )
            out.append(f"{prefix}: {msgs}" if prefix else msgs)
        for k, v in node.items():
            if k == "_errors":
                continue
            out.extend(_flatten_discord_errors(v, f"{prefix}.{k}" if prefix else str(k)))
    return out


def _discord_error_detail(r) -> str:
    """Turn a Discord error response into a field-level reason when possible.

    The webhook execute endpoint often replies with the terse `{"embeds":["0"]}`
    (embed #0 invalid — names NO field); when a richer `errors` tree is present we
    walk it so the alert can say 'embeds.0.url' instead of a useless '0'."""
    try:
        body = r.json()
    except ValueError:
        return (r.text or "")[:400]
    errs = body.get("errors") if isinstance(body, dict) else None
    if errs:
        flat = _flatten_discord_errors(errs)
        if flat:
            return "; ".join(flat)
    return str(body)[:400]


class DiscordHTTPError(RuntimeError):
    """Non-2xx webhook response. Carries the HTTP status so callers can treat 4xx
    (validation — safe to retry/degrade) differently from 5xx (may have created the
    message despite the error — must NOT retry, webhooks aren't idempotent)."""
    def __init__(self, status: int, detail: str):
        self.status = status
        super().__init__(f"HTTP {status}: {detail}")


# Bumped whenever _post_resilient recovers a post by dropping an embed image, so a
# systematic scraper/og:image regression is visible instead of silently posting
# thumbnail-less cards. Per-process (each run/share is its own process), so no reset.
image_drop_recoveries: int = 0


def _post(webhook_url: str, payload: dict) -> dict | None:
    """POST a webhook payload with ?wait=true so Discord returns the created
    message object (HTTP 200 + {id, channel_id}) instead of 204 No Content.
    Returns that message dict (used to log telemetry), or None if Discord 204'd
    despite wait=true — the post still succeeded, but there's no id to sweep so
    the caller skips logging. Raises DiscordHTTPError with Discord's parsed error
    detail on any other status (caller handles)."""
    sep = "&" if "?" in webhook_url else "?"
    r = requests.post(webhook_url + sep + "wait=true", json=payload, timeout=15)
    if r.status_code == 200:
        try:
            return r.json()
        except ValueError:
            return None
    if r.status_code == 204:
        return None
    raise DiscordHTTPError(r.status_code, _discord_error_detail(r))


def _post_resilient(webhook_url: str, payload: dict) -> dict | None:
    """Post a card; if Discord rejects it with a 4xx validation error, retry with
    the least-essential untrusted embed fields progressively dropped so one bad
    scraped og:image or model-mangled url never loses the whole card.

    Order: full payload → drop image → drop image AND url. The image is decorative
    (drop first); the title-url is useful but non-essential (drop next). 5xx errors
    are NOT retried — Discord may already have created the message on a 504-style
    failure, and webhook execute isn't idempotent, so retrying would double-post.
    Bumps ``image_drop_recoveries`` on an image-drop recovery so callers can alert
    on scraper regressions; logs the rejected embed on total failure so the next
    400 is diagnosable (Discord's terse {"embeds":["0"]} names no field)."""
    global image_drop_recoveries
    try:
        return _post(webhook_url, payload)
    except DiscordHTTPError as first:
        if first.status < 400 or first.status >= 500:
            raise   # 5xx (or weird 3xx): might have created the message — don't double-post
        for drop in (("image",), ("image", "url")):   # progressive degradation
            embeds = payload.get("embeds") or []
            if not any((e or {}).get(f) for e in embeds for f in drop):
                continue   # nothing in this field to drop; try the next stage
            attempt = json.loads(json.dumps(payload))   # deep copy; fields are nested per embed
            for e in attempt.get("embeds", []):
                if isinstance(e, dict):
                    for f in drop:
                        if e.get(f):
                            e[f] = None
            try:
                msg = _post(webhook_url, attempt)
            except DiscordHTTPError:
                continue   # this field wasn't the (only) culprit — try the next stage
            if "image" in drop:
                image_drop_recoveries += 1
            print(f"[post] recovered after dropping embed {drop} — original error: {first}")
            return msg
        # Every recovery failed — log the embed we sent so the next 400 is diagnosable,
        # then surface the original (most informative) error.
        try:
            print(f"[post] embed rejected on every attempt; embed0="
                  f"{json.dumps(payload.get('embeds', [{}])[0])[:500]}")
        except Exception:  # noqa: BLE001
            pass
        raise


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

Assign exactly one TOPIC (by its literal key). SUBJECT WINS OVER CATEGORY:
- coding — LLM / frontier-model releases, coding agents, dev tools, MCP / agent-skill
  tooling, inference tooling, general agent + robotics PRODUCT launches. DEFAULT catch-all
  for the agent/LLM beat; NOT investment, cyber, money, or research. A coding assistant
  that adds a security FEATURE stays here (purpose is still coding).
- company_investment — AI INDUSTRY business / money / strategy / org / policy: acquisitions
  & M&A (a creative-AI company being acquired = here — the deal is the story), funding
  rounds, IPO, big investments, compute + chip-capacity deals (GPU clusters, custom-silicon
  programs), PRICING changes (even for a creative tool — pricing = strategy), CEO /
  leadership / reorg moves, company open-weight POLICY stances + industry open-letter
  coalitions, AI fund / stock / earnings market moves. NOT a new thing shipping.
- cybersecurity — AI SECURITY as the SUBJECT: hacking incidents, jailbreaks / safety-bypass,
  models escaping eval sandboxes, red-teaming, AI-found vulnerabilities, AI cryptanalysis,
  supply-chain intrusions of AI infra, AND cybersecurity-PURPOSE model/tool releases (a
  Cyber model, a pentest agent) -> here even though they are also Launches. A deepfake-voice
  fraud incident = here, not creative_voice.
- creative_image — image generation
- creative_video — video generation + AI editing
- creative_voice — voice / audio / TTS / music
- research_study — research papers, academic studies, science-publishing norms, from-scratch
  / learning material. A research finding or paper = here even when about agents. Tie-break:
  a paper that SHIPS an attack/defense artifact (jailbreak repo, pentest harness) =
  cybersecurity; one that STUDIES / MEASURES = here.
- research_productivity — productivity / knowledge-work AI tools, deep-research agents,
  notes / second-brain / document tools.
- finance — the INDIVIDUAL / BUILDER earning WITH AI: side income, indie / SaaS / vibe-coding
  revenue, autonomous-business runs, AI trading / quant / prediction-market plays. NOT
  institutional / market flows (a fund collapsing, index moves, an M&A deal) -> those are
  company_investment.

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
                     dry_run: bool = False, alert_fn=None, force: bool = False) -> str:
    """Threads/link HITL: fetch a public URL -> GLM writes a topic-tagged card -> post.
    `force` overrides the same-link guard (owner prefixing the URL with '!')."""
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
    # source_url: the owner vouches for the URL they pasted, so trust it over the
    # model's value. The prompt tells the model to "pass through unchanged" but GLM
    # sometimes alters it — and a *valid-but-wrong* model URL would silently link the
    # card to a different story. Clean it; fall back to the raw url only if cleaning
    # ever empties it.
    source_url = _clean_url_for_discord(url) or url
    item = NewsItem(
        topic=topic,
        category=str(data.get("category", "UPDATE")).strip(),
        headline=str(data.get("headline", "")).strip(),
        body=str(data.get("body", "")).strip(),
        source_url=source_url,
        heat_reason="📣 shared by the owner",
    )
    # Same-link guard: refuse an exact (normalized) re-share of a URL already posted via
    # /share, so a double-submit can't post the same card twice. /share is an explicit
    # editor action, so the owner overrides by prefixing the URL with '!'.
    if not force:
        norm = _norm_url(item.source_url)
        if norm and any(_norm_url(r.get("source_url") or "") == norm
                        for r in read_jsonl(POSTED_LOG_SHARE)):
            msg = (f"SHARE_ALREADY_POSTED — already shared (norm {norm}). "
                   f"Prefix the URL with '!' to force a re-post.")
            print(f"[share] {msg}")
            return msg
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
    _img_before = image_drop_recoveries
    try:
        msg = _post_resilient(wh, payload)
    except Exception as e:  # noqa: BLE001
        if alert_fn:
            alert_fn(f"share post failed: {item.headline[:60]}: {e}", dry_run)
        return f"SHARE_POST_FAILED {e}"
    # If the card only went out after Discord rejected its image URL, tell the owner
    # (low volume, owner-initiated) — otherwise a thumbnail-less card looks fine and
    # the broken-image-URL cause stays invisible.
    if image_drop_recoveries > _img_before and alert_fn:
        alert_fn(f"/share card posted without its thumbnail — Discord rejected the image URL "
                 f"so it was dropped (card still posted). Card: {item.headline[:60]}", dry_run)
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
    seen |= _share_seen_keys()   # owner's /share cards (VM shard) also count as seen
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

    # Same-story memory for the ONE STORY, ONE CARD rule: the last ~48h of posted
    # headlines, so the judge can suppress a re-report of an already-covered event
    # (different source/URL/headline, same news) while still posting a genuinely
    # different storyline about the same entity.
    recent = _recent_posted_headlines()
    if recent:
        print(f"[same-story] {len(recent)} recently-posted headline(s) fed to the judge "
              f"(window {RECENT_STORY_WINDOW_H}h) for cross-run ONE-STORY-ONE-CARD dedup")

    if stub:
        items = [NewsItem(LIVE_TOPICS[0].key, "LAUNCH", "[STUB] a hot item",
                          "Canned item for local testing (no API key).",
                          candidates[0]["url"], "stub")]
    else:
        try:
            items = judge(candidates, api_key=api_key, model=model, base_url=base_url,
                          prefs_section=prefs.profile_section, recent=recent)
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
                msg = _post_resilient(wh, payload)
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
    # Scraper-regression signal: if several cards only went out after Discord rejected
    # their image URL (so they posted thumbnail-less via _post_resilient), the image
    # fetcher / og:image pipeline is likely broken — surface it before the feed goes
    # silently bare. One-off recoveries are noise; >=2 is a pattern.
    if image_drop_recoveries >= 2:
        _staff_alert(
            f"⚠️ **{image_drop_recoveries} news cards posted without a thumbnail** — Discord kept "
            "rejecting image URLs, so _post_resilient dropped them (cards still posted). A bare "
            "feed usually means _fetch_image / microlink is returning bad URLs — investigate.",
            dry_run)
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
