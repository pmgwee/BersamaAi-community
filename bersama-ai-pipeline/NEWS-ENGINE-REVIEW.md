# News Engine Review — Topic-Routed Digest vs. "Live Viral Tracker" Goals
*Investigated 2026-07-21 against `pipeline/news.py` + `pipeline/github_trending.py` as of commit `fcc4544`.*
*Hand this file to the enhancement agent as the work spec.*

---

## 0. The owner's actual requirement (restated)

The engine's job is to act as a **live tracker of the most viral, trending AI things**,
the way the owner's Threads feed surfaces them. Three concrete capture targets:

1. **Trending topics** on Reddit, Threads (social), and discussion forums.
2. **Viral new AI product launches** from official sources (e.g. Kimi K3, Grok Build) —
   caught early, not days later.
3. **"Supernewstar" GitHub repos** — skills/plugins/tools gaining stars at unusual
   *velocity* this week (e.g. `agent-reach`, `tirth8205/code-review-graph`,
   `tradingview-mcp`, `every-app/open-seo`) — NOT established giants that are merely active.

The 3-hour schedule is fine. **Selection quality is the requirement, not cadence.**
Benchmark: "out of 50 Threads posts, the 1–3 worth sharing to `#ai-dev-tools`."

---

## 1. Verdict summary

| # | Requirement | Verdict | Root cause |
|---|---|---|---|
| 1 | Reddit/forum trending | 🟡 Partial | Works, but unauthenticated Reddit JSON (403-risk from GCP IPs) + candidates get crowded out (see §2.3) |
| 1b | Threads trending | 🔴 Missing | No Threads source exists anywhere in the engine |
| 2 | Official-source launches | 🟡 Secondhand only | No official sources polled (no blogs/RSS/HuggingFace); catches launches only after Reddit/HN react |
| 3 | Supernewstar velocity repos | 🔴 **Broken by design** | Query sorts by *total* stars; velocity is never measured; giants dominate (see §2.1) |

"Verified in dry-run" verified the **plumbing** (fetch → judge → format → route → post).
It could not verify **selection quality**, because the problem is what enters the
candidate pool, not whether posting works.

---

## 2. Detailed findings (file + line specific)

### 2.1 🔴 The GitHub query structurally cannot find supernewstars

`github_trending.py` builds: `q = f"{kw} stars:>{min_stars} pushed:>{since}"`,
`sort=stars, order=desc` (lines 43–48).

- `pushed:>7d` = *any commit in the last week*. Every actively-maintained giant
  (langchain, transformers, ollama…) matches every run, forever.
- `sort=stars desc` returns the **highest-total-star** matches first. A repo born
  3 weeks ago with 800 stars (gained 700 this week — a true supernewstar) loses to
  a 4-year-old repo with 90k stars (gained 200 this week) on every query.
- **Star velocity appears nowhere in the data.** No `created_at` is captured, no
  star history is stored, no delta is computed.

Downstream effect: the judge's prompt asks for heat reasons like *"12k stars in
3 days"* — the judge **cannot know this**; it only sees a current total. The code
even acknowledges LLM heat text is unreliable (`_metric()` in `news.py` overwrites
`heat_reason` with the real score) — but the real score is a *total*, so the "heat"
shown is still not velocity.

### 2.2 🔴 No Threads source

Requirement 1 names Threads explicitly as the highest-signal source (the owner's
feed algorithm already curates AI virality). Nothing in `news.py` touches Threads.
See §3.4 for why full automation is NOT the recommended fix.

### 2.3 🟠 Cross-source crowding: one raw-score sort before the judge

`gather_candidates()` (news.py lines 173–189) concatenates GitHub + Reddit + HN,
sorts **all together by raw score**, and truncates to `LOCAL_LIMIT = 40`.
Scores are incommensurable: GitHub totals (tens of thousands) ≫ Reddit upvotes
(hundreds) ≫ HN points. Result: GitHub giants (already the wrong repos per §2.1)
push most Reddit/HN items out of the judge's view entirely. On a typical run the
judge may see 30+ GitHub rows and only a handful of Reddit/HN rows.

### 2.4 🟠 No official launch sources

No RSS/blog polling (OpenAI, Anthropic, Google/DeepMind, xAI, Moonshot/Kimi,
DeepSeek, Qwen), no Hugging Face trending-models feed. A "Kimi K3 released" event
is only caught if/after it trends on r/LocalLLaMA or HN. Usually that works within
hours — but it is secondhand, adds delay, and misses consumer-facing launches that
go viral on Threads/X without a strong Reddit thread.

### 2.5 🟡 Reddit fetch reliability

`fetch_reddit()` hits `www.reddit.com/*.json` unauthenticated with a custom UA.
Reddit rate-limits/403s datacenter IPs (GCP included) unpredictably. Failures are
swallowed (`except: continue`) — a fully-blocked run looks identical to a quiet
news day. No visibility, no alert.

### 2.6 🟡 Minor

- `MAX_POST_PER_RUN = 6` is global, not per-channel — one hot topic can starve others (fine while only `coding` is live; matters when creative/research go live).
- Dedup keys on discussion-URL hash — same story from HN *and* Reddit posts twice (different URLs). Acceptable; note only.
- `LOCAL_LIMIT=40` combined with `time.sleep(3)` per GitHub keyword query means 6 topics × ~5 keywords ≈ 90s of sleeps per run when all topics go live — fine on a VM, but worth knowing.

---

## 3. Fix spec (priority order)

### 3.1 P0 — True supernewstar detection (two mechanisms, both cheap)

**(a) New-repo explosion query.** Alongside the existing query, add per topic:
`q = f"{kw} created:>{(today - 14d).isoformat()} stars:>100"` sorted by stars.
Anything young with real stars IS high velocity by definition (800 stars ÷ 10 days
of existence). Tag these candidates `source="github-new"` so the judge sees they're new;
capture `created_at` and include "created N days ago, ⭐X" in the candidate line.

**(b) Star-delta tracking between runs.** New state file `state/github_stars.json`:
`{full_name: {"stars": int, "ts": iso}}`. Each run, for every repo fetched:
`velocity = (stars_now - stars_prev) / max(hours_elapsed/24, 0.5)`. Emit a
`velocity` field on the candidate; let anything with `velocity > ~150 stars/day`
bypass the score sort entirely (always reaches the judge). Now `_metric()` can print
the real thing: `⭐ +1.2k stars this week (4.1k total)`. Prune entries not seen for 30 days.

**(c) Optional, best visual parity with "what people see":** parse
`https://github.com/trending?since=daily` (server-rendered HTML, stable selectors,
one request; there are also maintained JSON mirrors). That page IS star-velocity-ranked.
Treat it as a third GitHub candidate source, keyword-filtered per topic.

### 3.2 P0 — Per-source quotas before the judge

Replace the single raw-score sort+truncate in `gather_candidates()` with quotas,
e.g. per run: `github-new: 8, github-velocity: 8, github: 8, reddit: 12, hn: 8`
(cap still ~40–44 total). Sort *within* each source only. This guarantees the judge
always sees every world. Keep the AI-keyword pre-filter as-is.

### 3.3 P1 — Official-source launch watch

Add `fetch_official()` polling RSS/Atom (all free, no auth):
- OpenAI news feed · Anthropic newsroom · Google DeepMind blog · blog.google (AI tag)
- xAI news · Qwen blog · DeepSeek (announcements) · Moonshot/Kimi (platform notes)
- **Hugging Face**: `GET https://huggingface.co/api/models?sort=likes7d&limit=20` —
  the cleanest early machine-readable signal for hot new model drops (works unauthenticated).
Candidates tagged `source="official"` / `"huggingface"`, given their own quota (~6),
and the judge prompt extended: official-source items rank as LAUNCH/RELEASE even
with no Reddit score yet ("official announcement" is itself the heat signal).
Dedup naturally prevents re-posting when the same launch later trends on Reddit.

### 3.4 P1 — Threads: human-in-the-loop share flow, NOT scraping

Investigated and recommended **against** automating the owner's personalized feed:
- Meta's official Threads API exposes *your own posts/replies/insights* and keyword
  search — **not your personalized For-You timeline**. The feed the owner values is
  exactly the thing the API does not serve.
- Cookie/session scraping of a logged-in feed violates Meta ToS and risks the
  account, and breaks silently whenever markup changes. Wrong risk for a community
  whose brand is trust.

**Do instead — make the owner's 5-second curation the feature:**
The owner already curates (50 posts → 1–3 winners). Build the friction-free relay:
1. Extend the existing phone-friendly on-demand trigger (`on_demand.py`) with a
   `/share?url=<threads-post-url>` endpoint (same auth token pattern).
2. Server-side: fetch the post's public page (single public-URL fetch of a post the
   owner explicitly chose — not feed scraping), extract text + first link + image;
   if a GitHub/product link is present, enrich it (stars via API, description).
3. GLM formats it into the standard card (§ current `build_news_payload` style) and
   posts to `#ai-dev-tools` (or topic-routed by the judge).
4. Phone UX: iOS/Android **share sheet → bookmark/shortcut** hitting the endpoint.
   See a post → share → done. Card appears in Discord in ~10s.

This keeps the owner as the taste algorithm (which beats GLM at "would my community
care") and the pipeline as the formatter/publisher — division of labor that matches
reality.

### 3.5 P2 — Reddit OAuth + failure visibility

- Register a free Reddit "script" app; client-credentials flow;
  `Authorization: Bearer` against `oauth.reddit.com`. Rate limit becomes a
  contractual 100 QPM instead of IP luck.
- Track per-source fetch counts each run; if a source returns 0 candidates
  **twice consecutively**, fire the existing `alert_fn` (Telegram DM) —
  distinguishes "quiet news day" from "we're blocked."

### 3.6 P2 — Per-channel post cap

`MAX_POST_PER_RUN` → per-topic (e.g. 3/topic/run) once creative/research channels go live.

---

## 4. What NOT to change

- 3-hour cadence — fine; the owner explicitly deprioritized cadence.
- GLM judge + forced tool-call schema — sound pattern, keep.
- `_metric()` real-score override — right instinct; feed it velocity data (§3.1b) and it becomes fully honest.
- Dedup/state/commit-back mechanics — solid.
- Card format — matches the seeded-resources look; keep.

## 5. Acceptance test (run for one week after implementing)

Daily dry-run review against the owner's own Threads feed:
1. Did the engine surface the 1–3 things the owner *would have* hand-picked? (target: ≥2 of 3)
2. Zero giant-repo noise (langchain/transformers-class repos absent unless a genuine
   RELEASE event occurred)?
3. Any official launch (model/product) captured within one cycle (≤3h of the announcement)?
4. Reddit fetch success rate 100% (OAuth) with per-source counts logged?
