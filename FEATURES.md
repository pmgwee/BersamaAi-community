# BersamaAi — Feature Registry & Community Tracker

> Single source of truth for **what BersamaAi does**, **where each feature lives**, **what's live vs pending**.
> **Last updated:** 2026-07-23 · **Owner:** you · Edit as things change.
> Onboarding / start-here doc: [`PROJECT-CONTEXT.md`](PROJECT-CONTEXT.md)

---

## How to use this doc
- Feature Registry below — every feature appears exactly once.
- Status: 🟢 live · 🟡 pending · ⚪ off.
- Append every meaningful change to the [Updates log](#updates-log).

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│              Discord: BersamaAi server (live)                         │
│       18 roles · 11 categories · seeded content                       │
└──────────────────────────────────────────────────────────────────────┘
       ▲ webhook                 ▲ Gateway (bot token)        ▲ Gateway (same token)
       │                         │                            │  + claude.ai / Claude Code
┌──────┴───────┐          ┌──────┴────────┐           ┌──────┴──────────────┐
│ bersama-ai-  │          │ bersama-bot   │           │ discord-mcp (SaseQ) │
│ pipeline     │          │ (discord.py)  │           │ admin jar            │
│              │          │               │           │                      │
│ • creator-   │          │ • welcome     │           │ • on-demand admin    │
│   watch      │          │ • reaction    │           │   via Claude         │
│   summarizer │          │   roles       │           │ • cron timers        │
│ • on-demand  │          │ • leveling    │           └──────────────────────┘
│   portal     │          │ • !/  / cmds  │
│   + /share   │          │ • @mention AI │
│ • topic-     │          │ Runs 24/7     │
│   routed news│          │ (GCP VM,      │
│              │          │  systemd)     │
│ Summarizer + │          │               │
│ portal: GCP  │          │               │
│ VM. News +   │          │               │
│ engagement:  │          │               │
│ GH Actions   │          │               │
└──────────────┘          └───────────────┘
```

| Component | Location | Reaches Discord via | Runs where |
|---|---|---|---|
| Content engine | `bersama-ai-pipeline/` | Webhook (posts) + bot token (engagement sweep, read-only REST) | **Summarizer + on-demand portal + `/share`**: GCP VM · **News + engagement loop**: GitHub Actions every 3h |
| Community bot | `bersama-bot/` | Bot token (Gateway) | GCP VM, systemd (`bersama`), 24/7 |
| Admin console | `discord-mcp/` | Same bot token (concurrent) | Local, on-demand |

> Bot + MCP jar **share one token** (concurrent Gateway sessions). Reset the token → update both `.env` files.

---

## A. Content engine — `bersama-ai-pipeline/`

LLM = **GLM `glm-5.2`** (Z.ai). ASR = **Groq Whisper** (`whisper-large-v3`).

| # | Feature | What it does | Posts to | Schedule | Status |
|---|---|---|---|---|---|
| A1 | **Creator-watch summarizer** | Watched channels (Kelly Tsai, 零度解说) → only NEW uploads (last ~3 days) → transcript (captions, or Groq Whisper for caption-less) → 5-point English summary + **thumbnail** | `#youtube-resources` | daily ~09:03 MYT (GCP cron) | 🟢 live |
| A2 | **On-demand portal** (`on_demand.py`) | Phone-friendly web UI (dark/Discord-blurple, VM port 8080, `?token=`-authed via `ON_DEMAND_TOKEN`). **`/run`** = summarize any YouTube URL; **`/share`** = share any URL (social video via yt-dlp + Groq Whisper so the card describes what the video actually says) as a topic-routed news card. Bookmark `http://<VM_IP>:8080/?token=<T>` on a phone, paste a link. | `#youtube-resources` (`/run`) / topic channel (`/share`) | manual | 🟢 live |
| A3 | **Topic-routed news (coding)** | Reddit (r/LocalLLaMA, r/ClaudeAI, r/OpenAI, r/ChatGPTCoding) + Hacker News + **GitHub Trending** (≥150★) → GLM judge tags topic + heat → **thumbnail** cards | `#ai-dev-tools` | every ~3h (GH Actions) | 🟢 live |
| A4 | **Topic-routed news (creative + research)** | Same engine, other topics — **all `live=True`** (image/video/voice + study/productivity) | `#image-creation` / `#video-creation-aigc-tvc` / `#voice-studio` / `#study-with-ai` / `#research-with-ai` (research posts via `DISCORD_EDUCATION_WEBHOOK_URL`) | every ~3h | 🟢 live |
| A5 | **Engagement feedback loop** | Member reactions (👍🔥👎) + replies on news cards → reward → EMA preference scores per topic/source/category → biases candidate quotas + a judge "taste memo" (ε-greedy bandit; heat still wins). Seed reactions added by the bot; swept by the pipeline; weekly digest to `#staff-chat` (with a 🟢/🟡/🔴 "flip PREFS_ENABLED?" verdict). Gated by `PREFS_ENABLED` + `n_events≥20` — dormant (byte-identical) until flipped on. | `#staff-chat` (digest) | sweep+prefs ride the 3h news run; digest weekly | 🟢 telemetry live · 🟡 actuator dormant |

- **`/share` run status** (`--mode share --url`): returns `SHARED <topic>` on success (or `SHARED_DRY` in `--dry-run`); both are in the pipeline's OK-status set so a share run never false-fails the GH Actions / VM run.
- **Creator-watch list:** `playlists.txt` (channel URLs, recency-filtered). Add creators anytime → push → `git pull` on VM.
- **News topics:** `pipeline/news.py` `TOPICS`. **All 7 are `live=True`** (coding, creative_image/video/voice, research_study/productivity, finance). To (re)configure one: set its `webhook_env` env var + keep `live=True`.
- **News source tuning** (2026-07-25): per-topic `reddit_subs` widened to 34 verified-live subs; `AI_KEYWORDS` grew to 144 terms so current product names (Seedance, Kling, Nano Banana, Seedream, Wan, Eleven Music…) survive the pre-filter; `OFFICIAL_RSS` grew 4 → 19 feeds in three tiers (labs / AI newsrooms / creative-tool blogs). Labs with **no** working RSS (Anthropic, xAI, Moonshot, DeepSeek, ByteDance, Kuaishou, ElevenLabs, Runway, BFL) are covered by the newsroom tier.
- **Caption-less path:** no captions → yt-dlp downloads audio → ffmpeg → 16 kHz mono mp3 → Groq Whisper transcribes → GLM summarizes → post. (Same ASR path powers `/share` on social videos.)

**Cross-cutting hardening:** forced tool-call schemas · per-topic dedup (`state/news_seen.json`, `state/processed.json`) · quality gate · corrupt-state halt · webhook/token masking · GLM retry on malformed output · real heat metrics (⭐ stars / ▲ upvotes / HN points) · robust thumbnail extractor with a microland.io fallback for JS-only shells (Threads/X).

---

## B. Community bot — `bersama-bot/` (MEE6 clone)

discord.py event bot on the GCP VM (systemd `bersama`). Privileged intents required (Members + Message Content). Single-guild allowlist.

| # | Feature | MEE6 equivalent | Status |
|---|---|---|---|
| B1 | Welcome + auto-role on join (`#welcome`) | Welcome + Autorole | ✅ verified |
| B2 | Self-assign reaction roles (1 menu, 5 emoji: 🎓 / 🎨 / 💼 / 💻 / 📈) | Reaction Roles | ✅ verified |
| B3 | Leveling (MEE6-curve XP, `/rank` `/leaderboard`, role rewards Lv 5/10/20/35, posts to `#level-ups`) | Leveling + Premium | ✅ verified |
| B4 | Prefix commands (`!rules` `!resources` `!ai`) + slash `/help` | Custom Commands | ✅ verified |
| B5 | `@BersamaAi` / `!ai` → GLM-5.2, **context-aware** (last 50 msgs + Jina Reader page content for up to 2 links, SSRF-guarded), cost-guardrailed | "AI" tier | ✅ verified |
| B6 | `seed_reactions` task (every 15 min) — adds 👍🔥👎 to webhook-authored news cards (the engagement-loop bridge) | — | ✅ verified |

> AI is **optional** — off (with a friendly reply) when `ZAI_API_KEY` is unset. A 5-min heartbeat `os._exit(1)`s on a stale Gateway so systemd restarts clean.

---

## C. Infrastructure & ops

| # | Feature | Status |
|---|---|---|
| C1 | discord-mcp jar (SaseQ) — admin via Claude, `localhost:8085` | 🟡 on-demand |
| C2 | Auto-moderation — delegated to Discord's **native AutoMod** (no auto-mod in bot code); **not yet enabled** in the server | 🟡 pending |
| C3 | Live server (18 roles, 11 categories, onboarding, reaction-role menu) | 🟢 live |
| C4 | Seeded content (`#youtube-resources` 6 talks, `#tools-directory` ~35 tools) | 🟢 live |

### Out of scope
❌ Music · ❌ heavy economy plugins · ❌ admin/config web dashboard (use `config.json`) · ❌ XHS/TikTok auto-post (manual bundle). *(The on-demand **portal** in A2 is a phone trigger for summarize/share, not an admin dashboard.)*

---

## Schedules

| Pipeline | Cron | When | Where |
|---|---|---|---|
| Creator-watch summarizer | `3 1 * * *` UTC | ~09:03 MYT daily | GCP VM (`run-daily.sh`) |
| News digest (all topics) | `17 */3 * * *` UTC | every 3h | GitHub Actions (`news-digest.yml`) |
| Engagement sweep + preferences | rides the news run | every 3h | GitHub Actions (steps in `news-digest.yml`) |
| Engagement weekly digest | `23 22 * * 0` UTC | Sun weekly | GitHub Actions (`engagement-digest.yml`) |
| On-demand portal (`/run` summarize, `/share` card) | manual | — | GCP VM (`on_demand.py` :8080) or SSH |

> Cron is UTC; GitHub Actions can drift ~5–15 min under load.

---

## Config & secrets

Authoritative list: [`bersama-ai-pipeline/.env.example`](bersama-ai-pipeline/.env.example) and [`bersama-bot/.env.example`](bersama-bot/.env.example) — each key tagged `[VM]` / `[GH]` / `[both]`.

| Key | pipeline `.env` (VM) | bot `.env` (VM) | GH Actions secret |
|---|:---:|:---:|:---:|
| `ZAI_API_KEY` / `ZAI_BASE_URL` / `GLM_MODEL` | ✅ | ✅ | ✅ (`ZAI_*` secret + vars) |
| `GROQ_API_KEY` (ASR — summarizer caption-less + `/share` social video) | ✅ | — | — |
| `GITHUB_TOKEN` (news GitHub Trending search) | optional | — | ✅ (built-in) |
| `DISCORD_YOUTUBE_WEBHOOK_URL` (legacy `DISCORD_WEBHOOK_URL`) → `#youtube-resources` (summarizer) | ✅ | — | ✅ |
| `DISCORD_DEVTOOLS_WEBHOOK_URL` (legacy `DISCORD_NEWS_WEBHOOK_URL`) → `#ai-dev-tools` (coding) | ✅ | — | ✅ |
| `DISCORD_IMAGE/VIDEO_CREATION/VOICE_STUDIO/EDUCATION_WEBHOOK_URL` (creative + research channels) | ✅ | — | ✅ |
| `DISCORD_FINANCE_WEBHOOK_URL` (`#earn-money-with-ai` finance topic) | ✅ | — | ✅ |
| `DISCORD_STAFF_CHAT_WEBHOOK_URL` (`#staff-chat` weekly digest + news 0-posted health alerts) | — | — | ✅ |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` (news Reddit OAuth — real upvote counts; unset = public RSS fallback, hot-rank only) | — | — | optional |
| `ON_DEMAND_TOKEN` + `ON_DEMAND_PORT` (portal `/run` + `/share` auth; port default 8080) | ✅ | — | — |
| `DISCORD_TOKEN` (engagement sweep — read-only REST) | — | ✅ | ✅ ⚠️ private repo only |
| `PREFS_ENABLED` (engagement actuator on/off; default `false`) | optional | — | ✅ |
| `JINA_API_KEY` (bot link-fetch context, optional) | — | optional | — |
| Telegram trio (`TELEGRAM_BOT_TOKEN` / `_CHANNEL_ID` / `_DM_CHAT_ID`) | ✅ | — | ✅ |

⚠️ Keep in sync: `DISCORD_TOKEN` (bot + MCP + GH sweep secret) · `ZAI_API_KEY` (pipeline + bot).
⚠️ **Security:** the `DISCORD_TOKEN` GH Actions secret grants full bot admin (Discord tokens aren't scoped). **Only add it if this repo is PRIVATE.** If public, run the engagement sweep on the GCP VM cron instead. Verify with `gh repo view --json visibility` before adding.

---

## Quick ops runbook

| I want to… | Do this |
|---|---|
| Test summarizer on VM | `python -m pipeline.main --mode url --url "<YT>" --dry-run` |
| Test news on VM | `python -m pipeline.main --mode news --dry-run` |
| Test `/share` on VM | `python -m pipeline.main --mode share --url "<URL>" --dry-run` |
| Run the daily scan now (VM) | `~/bersama/bersama-ai-pipeline/run-daily.sh && tail -40 logs/daily.log` |
| On-demand from phone | open `http://<VM_IP>:8080/?token=<T>` — `/run` to summarize, `/share` to post a link as a card |
| Relaunch the portal (VM) | `pkill -f on_demand.py; cd ~/bersama/bersama-ai-pipeline && source .venv/bin/activate && nohup python on_demand.py > on_demand.log 2>&1 &` |
| Add a watched creator | edit `playlists.txt` → push → `git pull` on VM |
| (Re)configure a news topic | edit `pipeline/news.py` `TOPICS` + its `webhook_env` env var (all 7 are live today) |
| Turn the engagement actuator ON | set GH secret `PREFS_ENABLED=true` (only after `n_events≥~100`; needs `DISCORD_TOKEN` secret + private repo) |
| Self-check the engagement math | `cd bersama-ai-pipeline && python verify_engagement.py` |
| Run the news + loop locally (offline) | `python -m pipeline.main --mode news --dry-run --stub-news` |
| Restart the bot (VM) | `sudo systemctl restart bersama && tail -n 4 ~/BersamaAi-community/bersama-bot/bersama.log` |
| Edit bot channels/roles | `bersama-bot/config.json` |

---

## Updates log

| Date | Change |
|---|---|
| 2026-07-24 | **New `finance` news topic → `#earn-money-with-ai`** (fintech / AI trading / quant / making-money-with-AI). 7th live topic: added to `TOPICS`, both judge prompts, `TOPIC_LABEL`; sourced from r/algotrading + r/QuantFinance + r/quant + GitHub keywords (fintech, trading bot, algorithmic trading, quant, ai finance). Webhook `DISCORD_FINANCE_WEBHOOK_URL` wired into `.env.example` + `news-digest.yml`. ⚠️ needs the `DISCORD_FINANCE_WEBHOOK_URL` **GitHub repo secret** set before production cards post (local `.env` already has it). |
| 2026-07-24 | **`/share` posts now feed the engagement loop.** `post_url_as_news` captures the post + logs to `posted_log.jsonl` (`origin="share"`) so owner-curated cards' reactions/replies are swept + scored by preferences — closing the loop on the engine's strongest taste signal (previously dropped because the `_post()` return was discarded). `_log_posted` gains an `origin` field (auto\|share); strictly additive. |
| 2026-07-24 | **Card heat labels rewritten for non-technical readers + GitHub forks.** Every news card's heat line now explains *in plain English* why an item is trending (designed via a 3-draft→judge panel for clarity): GitHub → `⭐ Exploding on GitHub: 12k stars (likes), +269 new stars/day, 1.4k forks (copies), only 2 days old` — **forks now captured** from the Search API; "Exploding" only when run-over-run growth is measured, else "Trending". HN → `📰 820 upvotes on Hacker News — front page of a top tech-news forum` (fixes the opaque "HN points"). Reddit → `👍 1.2k upvotes on r/LocalLLaMA — one of the hottest posts there right now` / `📈 Trending #3 … — one of the hottest posts in that forum right now`. HuggingFace → `🤗 1.2k likes and 50k downloads on HuggingFace (the AI model hub)`. Official blog → `📢 Straight from OpenAI's official blog — the source itself, not secondhand news` (company inferred from URL). `_fmt` now prints `12k` not `12.0k`. |
| 2026-07-23 | **News engine: Reddit revived + dedup made intelligent + silent runs made visible.** Reddit's unauthenticated `.json` API now 403s (all Reddit sources had silently been returning 0 for days) — `fetch_reddit` is now tiered: OAuth JSON when `REDDIT_CLIENT_ID/SECRET` secrets exist (real upvote counts) → public **multireddit RSS** fallback (one request per topic, `<category>`-attributed, per-sub hot-rank instead of scores; judge + card labels understand `hot_rank`). Source failures now log loudly (`[reddit] …`, `[sources] …`) instead of silent `continue`. **Dedup moved BEFORE the judge**: already-posted stories are filtered out of the pool up front, so quota slots + judge tokens only go to fresh stories (root cause of the "0 posted, wall of NEWS_DEDUPED" runs #17/#18). Fixed `news_seen.json` eviction (was lexicographic `sorted()[-500:]` — random hashes evicted; now insertion-order = oldest-first). **Any real run that posts 0 cards now warns `#staff-chat`** via `DISCORD_STAFF_CHAT_WEBHOOK_URL` with the run breakdown. Swapped dead `r/ArtificialIntelligence` (404) → `r/artificial`. |
| 2026-07-23 | **Docs synced to reality.** Documented the on-demand **portal** (`on_demand.py` — mobile-first blurple UI on VM port 8080) with **`/run`** (summarize) + **`/share`** (share-any-URL-as-news-card) as shipped. Recorded the **`/share` flow** (`--mode share`, `SHARED`/`SHARED_DRY` run status added to the OK set in commit `91d72c9`); social-video share uses yt-dlp + Groq Whisper. Confirmed **all 6 news topics are `live=True`** (creative + research channels live, not just coding). Pointed secrets map at the now-complete, `[VM]`/`[GH]`-tagged `.env.example` files (commit `015635c`). Refreshed the live server to **18 roles / 11 categories / 8 members** (new `SERVER STATS` category). Added B6 (`seed_reactions`), `/leaderboard` + `/help`, Jina link-context, and the heartbeat self-restart to the bot section. |
| 2026-07-22 | **Thumbnails + language overhaul** (news + on-demand share): new robust image extractor in `news.py` — og:image → `<link image_src>` → JSON-LD → first content `<img>`, with SVG/tracking-pixel/icon filtering, `&amp;`-unescaping, Next.js `_next/image` decoding, and a **microlink.io fallback for JS-only shells (Threads/X)** so cards almost always get a thumbnail. **Language policy reversed for cards**: news + share cards now keep the **source's original language** (no translation) — Chinese source → Chinese card, English → English. (Server English-only decision of 2026-07-20 is relaxed for card *content*; the small category/topic badge stays English.) News picks this up on GH Actions automatically; on-demand share needs a pipeline VM `git pull`. |
| 2026-07-22 | Engagement loop refinements: swapped the negative emoji 😐→👎 (clearer "not for me"; matches the −2 weight); refactored `engagement.py` so the seed emoji lives in one constant; weekly digest now leads with a 🟢/🟡/🔴 **"Flip PREFS_ENABLED?"** readiness verdict (≥40 events + ≥15% engagement → 🟢); wired the `PREFS_ENABLED` secret into the news-digest workflow steps. |
| 2026-07-22 | **Engagement feedback loop (A5)** shipped: news cards now log to `state/posted_log.jsonl` (`?wait=true`); bot seeds 👍🔥👎 on news cards every 15 min; a 3h sweep (`pipeline/engagement.py`) reads reactions via bot-token REST → reward → `state/engagement.jsonl` + `state/activity_baseline.json`; `pipeline/preferences.py` computes 14-day-half-life EMA preference scores → `state/preferences.json`; the news actuator (dynamic quotas + judge taste memo) is wired but **dormant** behind `PREFS_ENABLED=false` + `n_events≥20` (byte-identical to pre-loop). Weekly analytics digest → `#staff-chat` (`engagement-digest.yml`). Creative/research channels now **live** (A4). ⚠️ needs `DISCORD_TOKEN` + `DISCORD_STAFF_CHAT_WEBHOOK_URL` GH secrets (private repo only). |
| 2026-07-21 | Content engine overhauled: summarizer on **GCP VM** (creator-watch + Groq ASR + thumbnails + on-demand HTTP trigger); news became **topic-routed** (Reddit + HN + GitHub Trending → `#ai-dev-tools`, every 3h on GH Actions); creative/research channels wired-off pending webhooks. |
| 2026-07-21 | Repo consolidated to `pmgwee/BersamaAi-community`; GH Actions summarizer workflows retired (YouTube bot-blocked on Azure IPs). |
| 2026-07-20 | Server went English-only; summarizer switched to GLM; news digest added. |
