
## On-demand summarizer (paste a URL anytime)
cd ~/bersama/bersama-ai-pipeline && source .venv/bin/activate
python -m pipeline.main --mode url --url "<paste any YouTube URL>"

## Current Engine

Research pass: sources per domain + unified topic-routed engine design + creator-watch design (workflow running)

Your engine question — yes, same engine, and here's why
The engine is topic-parameterized: each topic is just { Reddit subs + GitHub search keywords + target channel }. Creative (image/video/voice) and research (study/productivity) use the exact same gather → GLM-judge → route → dedup pipeline — only the config differs. So no separate engine. One caveat I'll design around: creative-AI also thrives on X/Instagram (paid/hard to reach) — but Reddit + GitHub Trending + HN still capture the bulk of viral creative tools, so the same engine is the right call. Adding a topic later = one config entry + a webhook.

Launching the research + design pass now (6 domains in parallel → unified engine design), then I implement:

## Add .env token into GCP VM 
