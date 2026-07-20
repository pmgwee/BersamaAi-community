# BersamaAi

A Malaysia-based AI community on Discord — a content engine, a community bot,
and admin tooling, all in one repo.

> 👉 **Full feature registry, status, and architecture: [FEATURES.md](FEATURES.md)**

## What's in this repo

| Path | What it is | Runs where |
|---|---|---|
| [`bersama-ai-pipeline/`](bersama-ai-pipeline/) | Content engine — talk summarizer + trending news digest → `#curated-resources` + `#subscription-value` | GitHub Actions (cloud cron) |
| [`bersama-bot/`](bersama-bot/) | discord.py event bot — welcome, reaction roles, leveling, commands, `@mention` AI | Always-on host (24/7) |
| [`discord-mcp/`](discord-mcp/) | SaseQ discord-mcp jar — interactive admin via the claude.ai connector | On demand (localhost:8085) |
| [`FEATURES.md`](FEATURES.md) | Feature registry & community tracker | — |
| [`MARKET-RESEARCH-REPORT.md`](MARKET-RESEARCH-REPORT.md) | Founding market research | — |
| [`SESSION-HANDOFF.md`](SESSION-HANDOFF.md) | Session-to-session context | — |

## Quick start

Setup is per-component (each has its own `README.md`); the cross-cutting plan
lives in `SESSION-HANDOFF.md`. The GitHub Actions workflows at the repo root
(`.github/workflows/`) drive the pipeline on a schedule and on demand.

> ⚠️ **GitHub Actions note:** the pipeline code lives in `bersama-ai-pipeline/`,
> so the workflows set `working-directory: bersama-ai-pipeline`. Don't move the
> pipeline without updating them — GitHub only reads workflows from the repo root.
