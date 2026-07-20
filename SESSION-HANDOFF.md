# Session Handoff — BersamaAi Summarization Pipeline
*Last updated 2026-07-20 · read this first in the next session*

## What this project is
**BersamaAi** — a Malaysia-based AI community on Discord. This session built the
**content engine** that feeds it. Full context lives in memory (`MEMORY.md`:
community overview, English-only decision, discord-mcp setup, research report,
resources seeded, summarization-pipeline) and in `MARKET-RESEARCH-REPORT.md`.

## What got built this session
A standalone Python repo at **`bersama-ai-pipeline/`**, two pipelines, both English-only:

- **Talk summarizer** — YouTube talk → transcript → 5-point summary → auto-post to
  Discord `#curated-resources` (+ optional Telegram) + a Threads/Facebook caption bundle.
- **Trending news digest** — polls Reddit AI subs + Hacker News ~3h → GLM judge picks
  0–4 → auto-post to Discord `#subscription-value`.

Runs on **GitHub Actions**. LLM = **GLM (glm-5.2)** via Z.ai's OpenAI-compatible endpoint.
Code-reviewed + hardened (forced structured output, dedup, quality gate, corrupt-state
halt, token masking, commit-retry safety).

## Git state
Repo `bersama-ai-pipeline/`, **4 commits on `main`, NOT yet pushed to GitHub**:
`544830d` feat → `cec3604` review-hardening → `3c67d84` GLM swap → `4bf43f0` English-only + news + ZAI config.

## ⚠️ The immediate next step (everything is blocked on this)
The user's **`ZAI_API_KEY` is not yet pasted** into `bersama-ai-pipeline/.env` (the line
is staged blank, with `GLM_MODEL=glm-5.2` + `ZAI_BASE_URL=https://api.z.ai/api/coding/paas/v4`
pre-filled). The **live GLM call is the only untested path.** First action next session:
confirm the key is in, then run the smoke test in the setup steps below.

## Setup steps to make it work (in order)
1. Paste `ZAI_API_KEY` into `bersama-ai-pipeline/.env`.
2. Smoke-test: `python -m pipeline.main --mode url --url "https://www.youtube.com/watch?v=aircAruvnKk" --dry-run`
   — see a real summary = ✅. If it 404s, change `ZAI_BASE_URL` to `https://api.z.ai/api/paas/v4` (drop `coding`).
3. Push to GitHub (project not under git remotely): `git remote add origin … && git push -u origin main`.
4. Add 3 repo secrets: `ZAI_API_KEY`, `DISCORD_WEBHOOK_URL`, `DISCORD_NEWS_WEBHOOK_URL`.
5. Create 2 Discord webhooks: `#curated-resources`, `#subscription-value`.
6. Fill `playlists.txt`.
7. First run: Actions → **on-demand** → paste a URL. Then the daily + 3h schedules run themselves.

## Key decisions (don't re-litigate)
- **GLM not Anthropic** — user has a Z.ai key; endpoint = api.z.ai **coding** endpoint; model `glm-5.2`.
- **English-only** — matches the BersamaAi server decision (bilingual retired).
- **GitHub Actions not local** — runs while PC is off.
- **Discord via webhook, not the discord-mcp connector** — MCP is a local stdio process, unreachable from CI. MCP stays for *interactive* use.
- **No XHS/TikTok auto-post** — no open APIs for individuals; manual paste bundle instead.

## File map
```
bersama-ai-pipeline/
├── pipeline/        main.py (entry, llm_creds), fetch.py, summarize.py, news.py,
│                    publish.py, state.py, bundle.py, prompts.py
├── .github/workflows/  daily-summary.yml, on-demand.yml, news-digest.yml
├── .env             ← LOCAL secrets (gitignored); ZAI_API_KEY blank, ready to paste
├── .env.example     ← documents all env names (ZAI_API_KEY/ZAI_BASE_URL/GLM_MODEL)
├── playlists.txt    ← user fills with curated YouTube playlists
├── state/           processed.json (talk dedup), news_seen.json (news dedup)
└── README.md        full setup/usage
```

## Environment notes
- Python 3.10.6, ffmpeg, Node 24 on this Windows machine. `openai`, `yt-dlp`,
  `youtube-transcript-api`, `requests`, `python-dotenv` installed.
- ⚠️ `pip` on PATH points at the **hermes-agent venv** — always use **`python -m pip`**.

## Verification already done
Stub modes pass for both pipelines (summarizer publishes; news gathered 9 real
Reddit/HN candidates + posted one); negative path + corrupt-state halt verified;
code-review pass completed.

## Deferred (v1.1+)
Whisper audio fallback · Pillow card image · Cloudflare Worker one-tap on-demand ·
`#pipeline-alerts` channel · octokit atomic commit.
Full plan: `C:\Users\quekm\.claude\plans\concurrent-wibbling-cocke.md`.
