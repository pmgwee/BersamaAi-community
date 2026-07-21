"""GitHub Trending source — trending repos via the GitHub Search API.

GitHub has no official trending API, so we use the Search API as a "trending
proxy": repos CREATED in the last `since_days` (new this week) with >`min_stars`,
sorted by stars. `created:` (not `pushed:`) is the key — `pushed:` surfaces active
incumbents (LangChain pushes daily), while `created:` + high stars = genuinely new
repos that are blowing up, i.e. the real "supernewstar" signal.

Velocity: we also persist each repo's star count in state/github_stars.json across
runs and tag candidates whose stars rose since the last run (`cand["star_delta"]`) —
that's the true "rushing stars" signal, independent of total count. Needs 2 runs
to establish a baseline (first run just records; deltas appear from the 2nd).

Auth: optional GITHUB_TOKEN raises the rate limit. The Search endpoint is capped
at ~10 req/min unauthenticated — we sleep between queries so a small per-run
volume stays well under that.
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path

import requests

SEARCH_URL = "https://api.github.com/search/repositories"
STARS_STATE = Path(__file__).resolve().parent.parent / "state" / "github_stars.json"


def _load_stars() -> dict:
    if not STARS_STATE.exists():
        return {}
    try:
        return json.loads(STARS_STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_stars(d: dict) -> None:
    STARS_STATE.parent.mkdir(parents=True, exist_ok=True)
    # bound: keep the 1000 highest-star repos so the file doesn't grow forever
    top = dict(sorted(d.items(), key=lambda kv: -kv[1])[:1000])
    STARS_STATE.write_text(json.dumps(top, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_trending(
    keywords: list[str],
    *,
    min_stars: int = 200,
    since_days: int = 7,
    per_query: int = 15,
    token: str = "",
) -> list[dict]:
    """Return candidate repo dicts for the given keywords (created this week, high stars).

    Each dict also carries `star_delta` (stars gained since the last run) when
    available, so downstream can surface genuinely fast-rising repos.
    """
    since = (date.today() - timedelta(days=since_days)).isoformat()
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "BersamaAi-news/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    out: list[dict] = []
    for kw in keywords:
        q = f"{kw} stars:>{min_stars} created:>{since}"
        try:
            r = requests.get(
                SEARCH_URL,
                params={"q": q, "sort": "stars", "order": "desc", "per_page": per_query},
                headers=headers, timeout=20,
            )
            if r.status_code != 200:
                print(f"[github] {q!r} -> {r.status_code} {r.text[:120]}")
                continue
            for item in r.json().get("items", []) or []:
                full = item.get("full_name", "")
                if not full:
                    continue
                out.append({
                    "title": full,
                    "url": item.get("html_url", ""),
                    "discussion": item.get("html_url", ""),
                    "source": "github",
                    "score": int(item.get("stargazers_count") or 0),
                    "snippet": (item.get("description") or "")[:300],
                    "language": item.get("language") or "",
                    "thumbnail": (item.get("owner") or {}).get("avatar_url", ""),
                })
        except Exception as e:  # noqa: BLE001 — one query failing shouldn't kill the run
            print(f"[github] {q!r} failed: {e}")
        time.sleep(3)  # respect the Search rate limit (30/min authenticated, 10/min unauth)

    # velocity: tag repos whose stars rose since the last run, then persist current counts
    prev = _load_stars()
    updated = dict(prev)
    for c in out:
        repo, cur = c["title"], c["score"]
        p = prev.get(repo)
        if p is not None and cur > p:
            c["star_delta"] = cur - p
        updated[repo] = cur
    _save_stars(updated)
    return out
