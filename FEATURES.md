# BersamaAi — Feature Registry & Community Tracker

> The single source of truth for **what BersamaAi does**, **where each feature lives**, and **what's live vs pending**.
> Refer back here whenever you ship, change, or review the community.
>
> **Last updated:** 2026-07-20 · **Owner:** you · **Edit this file** as things change.

---

## How to use this doc

- **Feature Registry** below is the canonical list — every feature appears exactly once.
- Use the **Status** column to track what's live. Values: `🟢 live` · `🟡 pending` · `⚪ off`.
- Log every meaningful change in **[Updates log](#updates-log)** at the bottom (append-only).
- For *how to set things up*, see `SESSION-HANDOFF.md` and each component's own `README.md`.

---

## Architecture — three independent components

```
┌──────────────────────────────────────────────────────────────────────┐
│                        Discord: BersamaAi server                     │
│         (14 roles · 8 categories / 27 channels · seeded content)      │
└──────────────────────────────────────────────────────────────────────┘
       ▲                    ▲                          ▲
       │ webhook            │ Gateway (bot token)      │ Gateway (same token)
       │                    │                          │  + claude.ai connector
┌──────┴───────┐    ┌───────┴────────┐         ┌───────┴──────────────┐
│ bersama-ai-  │    │ bersama-bot    │         │ discord-mcp (SaseQ)  │
│ pipeline     │    │ (discord.py)   │         │ jar — your admin     │
│              │    │                │         │ console               │
│ • talk       │    │ • welcome      │         │ • on-demand admin     │
│   summarizer │    │ • reaction     │         │ • Claude-for-owner    │
│ • news       │    │   roles        │         │ • cron timers         │
│   digest     │    │ • leveling     │         └───────────────────────┘
│              │    │ • !commands    │
│ Runs on GH   │    │ • @mention AI  │
│ Actions (CI) │    │ Runs 24/7      │
└──────────────┘    └────────────────┘
```

| Component | Location | Reaches Discord via | When it runs |
|---|---|---|---|
| **Content pipeline** | `bersama-ai-pipeline/` | Webhook (no token) | GitHub Actions cloud cron |
| **Community bot** | `bersama-bot/` | Bot token (Gateway) | Always-on host |
| **Admin console** | `discord-mcp/` | Bot token (same, concurrent) | On demand |

> The bot and the MCP jar deliberately **share one token** (Discord allows concurrent Gateway sessions). If you ever reset the token, update **both** `.env` files.

---

## A. Content engine — `bersama-ai-pipeline/`

Runs on **GitHub Actions** so it works while your PC is off. LLM = **GLM `glm-5.2`** via Z.ai's OpenAI-compatible endpoint.

| # | Feature | What it does | Posts to | Schedule | Status |
|---|---|---|---|---|---|
| A1 | **Talk summarizer** | YouTube talk → transcript → **5-point English summary** + Threads/FB caption bundle | `#curated-resources` (+ optional Telegram) | daily ~09:03 MYT | 🟡 pending |
| A2 | **Trending news digest** | Reddit (4 AI subs) + Hacker News → GLM judge picks **0–4** → category-tagged cards | `#subscription-value` | every ~3h | 🟡 pending |
| A3 | **On-demand summarizer** | Paste a URL in the Actions UI → summary + post | `#curated-resources` | manual | 🟡 pending |

**Sources for A2:** `r/LocalLLaMA`, `r/singularity`, `r/OpenAI`, `r/ClaudeAI` + top 30 Hacker News stories (AI keyword-filtered). Twitter/X is intentionally excluded (paid API).

**Cross-cutting hardening (all three):**
- Forced structured output via tool-call schemas (`emit_summary`, `emit_news`)
- Dedup state — `state/processed.json` (talks) + `state/news_seen.json` (news), committed back to the repo each run
- Quality gate — short/bad transcript on a long video → parked in `content/_review/`, **not** published
- Corrupt-state halt — a bad state file stops the run loudly (never silently re-publishes the backlog)
- Token / webhook URL masking in all log output
- Commit-retry safety — rebase conflicts abort rather than push a broken state file
- Optional Telegram broadcast + DM failure alerts to you

---

## B. Community bot — `bersama-bot/` (the MEE6 clone)

discord.py event bot. Privileged intents **required**: Server Members + Message Content.

| # | Feature | What it does | MEE6 equivalent | Status |
|---|---|---|---|---|
| B1 | **Welcome + auto-role** | Greets new members in `#welcome`, assigns `@Newcomer` on join | Welcome + Autorole | 🟡 pending |
| B2 | **Self-assign reaction roles** | 5 emoji → role, add/remove on react | Reaction Roles | 🟡 pending |
| B3 | **Leveling** | MEE6-curve XP per message · `/rank` `/leaderboard` · auto role-rewards at Lv 5/10/20/35 | Leveling + Premium role-rewards | 🟡 pending |
| B4 | **Prefix commands** | `!rules` · `!resources` · `!ai` | Custom Commands | 🟡 pending |
| B5 | **`@BersamaAi` / `!ai` AI** | GLM-5.2 replies, async + cost-guardrailed | "AI" tier | 🟡 pending |

**B5 cost guardrails** (tune in `bot.py` top constants):
- 30 s per-user cooldown · 20 calls/min server-wide · 3 concurrent max
- Input truncated to 1 500 chars · 800-token reply cap
- Disable AI entirely by clearing `ZAI_API_KEY` — everything else still works

---

## C. Infrastructure & ops

| # | Feature | What it does | Status |
|---|---|---|---|
| C1 | **discord-mcp jar (SaseQ)** | Your interactive admin + Claude-for-owner, via claude.ai connector, `localhost:8085` | 🟡 pending |
| C2 | **Discord native AutoMod** | Free spam/profanity/link blocking → `#mod-log` + timeout. Replaces MEE6 automod, no bot | 🟡 pending |
| C3 | **Scheduled messages** | Channel housekeeping via MCP cron | 🟡 pending |
| C4 | **Live server shell** | 14 roles, 8 categories / 27 channels, permission overwrites, onboarding flow, reaction-role menu | 🟢 live |
| C5 | **Seeded content** | `#curated-resources` (5 talks) + `#tools-directory` (~35 tools) already posted | 🟢 live |

### Deliberately out of scope
- ❌ **Music** — discontinued ecosystem-wide (YouTube killed it Feb 2023; even MEE6 dropped it)
- ❌ Heavy game/economy plugins
- ❌ Custom web dashboard (configure via `bersama-bot/config.json` instead)
- ❌ XHS/TikTok auto-post (no open APIs for individuals — use the manual Threads/FB paste bundle)

---

## Schedules quick-reference

| Pipeline | Cron (UTC) | Local (MYT, UTC+8) | Workflow file |
|---|---|---|---|
| Talk summarizer — daily scan | `3 1 * * *` | ~09:03 daily | `daily-summary.yml` |
| News digest | `17 */3 * * *` | every 3h at :17 (11:17, 14:17, 17:17…) | `news-digest.yml` |
| On-demand summarizer | manual | — | `on-demand.yml` |

> GitHub Actions cron runs in **UTC** and can drift ~5–15 min under load. Off-peak minute marks are intentional.

---

## Config & secrets — where each lives

| Key | pipeline `.env` | bot `.env` | GH repo secret | GH repo variable |
|---|:---:|:---:|:---:|:---:|
| `ZAI_API_KEY` | ✅ | ✅ | ✅ | — |
| `ZAI_BASE_URL` | ✅ | ✅ | — | ✅ |
| `GLM_MODEL` | ✅ | ✅ | — | ✅ |
| `DISCORD_WEBHOOK_URL` (`#curated-resources`) | ✅ | — | ✅ | — |
| `DISCORD_NEWS_WEBHOOK_URL` (`#subscription-value`) | ✅ | — | ✅ | — |
| `TELEGRAM_BOT_TOKEN` / `CHANNEL_ID` / `DM_CHAT_ID` | ✅ optional | — | ✅ optional | — |
| `DISCORD_TOKEN` | — | ✅ | — | — |
| `MAX_DURATION_MIN` | ✅ | — | — | ✅ |

⚠️ **Two places to keep in sync:** `DISCORD_TOKEN` (MCP jar + bot) and `ZAI_API_KEY` (pipeline + bot). Rotating either means updating both.

---

## Quick ops runbook

| I want to… | Do this |
|---|---|
| Smoke-test the summarizer locally | `python -m pipeline.main --mode url --url "<YT_URL>" --dry-run` |
| Force one summary now | GitHub → Actions → **on-demand** → paste URL |
| Test news digest locally | `python -m pipeline.main --mode news --dry-run` |
| Run summarizer with no API key | add `--stub-summary`; news: `--stub-news` |
| Start the bot locally | `cd bersama-bot && python bot.py` |
| Edit channels/roles/reaction menus | `bersama-bot/config.json` |
| Trigger admin action via Claude | discord-mcp jar running on `localhost:8085` |
| See why a video was skipped/reviewed | `bersama-ai-pipeline/content/_skipped/` or `_review/` |

---

## Updates log

Append one line per change. Newest at top.

| Date | Change |
|---|---|
| 2026-07-20 | Feature registry created; redundant prototype `pipeline/`, session exports, stray logs, and dead bot config removed. |
| 2026-07-20 | Server went **English-only** (bilingual retired). Talk summarizer + news digest rewritten for English. |
| 2026-07-20 | Summarizer switched Anthropic → **GLM (Z.ai)**; trending news digest pipeline added. |

<!-- When you add a feature, mark its row 🟢 live here and add a dated line above. -->
