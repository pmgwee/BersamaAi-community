"""GitHub Trending source — trending repos via the GitHub Search API.

GitHub has no official trending API, so we use the Search API as a "trending
proxy": repos CREATED in the last `since_days` (new this week) with >`min_stars`,
sorted by stars. `created:` (not `pushed:`) is the key — `pushed:` surfaces active
incumbents (LangChain pushes daily), while `created:` + high stars = genuinely new
repos that are blowing up, i.e. the real "supernewstar" signal.

Auth: optional GITHUB_TOKEN raises the rate limit. The Search endpoint is capped
at ~10 req/min unauthenticated — we sleep between queries so a small per-run
volume stays well under that.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import requests

SEARCH_URL = "https://api.github.com/search/repositories"


def fetch_trending(
    keywords: list[str],
    *,
    min_stars: int = 200,
    since_days: int = 7,
    per_query: int = 15,
    token: str = "",
) -> list[dict]:
    """Return candidate repo dicts for the given keywords (created this week, high stars).

    Each dict is shaped like the Reddit/HN candidates so the news judge can treat
    them uniformly: {title, url, discussion, source, score, snippet}.
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
    return out
