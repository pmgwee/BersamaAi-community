# BersamaAi — Feature Registry & Community Tracker

> Single source of truth for **what BersamaAi does**, **where each feature lives**, **what's live vs pending**.
> **Last updated:** 2026-07-21 · **Owner:** you · Edit as things change.
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
│       17 roles · 10 categories / 39 channels · seeded content         │
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
│ • topic-     │          │ • leveling    │           └──────────────────────┘
│   routed news│          │ • !commands   │
│              │          │ • @mention AI │
│ Summarizer:  │          │ Runs 24/7     │
│  GCP VM      │          │ (GCP VM,      │
│ News: GH     │          │  systemd)     │
│  Actions     │          │               │
└──────────────┘          └───────────────┘
```

| Component | Location | Reaches Discord via | Runs where |
|---|---|---|---|
| Content engine | `bersama-ai-pipeline/` | Webhook (posts) + bot token (engagement sweep, read-only REST) | **Summarizer**: GCP VM cron · **News**: GitHub Actions every 3h |
| Community bot | `bersama-bot/` | Bot token (Gateway) | GCP VM, systemd (`bersama`), 24/7 |
| Admin console | `discord-mcp/` | Same bot token (concurrent) | Local, on-demand |

> Bot + MCP jar **share one token** (concurrent Gateway sessions). Reset the token → update both `.env` files.

---

## A. Content engine — `bersama-ai-pipeline/`

LLM = **GLM `glm-5.2`** (Z.ai). ASR = **Groq Whisper** (`whisper-large-v3`).

| # | Feature | What it does | Posts to | Schedule | Status |
|---|---|---|---|---|---|
| A1 | **Creator-watch summarizer** | Watched channels (Kelly Tsai, 零度解说) → only NEW uploads (last ~3 days) → transcript (captions, or Groq Whisper for caption-less) → 5-point English summary + **thumbnail** | `#curated-resources` | daily ~09:03 MYT (GCP cron) | 🟢 live |
| A2 | **On-demand summarizer** | Paste any YouTube URL → summary + post | `#curated-resources` | manual (VM HTTP trigger or SSH) | 🟡 needs firewall |
| A3 | **Topic-routed news (coding)** | Reddit (r/LocalLLaMA, r/ClaudeAI, r/OpenAI, r/ChatGPTCoding) + Hacker News + **GitHub Trending** → GLM judge tags topic + heat → **thumbnail** cards | `#ai-dev-tools` | every ~3h (GH Actions) | 🟢 live |
| A4 | **Topic-routed news (creative + research)** | Same engine, other topics | `#image-creation` / `#video-creation-aigc-tvc` / `#voice-studio` / `#study-with-ai` | every ~3h | 🟢 live |
| A5 | **Engagement feedback loop** | Member reactions (👍🔥👎) + replies on news cards → reward → EMA preference scores per topic/source/category → biases candidate quotas + a judge "taste memo" (ε-greedy bandit; heat still wins). Seed reactions added by the bot; swept by the pipeline; weekly digest to `#staff-chat` (with a 🟢/🟡/🔴 "flip PREFS_ENABLED?" verdict). Gated by `PREFS_ENABLED` + `n_events≥20` — dormant (byte-identical) until flipped on. | `#staff-chat` (digest) | sweep+prefs ride the 3h news run; digest weekly | 🟢 telemetry live · 🟡 actuator dormant |

- **Creator-watch list:** `playlists.txt` (channel URLs, recency-filtered). Add creators anytime → push → `git pull` on VM.
- **News topics:** `pipeline/news.py` `TOPICS`. Enable one = set its webhook env var + flip `live=True`.
- **Caption-less path:** no captions → yt-dlp downloads audio → ffmpeg → 16 kHz mono mp3 → Groq Whisper transcribes → GLM summarizes → post.

**Cross-cutting hardening:** forced tool-call schemas · per-topic dedup (`state/news_seen.json`, `state/processed.json`) · quality gate · corrupt-state halt · webhook/token masking · GLM retry on malformed output · real heat metrics (⭐ stars / ▲ upvotes / HN points).

---

## B. Community bot — `bersama-bot/` (MEE6 clone)

discord.py event bot on the GCP VM (systemd `bersama`). Privileged intents required (Members + Message Content).

| # | Feature | MEE6 equivalent | Status |
|---|---|---|---|
| B1 | Welcome + auto-role on join | Welcome + Autorole | ✅ verified |
| B2 | Self-assign reaction roles (5 emoji) | Reaction Roles | ✅ verified |
| B3 | Leveling (MEE6-curve XP, `/rank` `/leaderboard`, role rewards Lv 5/10/20/35) | Leveling + Premium | ✅ verified |
| B4 | Prefix commands (`!rules` `!resources` `!ai`) | Custom Commands | ✅ verified |
| B5 | `@BersamaAi` / `!ai` → GLM-5.2 (cost-guardrailed) | "AI" tier | ✅ verified |

---

## C. Infrastructure & ops

| # | Feature | Status |
|---|---|---|
| C1 | discord-mcp jar (SaseQ) — admin via Claude, `localhost:8085` | 🟡 on-demand |
| C2 | Discord native AutoMod → `#mod-log` + timeout | 🟡 pending |
| C3 | Live server (17 roles, 10 categories/39 channels, onboarding, reaction-role menu) | 🟢 live |
| C4 | Seeded content (`#curated-resources` 6 talks, `#tools-directory` ~35 tools) | 🟢 live |

### Out of scope
❌ Music · ❌ heavy economy plugins · ❌ web dashboard (use `config.json`) · ❌ XHS/TikTok auto-post (manual bundle)

---

## Schedules

| Pipeline | Cron | When | Where |
|---|---|---|---|
| Creator-watch summarizer | `3 1 * * *` UTC | ~09:03 MYT daily | GCP VM (`run-daily.sh`) |
| News digest (coding) | `17 */3 * * *` UTC | every 3h | GitHub Actions (`news-digest.yml`) |
| Engagement sweep + preferences | rides the news run | every 3h | GitHub Actions (steps in `news-digest.yml`) |
| Engagement weekly digest | `23 22 * * 0` UTC | Sun weekly | GitHub Actions (`engagement-digest.yml`) |
| On-demand summarizer | manual | — | GCP VM (`on_demand.py` HTTP) or SSH |

> Cron is UTC; GitHub Actions can drift ~5–15 min under load.

---

## Config & secrets

| Key | pipeline `.env` (VM) | bot `.env` (VM) | GH Actions secret |
|---|:---:|:---:|:---:|
| `ZAI_API_KEY` | ✅ | ✅ | ✅ |
| `ZAI_BASE_URL` / `GLM_MODEL` | ✅ | ✅ | ✅ (vars) |
| `GROQ_API_KEY` (ASR) | ✅ | — | — |
| `DISCORD_WEBHOOK_URL` (`#curated-resources`) | ✅ | — | ✅ |
| `DISCORD_NEWS_WEBHOOK_URL` (`#ai-dev-tools`) | ✅ | — | ✅ |
| `GITHUB_TOKEN` (news GitHub search) | optional | — | ✅ (built-in) |
| `ON_DEMAND_TOKEN` (HTTP trigger) | ✅ | — | — |
| `DISCORD_TOKEN` (engagement sweep — read-only REST) | — | ✅ | ✅ ⚠️ private repo only |
| `DISCORD_STAFF_CHAT_WEBHOOK_URL` (`#staff-chat` weekly digest) | — | — | ✅ |
| `PREFS_ENABLED` (engagement actuator on/off; default `false`) | optional | — | ✅ |

⚠️ Keep in sync: `DISCORD_TOKEN` (bot + MCP + GH sweep secret) · `ZAI_API_KEY` (pipeline + bot).
⚠️ **Security:** the `DISCORD_TOKEN` GH Actions secret grants full bot admin (Discord tokens aren't scoped). **Only add it if this repo is PRIVATE.** If public, run the engagement sweep on the GCP VM cron instead. Verify with `gh repo view --json visibility` before adding.

---

## Quick ops runbook

| I want to… | Do this |
|---|---|
| Test summarizer on VM | `python -m pipeline.main --mode url --url "<YT>" --dry-run` |
| Test news on VM | `python -m pipeline.main --mode news --dry-run` |
| Run the daily scan now (VM) | `~/bersama/bersama-ai-pipeline/run-daily.sh && tail -40 logs/daily.log` |
| On-demand from phone | open `http://<VM_IP>:8080/?token=<T>`, paste URL |
| Add a watched creator | edit `playlists.txt` → push → `git pull` on VM |
| Enable a creative/research news topic | set its webhook env var + flip `live=True` in `news.py` `TOPICS` |
| Turn the engagement actuator ON | set GH secret `PREFS_ENABLED=true` (only after `n_events≥~100`; needs `DISCORD_TOKEN` secret + private repo) |
| Self-check the engagement math | `cd bersama-ai-pipeline && python verify_engagement.py` |
| Run the news + loop locally (offline) | `python -m pipeline.main --mode news --dry-run --stub-news` |
| Edit bot channels/roles | `bersama-bot/config.json` |

---

## Updates log

| Date | Change |
|---|---|
| 2026-07-22 | **Thumbnails + language overhaul** (news + on-demand share): new robust image extractor in `news.py` — og:image → `<link image_src>` → JSON-LD → first content `<img>`, with SVG/tracking-pixel/icon filtering, `&amp;`-unescaping, Next.js `_next/image` decoding, and a **microlink.io fallback for JS-only shells (Threads/X)** so cards almost always get a thumbnail. **Language policy reversed for cards**: news + share cards now keep the **source's original language** (no translation) — Chinese source → Chinese card, English → English. (Server English-only decision of 2026-07-20 is relaxed for card *content*; the small category/topic badge stays English.) News picks this up on GH Actions automatically; on-demand share needs a pipeline VM `git pull`. |
| 2026-07-22 | Engagement loop refinements: swapped the negative emoji 😐→👎 (clearer "not for me"; matches the −2 weight); refactored `engagement.py` so the seed emoji lives in one constant; weekly digest now leads with a 🟢/🟡/🔴 **"Flip PREFS_ENABLED?"** readiness verdict (≥40 events + ≥15% engagement → 🟢); wired the `PREFS_ENABLED` secret into the news-digest workflow steps. |
| 2026-07-22 | **Engagement feedback loop (A5)** shipped: news cards now log to `state/posted_log.jsonl` (`?wait=true`); bot seeds 👍🔥😐 on news cards every 15 min; a 3h sweep (`pipeline/engagement.py`) reads reactions via bot-token REST → reward → `state/engagement.jsonl` + `state/activity_baseline.json`; `pipeline/preferences.py` computes 14-day-half-life EMA preference scores → `state/preferences.json`; the news actuator (dynamic quotas + judge taste memo) is wired but **dormant** behind `PREFS_ENABLED=false` + `n_events≥20` (byte-identical to pre-loop). Weekly analytics digest → `#staff-chat` (`engagement-digest.yml`). Creative/research channels now **live** (A4). ⚠️ needs `DISCORD_TOKEN` + `DISCORD_STAFF_CHAT_WEBHOOK_URL` GH secrets (private repo only). |
| 2026-07-21 | Content engine overhauled: summarizer on **GCP VM** (creator-watch + Groq ASR + thumbnails + on-demand HTTP trigger); news became **topic-routed** (Reddit + HN + GitHub Trending → `#ai-dev-tools`, every 3h on GH Actions); creative/research channels wired-off pending webhooks. |
| 2026-07-21 | Repo consolidated to `pmgwee/BersamaAi-community`; GH Actions summarizer workflows retired (YouTube bot-blocked on Azure IPs). |
| 2026-07-20 | Server went English-only; summarizer switched to GLM; news digest added. |
