# BersamaAi

A Malaysia-based AI community on Discord — a content engine, a community bot,
and admin tooling, all in one repo.

> 👉 **Full feature registry, status, and architecture: [FEATURES.md](FEATURES.md)**

> 👉 **New session, new agent, or new LLM? Start with [PROJECT-CONTEXT.md](PROJECT-CONTEXT.md)** —
> the single-file onboarding doc covering mission, market position, architecture, live
> server state, and decisions log, so you never have to re-explain the project from scratch.

## What's in this repo

| Path | What it is | Runs where |
|---|---|---|
| [`PROJECT-CONTEXT.md`](PROJECT-CONTEXT.md) | **Start here** — full project onboarding doc | — |
| [`bersama-ai-pipeline/`](bersama-ai-pipeline/) | Content engine — creator-watch summarizer + on-demand portal (`/run`, `/share`) + topic-routed news digest | Summarizer + portal + `/share` on GCP VM; news + engagement loop on GitHub Actions |
| [`bersama-bot/`](bersama-bot/) | discord.py event bot — welcome, reaction roles, leveling, commands, `@mention` AI | GCP VM, systemd (24/7) |
| [`discord-mcp/`](discord-mcp/) | SaseQ discord-mcp jar — interactive admin via the claude.ai connector | On demand (localhost:8085) |
| [`FEATURES.md`](FEATURES.md) | Feature registry & community tracker | — |
| [`MARKET-RESEARCH-REPORT.md`](MARKET-RESEARCH-REPORT.md) | Founding market research | — |
| [`SESSION-HANDOFF.md`](SESSION-HANDOFF.md) | Session-to-session context | — |

## Quick start

Setup is per-component (each has its own `README.md`). The GitHub Actions workflows
at the repo root (`.github/workflows/`) drive the **news digest + engagement loop** on
a schedule; the **creator-watch summarizer, the on-demand portal, and `/share`** run on
the GCP VM. (`SESSION-HANDOFF.md` is a dated snapshot, not current — use `PROJECT-CONTEXT.md`.)

> ⚠️ **GitHub Actions note:** the pipeline code lives in `bersama-ai-pipeline/`,
> so the workflows set `working-directory: bersama-ai-pipeline`. Don't move the
> pipeline without updating them — GitHub only reads workflows from the repo root.
