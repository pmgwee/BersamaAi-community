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
| [`bersama-ai-pipeline/`](bersama-ai-pipeline/) | Content engine — creator-watch summarizer + topic-routed news digest + `@EconomyApp` stock digest + on-demand portal (`/run`, `/share`) | Pipeline VM (LLM uses OpenRouter credits); weekly analytics on GitHub Actions |
| [`bersama-bot/`](bersama-bot/) | discord.py event bot — welcome, reaction roles, leveling, commands, `@mention` AI | GCP VM, systemd (24/7) |
| [`discord-mcp/`](discord-mcp/) | SaseQ discord-mcp jar — interactive admin via the claude.ai connector | On demand (localhost:8085) |
| [`FEATURES.md`](FEATURES.md) | Feature registry & community tracker | — |
| [`MARKET-RESEARCH-REPORT.md`](MARKET-RESEARCH-REPORT.md) | Founding market research | — |
| [`SESSION-HANDOFF.md`](SESSION-HANDOFF.md) | Session-to-session context | — |

## Quick start

Setup is per-component (each has its own `README.md`). The **news digest, creator-watch
summarizer, on-demand portal, `/share`, and `@EconomyApp` stock digest** run on the
pipeline GCP VM. GitHub Actions retains the weekly engagement analytics and a manual,
API-key news fallback. (`SESSION-HANDOFF.md` is a dated
snapshot, not current — use `PROJECT-CONTEXT.md`.)

> **Secret note:** the OpenRouter API key stays in the pipeline VM's `.env` and,
> for the manual fallback only, the GitHub Actions `OPENROUTER_API_KEY` secret.
