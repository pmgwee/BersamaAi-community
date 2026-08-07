# BersamaAi Summarization Pipeline

The recurring content engine for the BersamaAi Malaysia AI community. **Four modes:**

1. **Talk summarizer** — turns a YouTube talk into a 5-point summary ~09:03 MYT daily, auto-publishes to **Discord** (`#youtube-ai-video`) and (optionally) **Telegram**, and stages a ready-to-paste bundle for Threads / Facebook.
2. **Trending news digest** — gathers per-topic Reddit subs + Hacker News + GitHub Trending + HuggingFace trending + official lab/newsroom RSS every ~3h; an LLM judge tags each item with a topic + heat and routes it to that topic's channel (`#ai-llm-tools`, `#image-creation`, `#video-creation-aigc-tvc`, `#voice-studio`, `#research-with-ai`, `#earn-money-with-ai`, `#ai-company-investment`, `#ai-cybersecurity-bypass`).
3. **On-demand portal + `/share`** — a phone-friendly web UI (`on_demand.py`, port 8080) to summarize one URL (`/run`) or share any URL as a news card (`/share`).
4. **`@EconomyApp` stock digest** — mirrors an X account via its **Bluesky mirror** (public AT Protocol API — free, keyless, cookieless, IP-agnostic) and posts the day's posts to `#stock-financial-report`. No LLM (the post text IS the card); VM cron ~09:00 MYT daily.

**Runs split across two clouds** (see [`PROJECT-CONTEXT.md`](../PROJECT-CONTEXT.md) for the full picture):
- **GCP VM** — creator-watch summarizer (daily ~09:03 MYT) + the on-demand portal + `/share` + the `@EconomyApp` stock digest (cron daily ~09:00 MYT). YouTube bot-blocks Azure/GitHub-Actions datacenter IPs but not GCP, and the VM already hosts the always-on bot.
- **GitHub Actions** — the news digest + engagement loop (`.github/workflows/news-digest.yml`, every 3h) and the weekly engagement digest (`engagement-digest.yml`, Sun 22:23 UTC → one analytics card to `🔒-staff-chat`).

Card *content* keeps the source's original language (Chinese source → Chinese card); channel names / UI / the small category badge stay English.

```
PIPELINE 1 — talk summarizer (GCP VM)
  YouTube talk
     │  [fetch]    yt-dlp captions (json3) — fallback: youtube-transcript-api
     │  [asr]      no captions? → yt-dlp audio → ffmpeg → Groq Whisper
     │  [summarize] GLM glm-5.2 · forced tool call → exactly 5 points
     │  [gate]     short/bad transcript? → content/_review/, do NOT publish
     │  [publish]  Discord webhook (embed) + Telegram  ── auto
     │  [stage]    content/<date>_<slug>_<id>/ (post.threads.txt, post.facebook.txt) ── manual paste
     ▼  [state]    state/processed.json (dedup by video_id)

PIPELINE 2 — trending news digest (GitHub Actions, every ~3h)
  Reddit (per-topic subs) + Hacker News + GitHub Trending (star-velocity gate)
  + HuggingFace trending (boombastic only) + official lab/newsroom RSS
     │  [gather]  AI-relevant candidates (keyword pre-filter, score-ranked);
     │            per-topic quotas so every channel's domain reaches the judge
     │  [judge]   GLM tags TOPIC + HEAT, picks 0–N worth posting — sober, no hype
     │  [dedupe]  state/news_seen.json (before quotas + judge — no slot wasted on a repeat)
     ▼  [publish] Discord webhook for the topic's channel (coding → #ai-llm-tools, …)
     │  [health]  a real run that posts 0 cards warns 🔒-staff-chat (no silent failures)

PIPELINE 3 — on-demand portal + /share (GCP VM, manual)
  phone bookmark http://<VM_IP>:8080/?token=…
     │  /run     summarize a YouTube URL → #youtube-resources   (mode = url)
     ▼  /share   any URL (IG Reels / XHS / TikTok / Threads → yt-dlp + Groq Whisper)
                  → GLM builds a topic card → that topic's channel webhook  (mode = share)

PIPELINE 4 — @EconomyApp stock digest (VM cron, daily ~09:00 MYT)
  the X account's Bluesky mirror (public AT Protocol API — free, keyless, IP-agnostic)
     │  [fetch]   newest posts (images carried) — NO LLM, no auth, no cookie to expire
     │  [dedupe]  state/x_seen_<screen>.json   (MAX_AGE_DAYS=7; stale feed alerts staff)
     ▼  [publish] DISCORD_STOCK_FINANCIAL_REPORT_WEBHOOK_URL → #stock-financial-report   (mode = x-digest)
```

## Setup

### 1. Install (local dev / testing)
```powershell
cd bersama-ai-pipeline
python -m pip install -r requirements.txt
python -m pip install -U yt-dlp
```
> On this machine `pip` points at a different venv — always use **`python -m pip`**.

### 2. Provision secrets
Copy [`.env.example`](.env.example) → `.env` and fill it in. Every key is tagged with where it's needed:
- `[VM]` — the GCP pipeline VM (summarizer + on-demand portal + `/share`): `ZAI_API_KEY`, `GROQ_API_KEY`, `DISCORD_YOUTUBE_WEBHOOK_URL` (legacy `DISCORD_WEBHOOK_URL`), the creative/research webhook URLs, `ON_DEMAND_TOKEN`, Telegram trio.
- `[GH]` — a GitHub Actions secret (news digest + engagement loop): `ZAI_API_KEY`, `GITHUB_TOKEN`, all the webhook URLs, `DISCORD_TOKEN` (⚠️ private repo only), `PREFS_ENABLED`.
- `[both]` — needed in both.

In GitHub Actions, set each `[GH]`/`[both]` value as a repository secret (Settings → Secrets and variables → Actions). Locally and on the VM, keep them in `.env` (gitignored).

### 3. Curate playlists (talk summarizer only)
Edit [`playlists.txt`](playlists.txt) — one YouTube playlist URL per line. Aim for "talks by the people who built the tools".

### 4. Topics (news digest)
Topics live in `pipeline/news.py` `TOPICS` — each is `{ reddit_subs, github_keywords, github_min_stars, channel, webhook_env, live }`. **All 7 are `live=True`.** A topic only actually posts when its `webhook_env` is set.

Two lists, two cost models — the comment above `TOPICS` has the rules. Short version: `reddit_subs` is one multireddit request per topic (extra subs are free, but keep the list tight so a big sub can't crowd out small ones); `github_keywords` costs one Search call + 3s each and only matches repos **created in the last 7 days**, so use category terms there and put product names (Kling, Seedance, Nano Banana…) in `AI_KEYWORDS` + the judge prompt instead.

The other three sources are **shared across topics** (the judge tags them by topic, no per-topic config): **Hacker News** top stories (`HN_TOPN`, gated by `AI_KEYWORDS`), **HuggingFace trending** models (`HF_VIRAL_LIKES` = the boombastic-only bar), and **official lab/newsroom RSS** (`OFFICIAL_RSS` — 19 first-party + newsroom + practitioner feeds, round-robin merged, ≤`RSS_MAX_AGE_DAYS` old). Their candidate quotas are `HN_QUOTA` / `HF_QUOTA` / `RSS_QUOTA`; per-topic quotas are `PER_TOPIC_QUOTA`.

## Usage

### Talk summarizer — on-demand (one URL you just found)
Via the portal (`/run`), or on the VM:
```powershell
python -m pipeline.main --mode url --url "https://www.youtube.com/watch?v=..."
```

### Talk summarizer — daily scheduled scan
Runs automatically at ~09:03 MYT on the VM. To test locally:
```powershell
python -m pipeline.main --mode scheduled
```

### Trending news digest
Runs automatically every ~3h on GitHub Actions. To test locally:
```powershell
python -m pipeline.main --mode news
```

### @EconomyApp stock digest
Runs automatically daily (~09:00 MYT) on the VM cron. To test locally:
```powershell
python -m pipeline.main --mode x-digest
```
Mirrors the X account via its **Bluesky mirror** (public AT Protocol API) — no LLM, no
auth, no cookie to expire. The post text is the card verbatim (faithful to the figures).
Needs `DISCORD_STOCK_FINANCIAL_REPORT_WEBHOOK_URL`; a stale or unreachable feed alerts `🔒-staff-chat`.
See [STOCK-DIGEST-CHALLENGES.md](STOCK-DIGEST-CHALLENGES.md) for why this isn't X directly.

### Share any URL as a news card (`/share`)
Via the portal (`/share`), or on the VM:
```powershell
python -m pipeline.main --mode share --url "https://..."
```
Social-video URLs (IG Reels / XHS / TikTok / Threads / Douyin) are fetched with yt-dlp and transcribed with Groq Whisper so the card describes the video, not the bait caption. Returns `SHARED <topic>` on success.

### On-demand portal (`on_demand.py`)
A zero-dependency (Python stdlib) HTTP server, mobile-first dark/Discord-blurple single page. Start it on the VM detached:
```bash
cd ~/bersama/bersama-ai-pipeline && source .venv/bin/activate
nohup python on_demand.py > on_demand.log 2>&1 &
```
Then bookmark `http://<VM_IP>:8080/?token=<ON_DEMAND_TOKEN>` on a phone. Plain HTTP is fine for personal use on a phone bookmark; front it with Caddy/nginx for HTTPS.

### Flags
- `--dry-run` — build everything, print the Discord/Telegram payloads, **don't post**.
- `--stub-summary` / `--stub-news` — **local test only**: canned output, skip the LLM call (no API key needed).

## Output (talk summarizer)
Each processed video writes a folder under `content/<YYYY-MM-DD>_<slug>_<id>/`:
- `summary.md` — the canonical 5-point summary
- `post.threads.txt`, `post.facebook.txt` — captions, ready to paste
- `source.json` — metadata + structured summary
- `transcript.txt` — the fetched transcript (gitignored — large + soft copyright concern)

Quality-gate failures park in `content/_review/`; no-caption / too-long videos log to `content/_skipped/`. Both can alert you on Telegram.

## Configuration & state reference

> Snapshot of the live config for operators. The code in `pipeline/*.py` is the
> **source of truth** — these are the current values; re-check the constants there
> when the landscape moves (model/product names go stale fast).

### Environment variables (`.env.example`)

Tags: **`[VM]`** = GCP pipeline VM (summarizer + portal + `/share` + stock digest) ·
**`[GH]`** = GitHub Actions secret (news + engagement) · **`[both]`** = set in both.

**LLM + transcription**

| Key | Tag | Purpose |
|---|---|---|
| `ZAI_API_KEY` | `[both]` req | GLM API key (alt name `GLM_API_KEY` also works) |
| `ZAI_BASE_URL` | `[both]` | `https://api.z.ai/api/coding/paas/v4` (the "coding" endpoint for glm-5.2 plans) |
| `GLM_MODEL` | `[both]` | `glm-5.2` (default) |
| `GROQ_API_KEY` (alt `GROQ_KEY`) | `[VM]` | Whisper ASR — caption-less videos + social-video `/share`. Free key at console.groq.com |
| `GROQ_WHISPER_MODEL` | `[VM]` opt | default `whisper-large-v3` |
| `MAX_DURATION_MIN` | `[VM]` | `60` — skip videos longer than this |

**Gathering (news / engagement, mostly `[GH]`)**

| Key | Tag | Purpose |
|---|---|---|
| `GITHUB_TOKEN` | `[GH]` | authenticated GitHub Trending search (lifts the rate limit) |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | `[GH]` opt | Reddit OAuth (**script-type** app, reddit.com/prefs/apps) → real upvote counts. Unset → public RSS (`hot #N` rank) |
| `PREFS_ENABLED` | `[GH]` | `false` default — engagement-actuator kill switch |
| `DISCORD_TOKEN` | `[GH]` | bot token for the read-only reaction sweep. ⚠️ **private repo only** — a public-repo leak is full bot admin. Same token as the bot / MCP. Unset → sweep skipped (exit 0) |

**Discord webhooks** (all `[both]`; each has a legacy fallback name in parens, read new-name-first)

| Env var | Posts to |
|---|---|
| `DISCORD_YOUTUBE_WEBHOOK_URL` (`DISCORD_WEBHOOK_URL`) | `#youtube-ai-video` — creator-watch summarizer |
| `DISCORD_LLM_TOOLS_WEBHOOK_URL` (`DEVTOOLS` / `NEWS`) | `#ai-llm-tools` — coding / LLM / agent news |
| `DISCORD_IMAGE_CREATION_WEBHOOK_URL` | `#image-creation` |
| `DISCORD_VIDEO_CREATION_WEBHOOK_URL` | `#video-creation-aigc` |
| `DISCORD_VOICE_STUDIO_WEBHOOK_URL` | `#voice-studio` |
| `DISCORD_RESEARCH_WEBHOOK_URL` (`EDUCATION`) | `#research-with-ai` (was `#study-with-ai`) |
| `DISCORD_EARN_MONEY_WEBHOOK_URL` (`FINANCE`) | `#earn-money-with-ai` — individual / builder money-with-AI |
| `DISCORD_COMPANY_INVESTMENT_WEBHOOK_URL` | `#ai-company-investment` — AI-industry money/strategy/policy |
| `DISCORD_CYBERSECURITY_BYPASS_WEBHOOK_URL` | `#ai-cybersecurity-bypass` — AI security (incidents, jailbreaks, cyber-purpose tools) |
| `DISCORD_STOCK_FINANCIAL_REPORT_WEBHOOK_URL` (`STOCK_INVEST`) | `#stock-financial-report` (was `#stock-invest`) — `@EconomyApp` daily digest |
| `DISCORD_STAFF_CHAT_WEBHOOK_URL` | `🔒-staff-chat` — health warnings + weekly digest |

**Stock-digest tuning** (`[both]`, optional): `X_BSKY_API_BASE` (default `https://public.api.bsky.app`) · `X_STALE_DAYS` (default `6`).

**Portal + Telegram**

| Key | Tag | Purpose |
|---|---|---|
| `ON_DEMAND_TOKEN` | `[VM]` req | shared secret in the `/run` + `/share` URLs (`http://<VM_IP>:8080/?token=…`) |
| `ON_DEMAND_PORT` | `[VM]` | `8080` (code default; not in `.env.example`) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHANNEL_ID` / `TELEGRAM_DM_CHAT_ID` | `[both]` | broadcast-channel posts + maintainer failure DMs |

> **Retired:** `X_RSSHUB_BASE` / `TWITTER_AUTH_TOKEN` — the logged-in-cookie route was
> abandoned (the cookie expires and the job rots silently). Delete both if your `.env`
> still has them. See [STOCK-DIGEST-CHALLENGES.md](STOCK-DIGEST-CHALLENGES.md).

### News sources (PIPELINE 2)

Five sources — Reddit + GitHub are **per-topic**; HN + HuggingFace + official RSS are
**shared** (the judge tags every shared item with a topic). Gather puts shared sources
*first* so they can't be sliced off by the candidate cap.

| Source | Scope | Configured in | Quota |
|---|---|---|---|
| Reddit | per-topic | `TOPIC.reddit_subs` | `PER_TOPIC_QUOTA=8` (½ reserved for Reddit hot-order) |
| GitHub Trending | per-topic | `TOPIC.github_keywords` + `github_min_stars` | within topic quota; `VELOCITY_THRESHOLD=150` stars/day bypass |
| Hacker News | shared | `HN_TOPN=30` + `AI_KEYWORDS` filter | `HN_QUOTA=8` |
| HuggingFace | shared | `HF_VIRAL_LIKES=200` (boombastic-only gate) | `HF_QUOTA=3` |
| Official RSS | shared | `OFFICIAL_RSS` (below) | `RSS_QUOTA=12` |

Global caps: `LOCAL_LIMIT=100` candidates to the judge · `MAX_POST_PER_RUN=15` ·
`MAX_PER_TOPIC=3` posts/channel/run. RSS takes `RSS_PER_FEED=4` newest per feed,
round-robin merged, drops items older than `RSS_MAX_AGE_DAYS=14`. Reddit uses OAuth JSON
(real upvotes) when `REDDIT_CLIENT_ID/SECRET` are set, else public multireddit RSS
(`hot #N` rank instead of upvotes).

**`OFFICIAL_RSS`** — 19 feeds in `pipeline/news.py`, ordered highest-signal first
(round-robin means a later feed still contributes when earlier ones return nothing fresh):
- *Tier 1 — first-party labs:* OpenAI · Google AI · DeepMind · Google Gemini · Qwen · Mistral · HuggingFace blog
- *Tier 2 — AI newsrooms (where Kling / Seedance / Nano Banana news actually breaks):* the-decoder · smol.ai news · TechCrunch AI · VentureBeat AI · artificialintelligence-news · MarkTechPost · TestingCatalog (unreleased-feature sightings)
- *Tier 3 — creative-tool + practitioner:* fal.ai · ComfyUI blog · latent.space · Simon Willison · GitHub changelog

> Labs with no working RSS of their own — Anthropic, xAI, Moonshot, DeepSeek,
> ByteDance/Seed, Kuaishou (Kling), ElevenLabs, Runway, Black Forest Labs — arrive via
> the tier-2 newsrooms. (Checked 2026-07-24; every plausible URL 404s.)

**`TOPICS`** — all 9 are `live=True`; each routes to its own channel webhook:

| key | channel | webhook env | reddit_subs | github min ⭐ |
|---|---|---|---|---|
| `coding` | #ai-llm-tools | `DISCORD_LLM_TOOLS_WEBHOOK_URL` | LocalLLaMA, ClaudeAI, OpenAI, ChatGPTCoding, ClaudeCode, AI_Agents, cursor, singularity | 150 |
| `creative_image` | #image-creation | `DISCORD_IMAGE_CREATION_WEBHOOK_URL` | StableDiffusion, comfyui, midjourney, FluxAI, aiArt | 200 |
| `creative_video` | #video-creation-aigc | `DISCORD_VIDEO_CREATION_WEBHOOK_URL` | aivideo, KlingAI, runwayml, VeoAI, AIVideoGeneration | 200 |
| `creative_voice` | #voice-studio | `DISCORD_VOICE_STUDIO_WEBHOOK_URL` | SunoAI, elevenlabs, udiomusic, AIMusic | 150 |
| `research_study` | #research-with-ai | `DISCORD_RESEARCH_WEBHOOK_URL` | learnmachinelearning, artificial, MachineLearning, deeplearning | 100 |
| `research_productivity` | #research-with-ai | `DISCORD_RESEARCH_WEBHOOK_URL` | ChatGPT, PromptEngineering, notebooklm, perplexity_ai, Productivity | 150 |
| `finance` | #earn-money-with-ai | `DISCORD_EARN_MONEY_WEBHOOK_URL` | algotrading, quant, QuantFinance, SideProject, Entrepreneur, indiehackers, micro_saas, StartupSoloFounder, SaaS, solopreneur | 150 |
| `company_investment` | #ai-company-investment | `DISCORD_COMPANY_INVESTMENT_WEBHOOK_URL` | investing, stocks, wallstreetbets, technology, business, Economics | 50 |
| `cybersecurity` | #ai-cybersecurity-bypass | `DISCORD_CYBERSECURITY_BYPASS_WEBHOOK_URL` | cybersecurity, netsec, hacking, security, redteamsec, cryptography | 100 |

> ⚠️ `company_investment` + `cybersecurity` (added 2026-08-07) **peel two beats out of the
> old `coding` catch-all** — deals/strategy/org/policy and security/cyber-purpose tools now
> route to their own channels. Their subs are disjoint from every other topic (no duplicate
> fetch); cross-beat stories that originate on `coding`'s subs still reach them via the
> pooled judge + shared HN/RSS. The `research_*` topics post to `#research-with-ai` (channel
> renamed from `#study-with-ai`; webhook env `DISCORD_RESEARCH_WEBHOOK_URL`, legacy alias
> `DISCORD_EDUCATION_WEBHOOK_URL`). Product
> names (Kling, Nano Banana…) belong in `AI_KEYWORDS` + the judge prompt, **not**
> `github_keywords` (which only matches repos *created in the last 7 days*). See the comment
> above `TOPICS` for the two-list cost-model rules.

### State files (`state/` — committed by the news-digest workflow)

| File | What it tracks | Writer | Bounded by |
|---|---|---|---|
| `processed.json` | summarized video IDs (dedup) | summarizer | — (forever) |
| `news_seen.json` | posted-news story keys (dedup, filtered **before** the judge) | news | newest 500 |
| `github_stars.json` | per-repo star counts (→ star velocity) | github_trending | top 1000 by stars |
| `posted_log.jsonl` | one row per **auto-news** card (telemetry for the sweep) | news (GH Actions) | 14 d |
| `posted_log_share.jsonl` | one row per **owner `/share`** card | news (VM portal — sole writer, pushed to repo so the GH Actions sweep sees it) | 14 d |
| `engagement.jsonl` | one row per reaction-sweep snapshot | engagement | 60 d |
| `preferences.json` | the bandit model (EMA preference scores) | preferences | recompute-from-raw |
| `activity_baseline.json` | 7-day active-member count (reward normalizer) | engagement | latest |
| `x_seen_<screen>.json` | posted X-digest IDs, per account | x_digest | newest 500 |

A corrupt `processed.json` raises `StateCorruptError` and **halts the run** (a bad merge
must never re-publish the backlog); the engagement JSONL readers instead skip a truncated
final line — telemetry never halts the run.

### Engagement loop (dormant by default)

- **Seed reactions:** the community bot adds 👍 🔥 👎 to every news card every 15 min
  (`bersama-bot/bot.py` `SEED_EMOJIS`). `pipeline/engagement.py` `SEED_EMOJIS` **must
  match exactly** — order is a contract: 👍 useful, 🔥 amazing, 👎 not-for-me.
- **Sweep:** `engagement.py` reads reactions off cards 24 h–8 d old
  (`SWEEP_MIN_AGE_DAYS=1`, `SWEEP_MAX_AGE_DAYS=8`) via Discord REST (bot token,
  read-only — no Gateway), subtracts the bot's own seed reaction, computes a reward.
- **Reward weights** (`preferences.py`): 👍 +2 · 🔥 +3 · 👎 −2 · other react +1 · reply +4;
  clamped to `[−3, 5]`, normalized by the 7-day active-member baseline.
- **Model:** `preferences.py` — an ε-greedy contextual bandit over `topic` / `source` /
  `category`, 14-day half-life EMA, recompute-from-raw (no incremental drift).
- **Kill switch:** dormant unless `PREFS_ENABLED=true` **and** ≥ `MIN_EVENTS=20` reaction
  events. When active it retunes per-topic quotas (4–14), post caps (1–4), and shared
  quotas (±2); dormant = byte-identical to the static engine.
- **Weekly digest:** `digest.py` (Sun 22:23 UTC) posts one analytics card to `🔒-staff-chat`
  with a 🟢/🟡/🔴 verdict on whether to flip `PREFS_ENABLED` (ready at ≥40 events **and**
  ≥15% engagement — `READY_MIN_EVENTS_TOTAL`, `READY_MIN_ENGAGEMENT`).

### Stock digest (PIPELINE 4)

- **Watched account:** `@EconomyApp` → `#stock-financial-report`, via its Bluesky mirror DID
  `did:plc:kio5ffqovakoioxtxbuat6mr` (`X_SUBSCRIPTIONS` in `x_digest.py`).
- No LLM, no auth — public AT Protocol API. `MAX_PER_RUN=8` posts/run · `MAX_AGE_DAYS=7` ·
  first-run capped at `FIRST_RUN_MAX=3` (so a source switch can't dump a week at once) ·
  seen cap `SEEN_CAP=500`.
- **Stale guard:** `STALE_AFTER_DAYS=6` — a reachable feed whose newest post is ≥6 days old
  alerts `🔒-staff-chat` (a dead mirror otherwise fails silently). Env overrides:
  `X_BSKY_API_BASE`, `X_STALE_DAYS`.
- Each subscription supports `did=` (Bluesky JSON, preferred — carries images) **or**
  `rss=` (any RSS 2.0 / Atom feed, e.g. a Substack, as the documented fallback).

### Talk summarizer (PIPELINE 1)

- Forced `emit_summary` tool call → **exactly 5 points**, written in the **source video's
  language** (never translated). `MAX_SUMMARY_ATTEMPTS=3` — GLM doesn't strictly enforce
  the schema, so a malformed reply is retried, not fatal.
- **Transcript path:** yt-dlp captions (json3 preferred) → `youtube-transcript-api` →
  **Groq Whisper ASR** (opt-in via `GROQ_API_KEY`/`GROQ_KEY`; `whisper-large-v3`; ffmpeg
  transcodes to 16 kHz mono 32 kbps mp3 to fit Groq's 25 MB cap — a 60-min video ≈ 14 MB).
- YouTube datacenter-IP bot-block is handled by rotating `player_client` orderings, with a
  YouTube oEmbed safety net so a fully-blocked video still logs a clean, titled skip.
- **Creator-watch recency:** channel uploads are read from the dated YouTube RSS feed (last
  `RECENCY_DAYS=3`) and Shorts are stripped, so adding creators can't flood the run.
  `MAX_PER_RUN=5`/scheduled · `MAX_DURATION_MIN=60`.
- Output bundle under `content/<date>_<slug>_<id>/` (`summary.md`, `post.threads.txt`,
  `post.facebook.txt`, `source.json`, `transcript.txt`); quality-gate failures →
  `content/_review/`, skips → `content/_skipped/`.

## Notes
- **Why GLM, not Anthropic?** GLM (Z.ai) via its OpenAI-compatible endpoint; matches the project stack and has a cheap coding plan (~US$3/mo). To use Claude instead, swap the client in `summarize.py` / `news.py` to the Anthropic SDK and set `SUMMARY_MODEL`.
- **Why not Twitter/X for the news digest?** X's API is paid ($200/mo Basic) and anonymous scraping is blocked from datacenter IPs. Reddit + HN + GitHub Trending + HuggingFace + official RSS catch the same AI news within hours for free. The **stock digest** (PIPELINE 4) does follow one curated X account — via its **Bluesky mirror**, not X directly (the cookie never expires, the API is keyless); see [STOCK-DIGEST-CHALLENGES.md](STOCK-DIGEST-CHALLENGES.md).
- **Why not the `discord-mcp` connector?** That MCP is a local stdio process for *interactive* Claude Code use; it isn't reachable from cloud cron. This pipeline posts to Discord via webhooks directly.
- **Engagement loop (dormant by default).** The news-digest workflow also sweeps reactions on posted cards (`engagement.py`) and computes per-topic/source taste (`preferences.py`, gated by `PREFS_ENABLED` + a minimum event count). With enough signal the owner can opt in to a bandit actuator that dynamically retunes per-topic quotas + post caps; until then the static quotas run byte-identical. The community bot seeds 👍🔥👎 on every news card every 15 min so members have something to click. A weekly digest (`engagement-digest.yml`, Sun 22:23 UTC) posts one analytics card to `🔒-staff-chat`.
- **Cost:** GLM is fractions of a cent per run; GitHub Actions free tier covers the news + engagement schedules.
- **Keep `main` unprotected** while relying on the news run's auto-commit (the `GITHUB_TOKEN` can't push to a protected branch). Decide on a bot PAT before enabling branch protection.
