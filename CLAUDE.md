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
| Summarizer + on-demand portal (`on_demand.py`, port 8080) + `/share` | GCP VM (`~/bersama/bersama-ai-pipeline/`) |
| Community bot | GCP VM (`~/BersamaAi-community/bersama-bot/`), systemd service `bersama`, 24/7 |
| News digest + engagement loop | GitHub Actions (`.github/workflows/news-digest.yml` every 3h; `engagement-digest.yml` weekly) |
| discord-mcp admin jar | Local only, on-demand (`localhost:8085`) |

> **One GCP VM, two checkouts** — the same machine hosts `~/bersama/bersama-ai-pipeline/` (summarizer + portal + `/share`) AND `~/BersamaAi-community/` (the bot). There is no separate "news VM": the **news digest + engagement loop are NOT on the VM — they run on GitHub Actions**, so changes to news/engagement code take effect on the next scheduled run with **no VM action** (just push).

The bot and the MCP jar deliberately **share one Discord bot token** (Discord allows concurrent Gateway sessions). If the token is reset, update both `.env` files together.

## Key facts
- **LLM = GLM `glm-5.2`** via Z.ai's OpenAI-compatible endpoint (`https://api.z.ai/api/coding/paas/v4`) — chosen for cost (~US$3/mo), **NOT Anthropic**. Don't re-litigate without a specific reason.
- **English-only** server (bilingual was tried and retired 2026-07-20). Card *content* may keep a source's original language (relaxed 2026-07-22); channel names / UI / the small category badge stay English.
- **Docker Desktop is broken on this machine** → `discord-mcp` runs as a native Java 19 JAR via `run.cmd`, not via Docker.
- **All 6 news topics are `live=True`** in `pipeline/news.py` `TOPICS` (docstring updated 2026-07-23; trust the `TOPICS` table).
- **Reddit `.json` is 403-blocked for anonymous clients** (since ~2026-07). `fetch_reddit` tries OAuth JSON (`REDDIT_CLIENT_ID/SECRET` — real upvote counts) and falls back to public multireddit RSS (works unauthenticated; cards show `hot #N` rank instead of upvotes). News dedup filters already-posted stories **before** the judge; a real run that posts 0 cards warns `#staff-chat`.
- The `research_*` topics' `Topic.channel` is labeled `"#education"`, but no `#education` channel exists — `DISCORD_EDUCATION_WEBHOOK_URL` actually posts to `#study-with-ai` / `#research-with-ai`.

## Common commands
```bash
# Pipeline (inside bersama-ai-pipeline/ with the venv active)
python -m pipeline.main --mode scheduled              # daily creator-watch scan
python -m pipeline.main --mode url --url "<YT>"       # summarize one video
python -m pipeline.main --mode news                   # trending news digest
python -m pipeline.main --mode share --url "<URL>"    # share any URL as a news card
python on_demand.py                                   # the phone portal (port 8080)

# Bot (on the VM — systemd manages it)
sudo systemctl status bersama
sudo systemctl restart bersama && tail -n 4 ~/BersamaAi-community/bersama-bot/bersama.log
```

## Env & secrets
Each component has a complete, tagged `.env.example` (copy → `.env`; never commit the real one):
- `bersama-ai-pipeline/.env.example` — every key tagged `[VM]` / `[GH]` / `[both]`.
- `bersama-bot/.env.example` — `DISCORD_TOKEN`, `ZAI_API_KEY`/`ZAI_BASE_URL`/`GLM_MODEL`, optional `JINA_API_KEY`.

Bot channels / roles / levels live in `bersama-bot/config.json`, not env.

## Task completion workflow (do this every time a task is done)
Per the owner's standing instruction: **commit + push finished work to `main`
automatically on task completion** (the repo is main-based; CI also commits state
to main). Mid-task WIP commits still need asking.

1. **Before staging** — confirm only intended files changed: `git diff --ignore-cr-at-eol --stat`.
   Never `git add -A` blind: local pipeline runs rewrite `state/*.json` (CI owns those)
   and Windows CRLF can create phantom whole-file diffs. Stage only the files you edited.
2. **Commit + push** to `main`. End the commit message with the co-author trailer.
3. **Hand back the sync command — decide by WHAT CHANGED.** It's one GCP VM with two checkouts; the news digest is not on the VM:

   | What you changed | VM pull? | Restart? |
   |---|---|---|
   | **News / engagement pipeline** (`pipeline/news.py`, `engagement*.py`, `preferences.py`, `.github/workflows/news-digest.yml`) | **No** — runs on GitHub Actions; picks up pushed code on the next scheduled run | no |
   | **Summarizer / portal / `/share`** (other `bersama-ai-pipeline/` code, `on_demand.py`, `playlists.txt`) | `cd ~/bersama/bersama-ai-pipeline && git pull --ff-only` | restart portal only if a runtime change (see below) |
   | **Bot** (`bersama-bot/`, `config.json`) | `cd ~/BersamaAi-community && git pull --ff-only` | `sudo systemctl restart bersama` |
   | **discord-mcp** | n/a — local only (this machine) | — |
   | **Docs only** (`*.md`) | no | no |

   Portal restart (only if `on_demand.py`/`/share`/summarizer runtime changed): `pkill -f on_demand.py; cd ~/bersama/bersama-ai-pipeline && source .venv/bin/activate && nohup python on_demand.py > on_demand.log 2>&1 &`

   > Note: `news.py` *lives* under `bersama-ai-pipeline/`, but the news digest is executed by GitHub Actions — so a `news.py` change needs **no VM pull** to reach production (pulling the pipeline VM is optional, just to keep its copy in sync for local testing).
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
