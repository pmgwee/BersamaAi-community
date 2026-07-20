# BersamaAi Summarization Pipeline

The recurring content engine for the BersamaAi Malaysia AI community. **Two pipelines:**

1. **Talk summarizer** — turns a YouTube talk into a 5-point **English** summary, auto-publishes to **Discord** (`#curated-resources`) and (optionally) **Telegram**, and stages a ready-to-paste bundle for Threads / Facebook.
2. **Trending news digest** — polls Reddit AI subs + Hacker News every ~3h, an LLM judge picks the few items worth posting (launches, pricing, resets, benchmarks, open-source), and posts them to **Discord** (`#subscription-value`).

Runs on **GitHub Actions** (cloud cron) so it works while your PC is off. **English-only** (matches the server decision, 2026-07-20).

```
PIPELINE 1 — talk summarizer
  YouTube talk
     │  [fetch]    yt-dlp captions (json3) — fallback: youtube-transcript-api
     │  [summarize] GLM glm-4.6 · forced tool call → exactly 5 English points
     │  [gate]     short/bad transcript? → content/_review/, do NOT publish
     │  [publish]  Discord webhook (embed) + Telegram  ── auto
     │  [stage]    content/<date>_<slug>_<id>/ (post.threads.txt, post.facebook.txt) ── manual paste
     ▼  [state]    state/processed.json (dedup by video_id, committed back)

PIPELINE 2 — trending news digest
  Reddit (r/LocalLLaMA, r/singularity, r/OpenAI, r/ClaudeAI) + Hacker News top
     │  [gather]  AI-relevant candidates (keyword pre-filter, score-ranked)
     │  [judge]   GLM picks 0–4 worth posting — category-tagged, sober, no hype
     │  [dedupe]  state/news_seen.json
     ▼  [publish] Discord webhook (#subscription-value)
```

## Setup

### 1. Install (local dev / testing)
```powershell
cd bersama-ai-pipeline
python -m pip install -r requirements.txt
python -m pip install -U yt-dlp
```
> On this machine `pip` points at a different venv — always use **`python -m pip`**.

### 2. Provision secrets (GitHub → Settings → Secrets and variables → Actions)
| Secret | What it is | How to get it |
|---|---|---|
| `GLM_API_KEY` | the LLM (summarizer + news judge) | Zhipu `open.bigmodel.cn` **or** Z.ai `z.ai` → API Keys |
| `DISCORD_WEBHOOK_URL` | webhook for `#curated-resources` | channel → Integrations → Webhooks → New |
| `DISCORD_NEWS_WEBHOOK_URL` | webhook for `#subscription-value` (news digest) | same — falls back to the one above if unset |
| `TELEGRAM_BOT_TOKEN` | _(optional)_ posting bot | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `TELEGRAM_CHANNEL_ID` | _(optional)_ channel the bot posts to (bot must be admin) | `@channelname` or `-100…` |
| `TELEGRAM_DM_CHAT_ID` | _(optional)_ your chat id for failure alerts | [@userinfobot](https://t.me/userinfobot) |

Locally, copy `.env.example` → `.env` and fill the same values (gitignored).

### 3. Curate playlists (talk summarizer only)
Edit [`playlists.txt`](playlists.txt) — one YouTube playlist URL per line. Aim for "talks by the people who built the tools".

## Usage

### Talk summarizer — on-demand (one URL you just found)
GitHub → Actions → **on-demand** → Run workflow → paste a URL. Locally:
```powershell
python -m pipeline.main --mode url --url "https://www.youtube.com/watch?v=..."
```

### Talk summarizer — daily scheduled scan
Runs automatically at ~09:03 MYT. To test locally:
```powershell
python -m pipeline.main --mode scheduled
```

### Trending news digest
Runs automatically every ~3h. To test locally:
```powershell
python -m pipeline.main --mode news
```

### Flags
- `--dry-run` — build everything, print the Discord/Telegram payloads, **don't post**.
- `--stub-summary` / `--stub-news` — **local test only**: canned output, skip the LLM call (no API key needed).

## Output (talk summarizer)
Each processed video writes a folder under `content/<YYYY-MM-DD>_<slug>_<id>/`:
- `summary.md` — the canonical English 5-point summary
- `post.threads.txt`, `post.facebook.txt` — English captions, ready to paste
- `source.json` — metadata + structured summary
- `transcript.txt` — the fetched transcript (gitignored — large + soft copyright concern)

Quality-gate failures park in `content/_review/`; no-caption / too-long videos log to `content/_skipped/`. Both can alert you on Telegram.

## Notes
- **Why GLM, not Anthropic?** GLM (Zhipu / Z.ai) via its OpenAI-compatible endpoint; matches the project stack and has a free tier. To use Claude instead, swap the client in `summarize.py` / `news.py` to the Anthropic SDK and set `SUMMARY_MODEL`.
- **Why not Twitter/X for the news digest?** X's API is paid ($200/mo Basic) and scraping is fragile. Reddit + HN catch the same AI news within hours for free. An X API key can be wired in as an optional source later.
- **Why not the `discord-mcp` connector?** That MCP is a local stdio process for *interactive* Claude Code use; it isn't reachable from GitHub Actions CI. This pipeline posts to Discord via webhooks directly.
- **Cost:** GLM is fractions of a cent per run; GH Actions free tier covers both schedules.
- **Keep `main` unprotected** while relying on the auto-commit (the `GITHUB_TOKEN` can't push to a protected branch). Decide on a bot PAT before enabling branch protection.
