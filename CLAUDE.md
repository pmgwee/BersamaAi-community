# CLAUDE.md

Project memory for Claude Code sessions working on **BersamaAi** — a Malaysia-based
AI community on Discord. This file is the fast-load pointer; **[PROJECT-CONTEXT.md](PROJECT-CONTEXT.md)
is the full source of truth** and [FEATURES.md](FEATURES.md) is the feature registry.

## What this repo is
Three independent components that all reach the same Discord server (guild `1528524602861420625`):
- `bersama-ai-pipeline/` — content engine (Python): creator-watch YouTube summarizer, topic-routed news digest, on-demand portal + `/share`-as-news-card.
- `bersama-bot/` — discord.py event bot (welcome, reaction roles, leveling, commands, `@mention` AI).
- `discord-mcp/` — SaseQ admin jar for interactive owner access via Claude / claude.ai.

## Where things run
| Component | Runs where |
|---|---|
| Summarizer + on-demand portal (`on_demand.py`, port 8080) + `/share` | **Pipeline VM** `beresama-ai-news-pipelines` (`~/bersama/bersama-ai-pipeline/`) |
| Community bot | **Bot VM** `bersama-ai-bot` (`~/BersamaAi-community/bersama-bot/`), systemd service `bersama`, 24/7 |
| News digest + engagement loop | GitHub Actions (`.github/workflows/news-digest.yml` every 3h; `engagement-digest.yml` weekly) |
| Stock digest (`@EconomyApp` → `#stock-financial-report`) | **Pipeline VM** cron (no LLM) — but the fetch is now **IP-agnostic**, so it could equally run on GitHub Actions. Reads `@EconomyApp`'s **Bluesky mirror** (`did:plc:kio5ffqovakoioxtxbuat6mr`) via the public AT Protocol API: free, keyless, cookieless. X blocks anonymous scraping from datacenter IPs; **RSSHub + `TWITTER_AUTH_TOKEN` was abandoned** because the cookie expires and the job rots silently. See [STOCK-DIGEST-CHALLENGES.md](STOCK-DIGEST-CHALLENGES.md). |
| Serenity digest (`@aleabitoreddit` → `#serenity-x-posts`) | **Pipeline VM** cron 01:07 UTC (uses GLM for topic tags). Reads **trackserenity.com's public `signals.json`** (the same feed as the subscription-agent Stocks Page — his Bluesky account is stale since 2026-07-21), pills every `$TICKER` (cashtags ∪ regex), and pulls post photos via fxtwitter's keyless API (optional, tolerant). |
| discord-mcp admin jar | Local only, on-demand (`localhost:8085`) |

> **TWO GCP VMs** (split 2026-08-07; was one VM with two directories):
> - **Pipeline VM** `beresama-ai-news-pipelines` — checkout `~/bersama/` (repo root); runs the pipeline from `bersama-ai-pipeline/`: summarizer cron + on-demand portal (`on_demand.py` :8080) + `/share` + the `@EconomyApp` stock-digest cron. `pipeline/news.py` *lives* here, though the news *digest* runs on GitHub Actions.
> - **Bot VM** `bersama-ai-bot` — checkout `~/BersamaAi-community/` (repo root); runs `bersama-bot/` (systemd `bersama`, 24/7).
>
> The **news digest + engagement loop run on GitHub Actions, NOT on either VM.** (The old "no news VM" line meant the *digest* isn't VM-cron-scheduled — true — but the pipeline/portal/summarizer now have their own dedicated VM.)
>
> **Rule: every code change must explicitly name WHICH checkout (pipeline vs bot) AND which VM it lives on, and whether that VM needs a pull.** News/engagement code lives in the pipeline checkout but runs on GH Actions → **no VM pull**. Never leave "which checkout/VM" for the owner to guess — say it every time.

The bot and the MCP jar deliberately **share one Discord bot token** (Discord allows concurrent Gateway sessions). If the token is reset, update both `.env` files together.

## Key facts
- **LLM = GLM `glm-5.2`** via Z.ai's OpenAI-compatible endpoint (`https://api.z.ai/api/coding/paas/v4`) — chosen for cost (~US$3/mo), **NOT Anthropic**. Don't re-litigate without a specific reason.
- **English-only** server (bilingual was tried and retired 2026-07-20). Card *content* may keep a source's original language (relaxed 2026-07-22); channel names / UI / the small category badge stay English.
- **Docker Desktop is broken on this machine** → `discord-mcp` runs as a native Java 19 JAR via `run.cmd`, not via Docker.
- **All 9 news topics are `live=True`** in `pipeline/news.py` `TOPICS` (coding / creative_image / creative_video / creative_voice / research_study / research_productivity / finance / company_investment / cybersecurity). `company_investment` + `cybersecurity` were added 2026-08-07 to peel deals/strategy and security beats out of the old `coding` catch-all. Trust the `TOPICS` table.
- **Reddit `.json` is 403-blocked for anonymous clients** (since ~2026-07). `fetch_reddit` tries OAuth JSON (`REDDIT_CLIENT_ID/SECRET` — real upvote counts) and falls back to public multireddit RSS (works unauthenticated; cards show `hot #N` rank instead of upvotes). News dedup filters already-posted stories **before** the judge; a real run that posts 0 cards warns `#staff-chat`.
- The `research_*` topics' `Topic.channel` is `"#research-with-ai"` (channel renamed from `#study-with-ai` on 2026-08-07; webhook env `DISCORD_RESEARCH_WEBHOOK_URL`, legacy alias `DISCORD_EDUCATION_WEBHOOK_URL`). Both `research_study` and `research_productivity` post there.

## Common commands
```bash
# Pipeline (inside bersama-ai-pipeline/ with the venv active)
python -m pipeline.main --mode scheduled              # daily creator-watch scan
python -m pipeline.main --mode url --url "<YT>"       # summarize one video
python -m pipeline.main --mode news                   # trending news digest
python -m pipeline.main --mode x-digest               # @EconomyApp → #stock-financial-report (Bluesky mirror; no LLM, no auth)
python -m pipeline.main --mode serenity               # @Serenity → #serenity-x-posts (trackserenity.com mirror + GLM topic tags + $ticker pills + images)
python -m pipeline.main --mode share --url "<URL>"    # share any URL as a news card
python on_demand.py                                   # the phone portal (port 8080)

# Bot (on the BOT VM `bersama-ai-bot` — systemd manages it)
sudo systemctl status bersama
sudo systemctl restart bersama && tail -n 4 ~/BersamaAi-community/bersama-bot/bersama.log
```

### Creator-watch summarizer daily cron (the PIPELINE VM's crontab — `crontab -e`)
```cron
# creator-watch summarizer → #youtube-ai-video, once a day. 01:03 UTC = 09:03 MYT.
# run-daily.sh (VM-only, NOT in the repo) cds into the pipeline dir (load-bearing
# for .env), activates .venv, runs `python -m pipeline.main --mode scheduled`, and
# logs to logs/daily.log. It does NOT git pull (avoids state/ conflicts), so code
# fixes need a manual `cd ~/bersama/bersama-ai-pipeline && git pull --ff-only`
# before the next 09:03 MYT run picks them up.
3 1 * * * /home/ngxiaohao123/bersama/bersama-ai-pipeline/run-daily.sh
```
> The script ends with `|| true`, so cron always reports success — failures surface only via the `alert()` → 🔒-staff-chat path (set `DISCORD_STAFF_CHAT_WEBHOOK_URL`) and in `logs/daily.log`. Safe manual test: `cd ~/bersama/bersama-ai-pipeline && source .venv/bin/activate && python -m pipeline.main --mode scheduled --dry-run` (posts/marks nothing).

### Stock-digest daily cron (the PIPELINE VM's crontab — `crontab -e`)
```cron
# @EconomyApp → #stock-financial-report, once a day. 01:00 UTC = 09:00 MYT, ~4h after the
# US close, so the previous session's earnings posts are already mirrored.
0 1 * * * cd /home/ngxiaohao123/bersama/bersama-ai-pipeline && ./.venv/bin/python -m pipeline.main --mode x-digest >> /home/ngxiaohao123/bersama/x-digest.log 2>&1
```
> ⚠️ The `cd` is **load-bearing**: `pipeline/main.py` calls bare `load_dotenv()`,
> which resolves `.env` from the **current working directory**. Without the `cd`
> the webhook env var is unset and the run silently posts nothing (`X_NO_WEBHOOK`
> in the log) instead of failing. Missing a day is harmless — `MAX_AGE_DAYS=7`
> means the next run picks up anything from the last week.

### Serenity-digest daily cron (the PIPELINE VM's crontab — `crontab -e`)
```cron
# @Serenity (@aleabitoreddit, the AI-semis stock-picker) → #serenity-x-posts, once a
# day. 01:07 UTC = 09:07 MYT — staggered 7 min after the stock digest. Reads
# trackserenity.com's PUBLIC /data/signals.json (same feed as the subscription-agent
# Stocks Page), tags 1-4 topics via GLM (needs ZAI_API_KEY; keyword-rule fallback),
# pills every $TICKER (cashtags ∪ $-regex), and attaches the post's photo via
# fxtwitter when it has one. Same load-bearing `cd` as the stock digest.
7 1 * * * cd /home/ngxiaohao123/bersama/bersama-ai-pipeline && ./.venv/bin/python -m pipeline.main --mode serenity >> /home/ngxiaohao123/bersama/serenity-digest.log 2>&1
```
> He posts ~5.3/day (cap 12/run; first run posts only 3 back-cards). Staleness guards mirror x-digest: feed unreachable/empty, or newest post ≥ 4 days old → 🔒-staff-chat alert. Safe manual test: `cd ~/bersama/bersama-ai-pipeline && source .venv/bin/activate && python -m pipeline.main --mode serenity --dry-run`.

## Env & secrets
Each component has a complete, tagged `.env.example` (copy → `.env`; never commit the real one):
- `bersama-ai-pipeline/.env.example` — every key tagged `[VM]` / `[GH]` / `[both]`.
- `bersama-bot/.env.example` — `DISCORD_TOKEN`, `ZAI_API_KEY`/`ZAI_BASE_URL`/`GLM_MODEL`, optional `JINA_API_KEY`.

Bot channels / roles / levels live in `bersama-bot/config.json`, not env.

### Discord webhooks (env var → channel) — the canonical map
| Env var | Channel |
|---|---|
| `DISCORD_YOUTUBE_WEBHOOK_URL` (legacy `DISCORD_WEBHOOK_URL`) | `#youtube-ai-video` — creator-watch summarizer (was `#youtube-resources` / `#curated-resources`) |
| `DISCORD_LLM_TOOLS_WEBHOOK_URL` (legacy `DEVTOOLS` / `NEWS`) | `#ai-llm-tools` — coding / LLM / agent news (channel was `#ai-dev-tools`) |
| `DISCORD_IMAGE_CREATION_WEBHOOK_URL` | `#image-creation` |
| `DISCORD_VIDEO_CREATION_WEBHOOK_URL` | `#video-creation-aigc-tvc` |
| `DISCORD_VOICE_STUDIO_WEBHOOK_URL` | `#voice-studio` |
| `DISCORD_RESEARCH_WEBHOOK_URL` (legacy `EDUCATION`) | `#research-with-ai` (channel was `#study-with-ai`) |
| `DISCORD_EARN_MONEY_WEBHOOK_URL` (legacy `FINANCE`) | `#earn-money-with-ai` — individual / builder making money WITH AI |
| `DISCORD_COMPANY_INVESTMENT_WEBHOOK_URL` | `#ai-company-investment` — AI INDUSTRY money / strategy / policy: M&A, funding, chips/compute, pricing, leadership, open-weight POLICY (broad scope) |
| `DISCORD_CYBERSECURITY_BYPASS_WEBHOOK_URL` | `#ai-cybersecurity-bypass` — AI security as the subject: incidents, jailbreaks, eval escapes, AI-found vulns, cyber-purpose model/tool launches |
| `DISCORD_STOCK_FINANCIAL_REPORT_WEBHOOK_URL` (was `STOCK_INVEST`) | `#stock-financial-report` (was `#stock-invest`) — `@EconomyApp` daily digest (VM cron, via the account's Bluesky mirror; no auth) |
| `DISCORD_SERENITY_X_POSTS_WEBHOOK_URL` | `#serenity-x-posts` — `@aleabitoreddit` ("Serenity") post tracker: topic-tag + `$TICKER`-pill cards (VM cron `--mode serenity`; GLM topics + keyword fallback, images via fxtwitter) |
| `DISCORD_STAFF_CHAT_WEBHOOK_URL` | `🔒-staff-chat` — health warnings + weekly digest; **topic cards must never post here** (enforced by `_is_staff_webhook`) |

The two renamed vars are read new-name-first with the legacy name as fallback, so a half-migrated `.env` keeps working.

## Task completion workflow (do this every time a task is done)
Per the owner's standing instruction: **commit + push finished work to `main`
automatically on task completion** (the repo is main-based; CI also commits state
to main). Mid-task WIP commits still need asking.

1. **Before staging** — confirm only intended files changed: `git diff --ignore-cr-at-eol --stat`.
   Never `git add -A` blind: local pipeline runs rewrite `state/*.json` (CI owns those)
   and Windows CRLF can create phantom whole-file diffs. Stage only the files you edited.
2. **Commit + push** to `main`. End the commit message with the co-author trailer.
3. **Hand back the sync command — decide by WHAT CHANGED.** It's TWO GCP VMs — pipeline `beresama-ai-news-pipelines` (`~/bersama/`) and bot `bersama-ai-bot` (`~/BersamaAi-community/`); the news digest runs on neither (GitHub Actions):

   | What you changed | VM pull? | Restart? |
   |---|---|---|
   | **News / engagement pipeline, gather-side** (`pipeline/news.py` sources+quotas, `engagement*.py`, `preferences.py`, `.github/workflows/news-digest.yml`) | **No** — runs on GitHub Actions; picks up pushed code on the next scheduled run | no |
   | **`news.py` code that `/share` also executes** (see the ⚠️ below) | `cd ~/bersama/bersama-ai-pipeline && git pull --ff-only` | portal restart |
   | **Summarizer / portal / `/share`** (other `bersama-ai-pipeline/` code, `on_demand.py`, `playlists.txt`) | `cd ~/bersama/bersama-ai-pipeline && git pull --ff-only` | restart portal only if a runtime change (see below) |
   | **Bot** (`bersama-bot/`, `config.json`) | `cd ~/BersamaAi-community && git pull --ff-only` | `sudo systemctl restart bersama` |
   | **discord-mcp** | n/a — local only (this machine) | — |
   | **Docs only** (`*.md`) | no | no |

   Portal restart (only if `on_demand.py`/`/share`/summarizer runtime changed): `pkill -f on_demand.py; cd ~/bersama/bersama-ai-pipeline && source .venv/bin/activate && nohup python on_demand.py > on_demand.log 2>&1 &`

   > Note: `news.py` *lives* under `bersama-ai-pipeline/`, but the news **digest** is executed by GitHub Actions — so a digest-only `news.py` change needs **no VM pull** to reach production (pulling the pipeline VM is optional, just to keep its copy in sync for local testing).
   >
   > ⚠️ **`news.py` is NOT purely a GH-Actions file.** The VM's `/share` shells out to
   > `python -m pipeline.main --mode share`, which imports `pipeline/news.py`. So decide by
   > WHICH SYMBOLS you touched, not by the filename:
   > - **Gather-side → no VM pull.** `TOPICS`' `reddit_subs`/`github_keywords`, `AI_KEYWORDS`,
   >   `OFFICIAL_RSS`, `fetch_*`, `gather_candidates`, quotas/`LOCAL_LIMIT`, `SYSTEM_PROMPT`
   >   (the *digest* judge). `/share` never calls any of these.
   > - **Shared with `/share` → VM pull + portal restart.** `SINGLE_CARD_PROMPT`, `EMIT_ONE_TOOL`,
   >   `NewsItem`, `TOPIC_BY_KEY`, a `Topic`'s `key`/`channel`/`webhook_env`, `_topic_webhook`,
   >   `fetch_url_meta`, the card builder / posting / thumbnail code, `post_url_as_news`.
   >
   > When unsure, check it instead of guessing:
   > `sed -n '/^def post_url_as_news/,/^def run_news/p' bersama-ai-pipeline/pipeline/news.py`

   **Rule: EVERY task handoff must END with a sync block naming the EXACT TARGET** — always
   present, even when the answer is "nothing to do", and never left for the owner to infer.
   State it as exactly one of these three, copy-pasteable:
   - **No VM action — GitHub Actions** (gather-side news/engagement, docs)
   - **Pull the pipeline VM** (`beresama-ai-news-pipelines`): `cd ~/bersama/bersama-ai-pipeline && git pull --ff-only` (+ portal restart line if runtime changed)
   - **Pull the bot VM** (`bersama-ai-bot`): `cd ~/BersamaAi-community && git pull --ff-only && sudo systemctl restart bersama`

   Say **why** in half a sentence ("gather-side only, `/share` unaffected"), so the owner can
   sanity-check the call rather than trust it blindly. If a change spans both checkouts, give
   BOTH commands — never just the one that seems more important. And if the work is committed
   but **not pushed**, say that first: unpushed work reaches neither the VM nor GitHub Actions.
4. **Flag any new env/secrets**, and say *where* each must be set (these are NOT interchangeable):
   - **GitHub repo secret** (Settings → Secrets and variables → Actions) → anything the news-digest / engagement workflows read.
   - **VM `.env`** (`~/bersama/bersama-ai-pipeline/.env`) → summarizer + on-demand portal.
   - **Local `.env`** (`bersama-ai-pipeline/.env`) → local dev/test only; never the source of truth.
   Verify a webhook/token with a GET before declaring done (Discord webhooks return 200) — a working local value does NOT prove the GH secret is set.

## Conventions
- Match existing doc style (tables, emoji status markers 🟢🟡⚪, terse prose).
- Don't reintroduce Chinese channel names / content.
- Historical point-in-time docs (`MARKET-RESEARCH-REPORT.md`, `SESSION-HANDOFF.md`, `NEWS-ENGINE-REVIEW.md`, `ENGAGEMENT-LOOP-PLAN.md`) are dated snapshots — don't "update" them; edit the live trackers instead.
- See **Task completion workflow** above for the commit/push + VM-handoff default.
