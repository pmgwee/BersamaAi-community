
## On-demand summarizer (paste a URL anytime)
cd ~/bersama/bersama-ai-pipeline && source .venv/bin/activate
python -m pipeline.main --mode url --url "<paste any YouTube URL>"

## Share any URL as a news card (/share)
cd ~/bersama/bersama-ai-pipeline && source .venv/bin/activate
python -m pipeline.main --mode share --url "<any URL — IG Reels / XHS / TikTok / Threads / article / repo>"
# Returns SHARED <topic> on success → posts to the topic's channel webhook.
# Same as the phone portal's /share button.

## Current Engine

Research pass: sources per domain + unified topic-routed engine design + creator-watch design (workflow running)

Your engine question — yes, same engine, and here's why
The engine is topic-parameterized: each topic is just { Reddit subs + GitHub search keywords + target channel }. Creative (image/video/voice) and research (study/productivity) use the exact same gather → LLM-judge → route → dedup pipeline — only the config differs. So no separate engine. One caveat I'll design around: creative-AI also thrives on X/Instagram (paid/hard to reach) — but Reddit + GitHub Trending + HN still capture the bulk of viral creative tools, so the same engine is the right call. Adding a topic later = one config entry + a webhook.

Launching the research + design pass now (6 domains in parallel → unified engine design), then I implement:

## Add .env token into GCP VM 


## REstat handphone on-demand endpoint 

# 1. Stop the old server (it's still holding the pre-pull code from Jul 21)
kill 40387 && sleep 1

# 2. Relaunch with the new code — detached so it survives logout (matches how it ran)
cd ~/bersama/bersama-ai-pipeline && source .venv/bin/activate && nohup python on_demand.py > on_demand.log 2>&1 &

# 3. Confirm it's back up (you should see a NEW pid, not 40387)
sleep 2 && ps -ef | grep on_demand | grep -v grep && echo "--- log ---" && tail -n 8 on_demand.log

pkill -f on_demand.py; sleep 1
cd ~/bersama/bersama-ai-pipeline && source .venv/bin/activate && nohup python on_demand.py > on_demand.log 2>&1 &

nano ~/bersama/bersama-ai-pipeline/.env
