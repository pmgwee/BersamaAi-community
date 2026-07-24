# BersamaAi Summarization Pipeline

The recurring content engine for the BersamaAi Malaysia AI community. **Three modes:**

1. **Talk summarizer** — turns a YouTube talk into a 5-point summary, auto-publishes to **Discord** (`#curated-resources`) and (optionally) **Telegram**, and stages a ready-to-paste bundle for Threads / Facebook.
2. **Trending news digest** — polls per-topic Reddit subs + Hacker News + GitHub Trending every ~3h, an LLM judge tags each item with a topic + heat and routes it to that topic's channel (`#ai-dev-tools`, `#image-creation`, `#video-creation-aigc-tvc`, `#voice-studio`, `#study-with-ai` / `#research-with-ai`, `#earn-money-with-ai`).
3. **On-demand portal + `/share`** — a phone-friendly web UI (`on_demand.py`, port 8080) to summarize one URL (`/run`) or share any URL as a news card (`/share`).

**Runs split across two clouds** (see [`PROJECT-CONTEXT.md`](../PROJECT-CONTEXT.md) for the full picture):
- **GCP VM** — creator-watch summarizer (daily ~09:03 MYT) + the on-demand portal + `/share`. YouTube bot-blocks Azure/GitHub-Actions datacenter IPs but not GCP, and the VM already hosts the always-on bot.
- **GitHub Actions** — the news digest + engagement loop (`.github/workflows/news-digest.yml`, every 3h) and the weekly engagement digest (`engagement-digest.yml`).

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
  per-topic Reddit subs + Hacker News top + GitHub Trending (star-velocity gate)
     │  [gather]  AI-relevant candidates (keyword pre-filter, score-ranked)
     │  [judge]   GLM tags TOPIC + HEAT, picks 0–N worth posting — sober, no hype
     │  [dedupe]  state/news_seen.json
     ▼  [publish] Discord webhook for the topic's channel (coding → #ai-dev-tools, …)

PIPELINE 3 — on-demand portal + /share (GCP VM, manual)
  phone bookmark http://<VM_IP>:8080/?token=…
     │  /run     summarize a YouTube URL → #curated-resources   (mode = url)
     ▼  /share   any URL (IG Reels / XHS / TikTok / Threads → yt-dlp + Groq Whisper)
                  → GLM builds a topic card → that topic's channel webhook  (mode = share)
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
- `[VM]` — the GCP pipeline VM (summarizer + on-demand portal + `/share`): `ZAI_API_KEY`, `GROQ_API_KEY`, `DISCORD_WEBHOOK_URL`, the creative/research webhook URLs, `ON_DEMAND_TOKEN`, Telegram trio.
- `[GH]` — a GitHub Actions secret (news digest + engagement loop): `ZAI_API_KEY`, `GITHUB_TOKEN`, all the webhook URLs, `DISCORD_TOKEN` (⚠️ private repo only), `PREFS_ENABLED`.
- `[both]` — needed in both.

In GitHub Actions, set each `[GH]`/`[both]` value as a repository secret (Settings → Secrets and variables → Actions). Locally and on the VM, keep them in `.env` (gitignored).

### 3. Curate playlists (talk summarizer only)
Edit [`playlists.txt`](playlists.txt) — one YouTube playlist URL per line. Aim for "talks by the people who built the tools".

### 4. Topics (news digest)
Topics live in `pipeline/news.py` `TOPICS` — each is `{ reddit_subs, github_keywords, github_min_stars, channel, webhook_env, live }`. **All 6 are `live=True`.** A topic only actually posts when its `webhook_env` is set.

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

## Notes
- **Why GLM, not Anthropic?** GLM (Z.ai) via its OpenAI-compatible endpoint; matches the project stack and has a cheap coding plan (~US$3/mo). To use Claude instead, swap the client in `summarize.py` / `news.py` to the Anthropic SDK and set `SUMMARY_MODEL`.
- **Why not Twitter/X for the news digest?** X's API is paid ($200/mo Basic) and scraping is fragile. Reddit + HN + GitHub Trending catch the same AI news within hours for free. An X API key can be wired in as an optional source later.
- **Why not the `discord-mcp` connector?** That MCP is a local stdio process for *interactive* Claude Code use; it isn't reachable from cloud cron. This pipeline posts to Discord via webhooks directly.
- **Cost:** GLM is fractions of a cent per run; GitHub Actions free tier covers the news + engagement schedules.
- **Keep `main` unprotected** while relying on the news run's auto-commit (the `GITHUB_TOKEN` can't push to a protected branch). Decide on a bot PAT before enabling branch protection.
