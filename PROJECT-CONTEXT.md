# BersamaAi — Project Context
*The one document to hand to any agent, new session, or LLM to onboard them instantly. Last updated 2026-07-23.*

> **How to use this file:** paste this whole document as context when starting a new
> session, briefing a subagent, or switching LLMs. For deeper detail on any section,
> follow the pointers to `MARKET-RESEARCH-REPORT.md`, `FEATURES.md`, or the
> component READMEs — this file is the map, not the full territory.

---

## 1. What BersamaAi is

**BersamaAi** is a Malaysia-based AI community, currently running on Discord.

- **Mission:** promote AI integration into daily life — make life easier, and help
  everyone develop personalized, tailored AI use cases. Not a developer-only space.
- **Format:** sharing + open discussion + learning + a consolidated AI-resources hub —
  a forum for everyone to learn, play, and share everything about AI.
- **The hook (the emotional core of the brand):** *"everyone can be the CEO of their
  own company — a one-person show."* (一人公司，一人一城，一个人就能让一个整个公司运作)
  AI is framed as the leverage that makes solo entrepreneurship real for ordinary people,
  not just developers.
- **Founder / owner:** the user (Discord `jonathangwee`), operating largely solo with
  AI-agent tooling (this Claude Code project) doing the building and admin work.
- **Server name:** BersamaAi ("bersama" = Malay for "together"). Live since 2026-07-19.
  Invite: `https://discord.gg/HfRZeJMmqn`

---

## 2. Market position (full detail: [`MARKET-RESEARCH-REPORT.md`](MARKET-RESEARCH-REPORT.md))

> ⚠️ Stats in the research report are sourced but not independently verified
> (adversarial fact-check pass was rate-limited) — treat as directional.

**Why now:** Malaysian AI *adoption* is very high (67–93% depending on segment) but
*competence* is shallow — only ~12% of employees get adequate AI training, 73% of
AI-adopting businesses stay at basic usage. BersamaAi sells the adoption-to-competence
bridge.

**Who the member is:** 25–34, Klang Valley-skewed, higher income, already chats with AI
daily (chatbots, not code — only ~10% of MY AI users touch coding tools), wants to go
from "I use ChatGPT" to "AI runs parts of my life/business."

**Whitespace:** every existing Malaysian AI community (Mesolitica's Discord ~300 members,
AI Tinkerers KL, AI/ML Malaysia) serves *builders*. Nobody owns "AI for daily life" for
the mass consumer. That's BersamaAi's lane.

**Competitors studied as models:**
- **Developer Kaki** (~46k, Facebook-first) — free community as top-of-funnel; monetizes
  via a jobs board + sponsorships + the founder's own B2B consulting. **Copy this.**
- **OE杰青商学院** (~1M+ learners claimed, Chinese-language business education) — proves
  Malaysians will pay RM2k–8k for self-improvement education, but carries a visible
  "骗局" (scam) backlash on TikTok from high-pressure seminar funnels. **Avoid this
  reputation trap** — BersamaAi should stay free-first and transparent.

**Platform reality:** Discord is niche in Malaysia (WhatsApp ~90% penetration, Telegram
~56%). Strategic decision: **Discord is the clubhouse, not the growth engine.** Acquire
members via Facebook/XHS/TikTok/Threads (repost curated-resource summaries there),
broadcast via WhatsApp/Telegram, and use Discord for the members who want depth.

**Launch-lean discipline:** dead-server research says a big empty channel list kills
first impressions. Channels expand only when an existing one overflows — never
speculatively.

---

## 3. Current architecture — three independent components

```
┌──────────────────────────────────────────────────────────────────────┐
│                    Discord: BersamaAi server (live)                  │
│      18 roles · 11 categories · seeded content                       │
└──────────────────────────────────────────────────────────────────────┘
       ▲                    ▲                          ▲
       │ webhook            │ Gateway (bot token)      │ Gateway (same token)
       │                    │                          │  + claude.ai / Claude Code
┌──────┴───────┐    ┌───────┴────────┐         ┌───────┴──────────────┐
│ bersama-ai-  │    │ bersama-bot    │         │ discord-mcp (SaseQ)  │
│ pipeline     │    │ (discord.py)   │         │ jar — admin console  │
│              │    │                │         │                       │
│ • summarizer │    │ • welcome      │         │ • on-demand admin     │
│ • on-demand  │    │ • reaction     │         │   via Claude          │
│   portal +   │    │   roles        │         │ • cron timers         │
│   /share     │    │ • leveling     │         └───────────────────────┘
│ • news       │    │ • !commands    │
│   digest     │    │ • @mention AI  │
│ Summarizer + │    │   (GLM-5.2)    │
│ portal → VM  │    │ Runs 24/7 on   │
│ News → GH    │    │ GCP VM         │
└──────────────┘    └────────────────┘
```

| Component | Location | Reaches Discord via | Runs where |
|---|---|---|---|
| **Content engine** | `bersama-ai-pipeline/` | Discord webhook (no token) + bot token (engagement sweep) | **Summarizer + on-demand portal + `/share`**: GCP VM · **News + engagement loop**: GitHub Actions |
| **Community bot** | `bersama-bot/` | Bot token (Gateway, always-on) | GCP VM, systemd service `bersama` |
| **Admin console** | `discord-mcp/` (SaseQ jar) | Same bot token, concurrent Gateway session | Local, on-demand (this machine) |

The bot and the MCP jar **deliberately share one Discord bot token** — Discord allows
concurrent Gateway sessions per token. If the token is ever reset, both `.env` files
must be updated together.

**LLM powering the bot + pipeline:** **GLM `glm-5.2`** via Z.ai's OpenAI-compatible
endpoint (`https://api.z.ai/api/coding/paas/v4`) — chosen for cost (~US$3/month GLM
Coding Plan vs. Anthropic), not Claude. This is a deliberate, already-settled decision —
don't re-litigate it without a specific reason.

### A. Content engine — `bersama-ai-pipeline/`
English-only (card *content* may keep a source's original language since 2026-07-22). **Summarizer + on-demand portal + `/share`** run on the GCP VM; **news digest + engagement loop** run on GitHub Actions.
1. **Creator-watch summarizer** — watches a short list of YouTube channels (`playlists.txt`;
   currently Kelly Tsai + 零度解说), summarizes only their **new uploads** (last ~3 days,
   recency-filtered — not the backlog) → transcript (yt-dlp captions → **Groq Whisper**
   fallback for caption-less videos) → 5-point English summary + thumbnail embed →
   `#curated-resources` (+ optional Telegram) + a Threads/Facebook caption bundle for
   manual posting. Daily ~09:03 MYT. **On-demand portal** (`on_demand.py`, VM port 8080,
   `?token=`-authed) — a phone-friendly dark/blurple web UI with **`/run`** (summarize any
   YouTube URL → `#curated-resources`) and **`/share`** (share any URL — incl. IG Reels /
   XHS / TikTok / Threads via yt-dlp + Groq Whisper — as a topic-routed news card). Bookmark
   `http://<VM_IP>:8080/?token=<T>` on a phone.
2. **Topic-routed news digest** — Reddit (per-topic subs; **OAuth JSON when
   `REDDIT_CLIENT_ID/SECRET` are set, else public multireddit RSS** — reddit.com 403s
   unauthenticated `.json` since ~2026-07, RSS cards show `hot #N` rank instead of
   upvotes) + Hacker News + **GitHub Trending** + HuggingFace trending + official blog
   RSS → an LLM judge tags each item with a topic + heat (viral/popular, **not brand** —
   startup/community/US+Chinese all count) → routes to the topic's channel. Already-posted
   stories are filtered out **before** the judge (fresh pool every run); a real run that
   posts 0 cards warns `#staff-chat`. **All six topics are live** (`live=True`): coding →
   `#ai-dev-tools`; creative (image/video/voice) → `#image-creation` /
   `#video-creation-aigc-tvc` / `#voice-studio`; research (study/productivity) →
   `#study-with-ai` / `#research-with-ai` (via `DISCORD_EDUCATION_WEBHOOK_URL`). Every ~3h.

Summarizer moved GitHub Actions → **GCP VM** (2026-07-21) — YouTube bot-blocks Azure
datacenter IPs but not GCP; the VM also hosts the always-on bot.

### B. Community bot — `bersama-bot/` (the "MEE6 clone")
A discord.py event bot covering what the MCP admin console can't (it only acts on
demand; the bot reacts to live events):
- Welcome message + auto-role on join
- Self-assign reaction roles (5 emoji → role)
- Leveling / XP / `/rank` / `/leaderboard`, with automatic role rewards at levels 5/10/20/35
- Slash `/help` · prefix commands `!rules`, `!resources`, `!ai`
- `@BersamaAi` mention / `!ai` → GLM-5.2 chat, **context-aware** (reads recent channel
  messages + fetches up to 2 linked pages via Jina Reader, SSRF-guarded), with cost
  guardrails (30s per-user cooldown, 20 calls/min server-wide cap, 3 concurrent max,
  1500-char input cap, 800-token reply cap). AI is optional — off when `ZAI_API_KEY` is unset.
- Seeds 👍🔥👎 on news cards every 15 min (the engagement-loop bridge); a 5-min heartbeat
  self-restarts (`os._exit(1)`) on a stale Gateway.

**Deployed 24/7 on a GCP VM** under systemd (service name `bersama`, auto-restart on
crash/reboot). Repo: `github.com/pmgwee/BersamaAi-community` (pushed; 17 commits on
`main` as of this writing).

**Known issue to watch:** the assistant has answered off-topic questions (e.g. Johor/
Melaka travel recommendations) and given a stale/confused answer about Claude's current
capabilities. Worth reviewing `ai_system_prompt` in `bersama-bot/config.json` if this
recurs — the community's whole premise is AI literacy, so the bot's own answers being
current matters more here than in a generic server.

### C. Admin console — `discord-mcp/` (SaseQ jar)
Interactive admin access for the owner via Claude Code / claude.ai connector.
Runs on-demand, locally, at `localhost:8085`. Docker Desktop is broken on this machine,
so it runs as a native Java 19 JAR (`run.cmd`) instead.

---

## 4. Live server structure

**Guild ID:** `1528524602861420625` · **8 members** (early/pre-public-launch stage)

**11 categories** (incl. a 📊 SERVER STATS counter category) (grew from the original 6/13 lean launch structure as
features were added — see `FEATURES.md` updates log for what changed and when):

- **📌 START HERE** — welcome, announcements, introductions, rules, faq, get-roles
- **💬 AI DAILY** — ai-general, ask-anything
- **📚 RESOURCES** — curated-resources (read-only, seeded with 6 summarized talks:
  Claude Code Live, Prompting 101, Google Flow/Veo 3, Sam Altman TED2025, ChatGPT Study
  Mode), free-credits-deals, tools-directory (read-only, ~35 tools), help
- **💻 CODING** — ai-dev-tools
- **🛠️ CREATIVE** — image-creation, video-creation-aigc-tvc, voice-studio
- **🔍 RESEARCH & PRODUCTIVITY** — research-with-ai, study-with-ai
- **🚀 SHOWCASE** — showcase-your-project (seeded with the "2026 First Annual: What cool
  thing did you build with Claude Code?" campaign), beta-testers
- **🔊 VOICE** — Hangout, Lounge, Study Room, AI Workshop
- **🤖 AI ASSISTANT** — ai-chat (bot answers here), bot-commands, level-ups
- **🛡️ STAFF** — mod-log, staff-chat
- **📊 SERVER STATS** — member/role counter channels (auto-updating)

**18 roles**, from bottom to top: `@everyone` → Newcomer → Deals Hunter, Developer,
One-Person Company, Student, Content Creator (self-assigned via reaction roles) →
Muted → AI Guide → Moderator → Member → CEO (top, likely the owner's own role) →
`mcp-connectors` (the bot's own role, Administrator permission).

**Level rewards:** Bronze (Lv5) → Silver (Lv10) → Gold (Lv20) → Platinum (Lv35).

**Language:** English-only (bilingual Chinese/English approach was tried and explicitly
retired 2026-07-20 — don't reintroduce Chinese channel names/content without the owner
asking again).

---

## 5. Key decisions log (don't re-litigate without a specific reason)

| Decision | Why |
|---|---|
| Discord is the clubhouse, not the growth engine | Malaysian mass audience lives on WhatsApp/Telegram/FB, not Discord (market research) |
| English-only server | Simpler moderation, broadest MY reach; bilingual was tried and retired |
| GLM (`glm-5.2` via Z.ai), not Anthropic, powers the bot/pipeline | Cost — ~US$3/mo vs. Anthropic API pricing |
| Bot + MCP jar share one Discord token | Discord allows concurrent Gateway sessions per token; simpler ops |
| Content pipeline posts via webhook, not the MCP connector | MCP is a local stdio process, unreachable from cloud cron |
| Pipeline moved from GitHub Actions → GCP VM | Consolidate with the bot's always-on host (2026-07-21) |
| On-demand portal + `/share` on the VM (not GitHub Actions) | Always-reachable phone trigger; `/share` uses yt-dlp + Groq ASR that suit a persistent host — and it keeps the summarizer's GCP-IP advantage over Azure/Actions |
| No music bot, no heavy economy/game plugins | Discontinued ecosystem-wide / out of scope |
| "Learn *with* AI" framing (not "AI for exams") | Avoids reading as cheating-enablement — reputational risk flagged in research |
| Free-first, no upsell ladder, no "guru" aesthetics | Direct differentiation against the OE杰青商学院 scam-backlash pattern |
| Docker Desktop unusable on this machine | Broken daemon; discord-mcp runs as a native Java JAR instead |

---

## 6. Where to look for more

- **Full market research, competitor deep-dives, 90-day launch playbook, risk table:**
  [`MARKET-RESEARCH-REPORT.md`](MARKET-RESEARCH-REPORT.md)
- **Feature-by-feature status (live/pending/off), architecture diagram, config/secrets
  map, ops runbook:** [`FEATURES.md`](FEATURES.md)
- **Component setup instructions:** each of `bersama-ai-pipeline/README.md`,
  `bersama-bot/README.md`, `discord-mcp/` has its own setup guide
- **Most recent session-to-session handoff notes:** [`SESSION-HANDOFF.md`](SESSION-HANDOFF.md)
  (may be stale — check its date against this file's date; `FEATURES.md`'s updates log
  and `git log` are more current sources of truth for what's actually shipped)
- **GitHub repo:** `github.com/pmgwee/BersamaAi-community`
