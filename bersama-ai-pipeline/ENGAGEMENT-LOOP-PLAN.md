# Engagement Feedback Loop — Implementation Plan
*Reward-driven content selection for the BersamaAi news engine.*
*Written 2026-07-21 against `news.py` @ commit `b4f5f56`. Hand this file to the implementing agent as the work order.*

---

## 0. Goal & design philosophy

**Owner's goal:** make the content engine learn what the community actually finds
useful/high-quality — like Threads/TikTok/IG feed algorithms tailor to each user —
by using member reactions on Discord posts as the reward signal, feeding analytics
back into the engine to tune its selection over time.

**Design reality check (why bandit + preference scores, not deep RL):**
- Data volume: 5 channels × ~10–15 posts/day × a small server = tens of reaction
  events/week. Policy-gradient RL needs orders of magnitude more; it would overfit
  noise and thrash. The correct production architecture at this scale is:
  **telemetry → reward aggregation → preference scores → biased selection with an
  exploration floor** — an ε-greedy contextual bandit. Same loop as big recsys,
  sized to converge on sparse data.
- This is also *upgradeable*: every event is logged raw (append-only JSONL), so a
  learned ranker can be trained later from day-one data when volume justifies it.
- One honest caveat to embrace: with a small community, "the community's taste"
  ≈ the few active members' taste. That is FINE — the owner explicitly wants a
  Threads-like tailored feed for this community, and early members ARE the community.
  The exploration floor (§4.3) prevents full echo-chamber collapse.

**Non-goals:** per-user personalization (Discord channels are shared surfaces —
one feed per channel, not per member); real-time online learning (batch update
per run is plenty at 3h cadence); any UI outside Discord.

---

## 1. Current-state facts the design builds on (verified in code)

| Fact | Where | Consequence |
|---|---|---|
| News posts go out via **webhook** `requests.post(wh, json=payload)` | `news.py _post()` | Pipeline never learns message IDs → **must add `?wait=true`** to get them |
| `bersama-bot` already handles `on_raw_reaction_add/remove` | `bot.py:287` | Reaction listening infra exists; extend, don't invent |
| Bot + pipeline run on the **same GCP VM** but in **different clones** (`~/BersamaAi-community/` vs `~/bersama/`) | bot_command.md / post_command.md | Don't couple via filesystem paths across repos — use a **shared state dir** (§2.4) or channel sweeps |
| Pipeline state = JSON files in `state/`, committed back | `state.py` | Follow the same pattern for new state |
| Judge = GLM forced tool-call, single system prompt | `news.py judge()` | Preference injection = a generated prompt section (§4.2) |
| Per-topic quotas `PER_TOPIC_QUOTA=8`, per-channel cap `MAX_PER_TOPIC=3` | `news.py` | These constants become the bandit's actuator (§4.1) |

---

## 2. Phase 1 — Telemetry (the sensor). ~1 session

### 2.1 Capture message IDs at post time
In `news.py _post()`: append `?wait=true` to the webhook URL (Discord then returns
the created message object instead of 204). Record every posted card to
**`state/posted_log.jsonl`** (append-only, one JSON object per line):

```json
{"message_id": "…", "channel_id": "…", "channel": "#ai-dev-tools",
 "topic": "coding", "category": "LAUNCH", "source": "github",
 "source_url": "…", "headline": "…", "heat_score": 4100,
 "star_velocity": 150, "posted_at": "2026-07-21T16:47:22Z"}
```

Also do this in the curated-resources summarizer publish path (`publish.py`) with
`{"topic": "curated", …}` — the loop should cover both engines eventually; news
first.

### 2.2 Seed voting reactions (cold-start killer)
Members rarely react unprompted; pre-seeded buttons raise signal rate ~5–10×
(the TLDR-newsletter pattern). Webhooks can't react, but the **bot** can:

- New bot task (in `bersama-bot/bot.py`): every 15 min, scan the 5 news channels'
  last 20 messages; for any webhook-authored post without the seed reactions, add
  **👍 🔥 😐** ( useful / amazing / meh ). Cheap, idempotent, no cross-repo coupling
  (the bot doesn't need `posted_log` — "webhook-authored in a news channel" is the filter).

### 2.3 Harvest engagement (the sweep)
A new **`pipeline/engagement.py`** run (cron, hourly or piggybacked on each news
run) sweeps posts from `posted_log.jsonl` that are **24h–8d old** and, via the
**bot token** (read-only REST, no Gateway needed — plain `requests` to
`GET /channels/{cid}/messages/{mid}` and `GET …/reactions/{emoji}`), records into
**`state/engagement.jsonl`**:

```json
{"message_id": "…", "sweep_at": "…", "age_h": 26,
 "reactions": {"👍": 4, "🔥": 2, "😐": 1, "other": 3},
 "reply_count": 2}
```

- `reply_count`: messages in the channel within 48h whose `message_reference` or
  content links/replies to the post (fetch channel history once per sweep, match
  locally — 1 request per channel, not per post).
- Subtract the bot's own seed reactions (−1 per seeded emoji).
- Sweep each post at ~T+24h and ~T+72h (two data points; second captures slow burns).
- **Env:** the pipeline `.env` gains `DISCORD_BOT_TOKEN` (same token the bot uses).
  Note this in FEATURES.md's config table — third place this token now lives.

---

## 3. Phase 2 — Reward aggregation (the analytics). ~1 session

**`pipeline/preferences.py`** — pure computation, no network. Run at the start of
each news run (before gathering).

### 3.1 Per-post reward
```
raw    = 2·👍 + 3·🔥 − 2·😐 + 1·other_reactions + 4·replies
reward = raw / active_member_baseline        # normalize so growth doesn't inflate scores
```
`active_member_baseline` = rolling 7-day count of distinct members who messaged
anywhere (bot can drop this into `state/activity_baseline.json` daily; default 10
if absent). Clamp reward to [−3, +5]. Weights are constants at the top of the file —
tuning them is expected; log them into every analytics snapshot (§5).

### 3.2 Preference scores (the "model")
Exponentially-decayed mean per **arm** along three dimensions, stored in
**`state/preferences.json`**:

```json
{"updated_at": "…", "half_life_days": 14,
 "topic":    {"coding": 1.42, "creative_video": 0.31, "research_study": -0.12, …},
 "source":   {"github": 1.1, "r/LocalLLaMA": 0.9, "official": 0.4, "HN": 0.2, …},
 "category": {"LAUNCH": 1.3, "OPEN_SOURCE": 1.1, "PRICING": 0.5, "UPDATE": -0.2, …},
 "n_events": 214,
 "exemplars": {"top": [{"headline": "…", "reward": 4.2}, …×5],
               "bottom": [{"headline": "…", "reward": -1.8}, …×3]}}
```

EMA with a 14-day half-life = recent taste dominates, old taste fades — the
"fine-tune over time" the owner asked for, without ever retraining anything.
`exemplars` = concrete recent wins/losses for the judge prompt (§4.2).

---

## 4. Phase 3 — Close the loop (the actuator). ~1 session

Three injection points, weakest-coupling first:

### 4.1 Dynamic quotas (bandit allocation)
In `gather_candidates()`: replace fixed `PER_TOPIC_QUOTA = 8` with
```
quota(topic) = clamp(4 + round(2 · pref.topic[topic]), 4, 14)   # floor 4 = exploration
```
Same idea for the shared-source quotas (HN/HF/RSS ± 2 by source score). The
**floor is mandatory** — every arm keeps getting pulled, so a topic can recover
when taste shifts (ε-greedy exploration). Same transform for `MAX_PER_TOPIC`
(cap 2–4 by channel engagement).

### 4.2 Judge prompt injection (the taste memo)
Generate a section appended to `SYSTEM_PROMPT` each run from `preferences.json`:

```
COMMUNITY PREFERENCE PROFILE (learned from member reactions; updated {date}):
- This community engages MOST with: LAUNCH & OPEN_SOURCE items in coding (esp.
  GitHub repos with high velocity); r/LocalLLaMA finds.
- Engages LEAST with: generic UPDATE items; research_study listicles.
- Recent hits: "{top exemplar 1}" · "{top exemplar 2}"
- Recent misses: "{bottom exemplar 1}"
When choosing between two comparably-hot items, prefer the profile. NEVER use the
profile to post a lukewarm item over a genuinely viral one — heat still wins.
```

That last sentence is load-bearing: it keeps the trending mission primary
(commit `b4f5f56` deliberately made the judge pure-trending; the profile is a
tiebreaker, not a new bias).

### 4.3 Guardrails (prevent the classic failure modes)
- **Exploration floor** (§4.1) — never below 4 candidates/topic, cap ≥ 1 post/channel.
- **Minimum evidence**: dimensions with `n_events < 20` contribute NOTHING (profile
  section omits them; quotas stay default). No learning from 3 clicks.
- **Kill switch**: env `PREFS_ENABLED=false` reverts the whole loop to today's
  static behavior. One env var, no code rollback.
- **Drift visibility**: each run logs `quota deltas vs default` + `profile summary`
  into the run output — changes are always observable, never silent.

---

## 5. Phase 4 — Analytics surface (see what it's learning). ~½ session

Weekly digest (cron, Sunday night) posted by the pipeline to **`#staff-chat`**:
top/bottom 5 posts by reward, per-channel engagement rate, current preference
scores vs last week, current quota allocation, `n_events`. ~30 lines of formatting
over data already in `state/`. This is the owner's window into "what has the
algorithm learned" — and the early-warning if weights need tuning.

---

## 6. Build order & effort

| Step | What | Files | Effort |
|---|---|---|---|
| 1 | `?wait=true` + `posted_log.jsonl` | `news.py`, `publish.py` | S |
| 2 | Bot seed-reactions task | `bersama-bot/bot.py` | S |
| 3 | `engagement.py` sweep (REST, bot token) | new file + cron | M |
| 4 | `preferences.py` (reward + EMA + exemplars) | new file | M |
| 5 | Quota + prompt injection + guardrails | `news.py` | M |
| 6 | Weekly analytics digest | new small module + cron | S |
| 7 | FEATURES.md rows (A4 engagement loop) + config table update | docs | S |

Steps 1–3 can ship alone and just *collect* for 2 weeks while the community grows —
recommended: **ship telemetry now, flip the actuator on when `n_events ≥ 100`.**

## 7. Acceptance tests

1. Post a news run → every card appears in `posted_log.jsonl` with a real message_id;
   seed reactions appear within 15 min.
2. React 🔥 on two coding posts, 😐 on a research post → after sweeps + a preference
   run, `preferences.json` shows coding > research; next run's log shows coding quota > default.
3. With `n_events < 20`: profile section absent, quotas = defaults (verify in run log).
4. `PREFS_ENABLED=false` → behavior byte-identical to today's engine (diff the run logs).
5. Weekly digest posts to #staff-chat with non-empty tables.
6. Echo-chamber check (manual, monthly): a deliberately off-profile viral item
   (e.g. huge creative_video launch during a coding-heavy month) still gets posted.

## 8. Explicitly out of scope (v2+ if data justifies)

- Learned ranking model / embeddings similarity (needs ~10k+ events; the JSONL
  logs are the future training set — nothing is thrown away).
- Per-member personalization, DM digests, cross-channel A/B tests.
- Click tracking (Discord gives no link-click telemetry; reactions/replies are the
  best available proxy — accepted limitation).
