"""Trending AI news aggregator → #subscription-value.

Polls free, no-auth sources (Reddit AI subs + Hacker News), an LLM judge picks
the few items genuinely worth a Malaysian mass-market AI community's attention
(launches, pricing/resets, benchmarks, open-source releases), and posts each as
a category-tagged card to Discord.

Twitter/X is intentionally NOT a source: its API is paid ($200/mo Basic) and
scraping is fragile. The same news reaches Reddit/HN within hours. An X API key
can be wired in later as an optional extra source.

Dedup: state/news_seen.json — a list of seen keys (url or title hash), committed
back to the repo like state/processed.json. Anything already seen is skipped.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import requests
from openai import OpenAI

STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "news_seen.json"
HEADERS = {"User-Agent": "BersamaAi-news/1.0 (community bot)"}

REDDIT_SUBS = ["LocalLLaMA", "singularity", "OpenAI", "ClaudeAI"]
HN_TOPN = 30          # pull top-N HN stories, then keyword-filter locally
LOCAL_LIMIT = 24      # max candidates sent to the LLM judge per run
MAX_POST_PER_RUN = 4  # never spam the channel

# Local pre-filter: only keep items that look AI-relevant (cheap, before the LLM).
AI_KEYWORDS = (
    "ai", "llm", "gpt", "chatgpt", "claude", "anthropic", "openai", "gemini",
    "deepmind", "grok", "xai", "elon", "kimi", "moonshot", "deepseek", "qwen",
    "glm", "z.ai", "zhipu", "mistral", "llama", "meta ai", "muse", "sora", "veo",
    "model", "benchmark", "open source", "open-source", "open weight", "open-weight",
    "api", "subscription", "pricing", "reset", "rate limit", "credits", "free tier",
    "agent", "reasoning", "frontier",
)

GLM_DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
GLM_DEFAULT_MODEL = "glm-4.6"

SYSTEM_PROMPT = """\
You are the news editor for BersamaAi, a Malaysian mass-market AI community's
#subscription-value channel. You receive a batch of candidate AI items pulled
from Reddit and Hacker News. Your job: pick the FEW that are genuinely worth a
busy member's attention right now, and write each as a tight, sober, no-hype card.

What qualifies (be selective — most candidates are NOT worth posting):
- A new model or major product LAUNCH (e.g. "Grok 4.5 released", "Kimi K3 ships").
- PRICING or access changes (e.g. "Fable 5 added to Max at 50% limits", a price cut).
- USAGE RESETS / rate-limit / free-tier changes that affect subscribers.
- A BENCHMARK record or ranking shake-up (e.g. "Kimi K3 hits #1").
- A notable OPEN-SOURCE / open-weight release.
Skip: rumors with no source, drama/flamewars, opinion hot-takes, niche dev trivia,
security scare-stories without confirmation, duplicates.

For each picked item, write:
- category: one of LAUNCH | PRICING | USAGE_RESET | BENCHMARK | OPEN_SOURCE | DEAL.
- headline: one crisp line (<= 110 chars), the news itself (no clickbait).
- body: 1-2 sentences — what happened + why it matters to an ordinary Malaysian
  AI user (subscription value, a cheaper/free alternative, something to try).
  Ground it in the candidate text; never invent facts or numbers.
- source_url: the candidate's url (pass through unchanged).
- when: a short relative time label if available (e.g. "today", "this week"),
  else "".

English only. If nothing in the batch is worth posting, return items: [].
"""

EMIT_NEWS_TOOL = {
    "name": "emit_news",
    "description": "Emit the selected news items to post. Call exactly once.",
    "input_schema": {
        "type": "object",
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "required": ["category", "headline", "body", "source_url"],
                    "properties": {
                        "category": {"type": "string",
                                     "enum": ["LAUNCH", "PRICING", "USAGE_RESET",
                                              "BENCHMARK", "OPEN_SOURCE", "DEAL"]},
                        "headline": {"type": "string", "maxLength": 200},
                        "body": {"type": "string", "maxLength": 600},
                        "source_url": {"type": "string"},
                        "when": {"type": "string", "maxLength": 40},
                    },
                },
            }
        },
    },
}


@dataclass
class NewsItem:
    category: str
    headline: str
    body: str
    source_url: str
    when: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class NewsError(Exception):
    pass


# ── sources ──────────────────────────────────────────────────────────────────

def _looks_ai(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in AI_KEYWORDS)


def fetch_reddit() -> list[dict]:
    """Hot items from a few AI subreddits. Returns candidate dicts."""
    out = []
    for sub in REDDIT_SUBS:
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
                # prefer the external link if it's not a self-post
                link = ext if ext and "reddit.com" not in ext else perma
                snippet = (d.get("selftext") or "")[:300]
                out.append({
                    "title": title,
                    "url": link,
                    "discussion": perma,
                    "source": f"r/{sub}",
                    "score": int(d.get("score") or 0),
                    "snippet": snippet,
                })
        except Exception:  # noqa: BLE001 — one sub failing shouldn't kill the run
            continue
        time.sleep(0.5)  # be polite to reddit
    return out


def fetch_hn() -> list[dict]:
    """Top HN stories, keyword-filtered to AI. Returns candidate dicts."""
    out = []
    try:
        ids = requests.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json",
            headers=HEADERS, timeout=15).json()[:HN_TOPN]
    except Exception:  # noqa: BLE001
        return out
    for i in ids:
        try:
            it = requests.get(
                f"https://hacker-news.firebaseio.com/v0/item/{i}.json",
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
            "source": "HN",
            "score": int(it.get("score") or 0),
            "snippet": "",
        })
    return out


def gather_candidates() -> list[dict]:
    """All candidates, AI-relevant, sorted by score, capped to LOCAL_LIMIT."""
    cand = fetch_reddit() + fetch_hn()
    cand = [c for c in cand if _looks_ai(c["title"]) or _looks_ai(c["snippet"])]
    cand.sort(key=lambda c: c.get("score", 0), reverse=True)
    return cand[:LOCAL_LIMIT]


# ── LLM judge ────────────────────────────────────────────────────────────────

def _build_judge_user_message(candidates: list[dict]) -> str:
    lines = [f"CANDIDATES ({len(candidates)}):"]
    for i, c in enumerate(candidates, 1):
        lines.append(
            f"\n[{i}] source={c['source']} score={c['score']}\n"
            f"title: {c['title']}\nurl: {c['url']}"
            + (f"\nexcerpt: {c['snippet']}" if c["snippet"] else "")
        )
    lines.append(
        "\nPick the few worth posting to #subscription-value. "
        "Call emit_news with the selected items (or items: [] if none qualify)."
    )
    return "\n".join(lines)


def judge(
    candidates: list[dict], api_key: str, model: str, base_url: str
) -> list[NewsItem]:
    """Force a GLM emit_news tool call; return validated NewsItems (may be empty)."""
    if not api_key:
        raise NewsError("ZAI_API_KEY is missing — cannot judge news.")
    client = OpenAI(api_key=api_key, base_url=base_url)
    tool = {"type": "function", "function": {
        "name": EMIT_NEWS_TOOL["name"],
        "description": EMIT_NEWS_TOOL["description"],
        "parameters": EMIT_NEWS_TOOL["input_schema"],
    }}
    try:
        resp = client.chat.completions.create(
            model=model, max_completion_tokens=2048,  # Z.ai GLM ignores max_tokens
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_judge_user_message(candidates)},
            ],
            tools=[tool], tool_choice="required",
        )
    except Exception as e:  # noqa: BLE001
        raise NewsError(f"GLM API call failed: {e}") from e

    msg = resp.choices[0].message
    tc = getattr(msg, "tool_calls", None)
    if not tc:
        return []
    try:
        data = json.loads(tc[0].function.arguments or "{}")
    except (json.JSONDecodeError, TypeError) as e:
        raise NewsError(f"emit_news returned invalid JSON: {e}") from e

    out = []
    for raw in (data.get("items") or [])[:MAX_POST_PER_RUN]:
        try:
            out.append(NewsItem(
                category=str(raw.get("category", "LAUNCH")).strip(),
                headline=str(raw.get("headline", "")).strip(),
                body=str(raw.get("body", "")).strip(),
                source_url=str(raw.get("source_url", "")).strip(),
                when=str(raw.get("when", "")).strip(),
            ))
        except Exception:  # noqa: BLE001
            continue
    return out


# ── dedup state ───────────────────────────────────────────────────────────────

def _load_seen() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return set()


def _save_seen(seen: set[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # keep the file bounded — only the most recent 500 keys
    recent = sorted(seen)[-500:]
    STATE_FILE.write_text(json.dumps(recent, ensure_ascii=False, indent=2), encoding="utf-8")


def _key(item: NewsItem, candidate_by_url: dict) -> str:
    """Stable dedup key: prefer the matched candidate's discussion url, else headline hash."""
    c = candidate_by_url.get(item.source_url)
    base = (c["discussion"] if c else item.source_url) or item.headline
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


# ── orchestration ────────────────────────────────────────────────────────────

CATEGORY_EMOJI = {
    "LAUNCH": "🚀", "PRICING": "💰", "USAGE_RESET": "♻️",
    "BENCHMARK": "📊", "OPEN_SOURCE": "🔓", "DEAL": "🎁",
}
BRAND_COLOR = 0x5865F2


def build_news_payload(item: NewsItem) -> dict:
    emoji = CATEGORY_EMOJI.get(item.category, "📡")
    when = f" · {item.when}" if item.when else ""
    desc = f"{item.body}\n\n🔗 {item.source_url}"
    return {
        "username": "BersamaAi",
        "embeds": [{
            "title": f"{emoji} {item.category.replace('_', ' ')}{when} — {item.headline}"[:256],
            "description": desc[:4096],
            "url": item.source_url or None,
            "color": BRAND_COLOR,
            "footer": {"text": "BersamaAi · trending AI moves"},
        }],
    }


def run_news(
    *, dry_run: bool, stub: bool,
    api_key: str, model: str, base_url: str,
    webhook_url: str, alert_fn=None,
) -> list[str]:
    """Full news run. Returns a list of one-line status strings."""
    print("\n=== news run ===")
    candidates = gather_candidates()
    print(f"gathered {len(candidates)} AI-relevant candidates")
    if not candidates:
        return ["NEWS_NO_CANDIDATES"]

    if stub:
        items = [NewsItem("LAUNCH", "[STUB] A notable AI launch happened",
                          "This is a canned news item for local testing (no API key).",
                          candidates[0]["url"], "today")]
    else:
        try:
            items = judge(candidates, api_key, model, base_url)
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
        payload = build_news_payload(item)
        if dry_run:
            print(f"\n[discord DRY-RUN] news\n{payload}\n")
        elif webhook_url:
            try:
                r = requests.post(webhook_url, json=payload, timeout=15)
                if r.status_code not in (200, 204):
                    raise RuntimeError(f"{r.status_code} {r.text[:200]}")
            except Exception as e:  # noqa: BLE001
                if alert_fn:
                    alert_fn(f"news post failed: {item.headline[:60]}: {e}", dry_run)
                results.append(f"NEWS_POST_FAILED {item.headline[:40]}")
                continue
        else:
            print("[discord] skipped — no DISCORD_NEWS_WEBHOOK_URL set")
        seen.add(key)
        posted += 1
        results.append(f"NEWS_POSTED {item.headline[:40]}")
        if posted >= MAX_POST_PER_RUN:
            break

    if not dry_run:
        _save_seen(seen)
    print(f"\nposted {posted} news item(s)")
    return results
