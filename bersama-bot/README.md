# BersamaAi Event Bot

The **event-driven half** of BersamaAi's MEE6 clone. It runs *alongside* the
SaseQ discord-mcp jar on the **same bot token** (Discord allows concurrent
Gateway sessions per token) and powers the five features the MCP cannot do,
because the MCP only acts on demand and never "sees" events happen:

| Feature | Where it lives |
|---|---|
| Welcome message + auto-role on join | **this bot** (`on_member_join`) |
| Self-assign reaction roles (5 emoji: 🎓 / 🎨 / 💼 / 💻 / 📈) | **this bot** (`on_raw_reaction_add/remove`) |
| Leveling / XP / `/rank` / `/leaderboard` (rewards Lv 5/10/20/35) | **this bot** (`on_message` + SQLite) |
| Prefix commands (`!rules`, `!resources`, `!ai`) + slash `/help` | **this bot** |
| @BersamaAi / `!ai` → GLM AI (context-aware: recent msgs + Jina link fetch) | **this bot** (Z.ai, OpenAI-compatible) |
| Seed 👍🔥👎 on news cards every 15 min (engagement-loop bridge) | **this bot** (`seed_reactions` task) |
| Auto-moderation (spam/links/profanity) | **Discord's native AutoMod** — not yet enabled (see Step 6) |
| Timers / scheduled messages | SaseQ MCP (cron → `http://localhost:8085/mcp`) |
| On-demand admin + Claude for the owner | SaseQ MCP via claude.ai connector |

---

## Step 1 — Enable the two Privileged Intents (REQUIRED, do this first)

The bot cannot see member joins or message text without these.

1. Go to <https://discord.com/developers/applications> → your **BersamaAi** app.
2. Left menu → **Bot**.
3. Scroll to **Privileged Gateway Intents** and turn **ON**:
   - ✅ **PRESENCE INTENT** *(not required, leave off is fine)*
   - ✅ **SERVER MEMBERS INTENT** ← required (welcome + auto-role)
   - ✅ **MESSAGE CONTENT INTENT** ← required (prefix commands + AI mentions)
4. **Save Changes**.

> The MCP jar doesn't need these, so it's fine that they were off until now.

## Step 2 — Install Python + dependencies

You need **Python 3.10 or newer** (<https://python.org> — tick "Add Python to PATH" on Windows).

```bash
cd bersama-bot
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

## Step 3 — Configure

```bash
# Windows (PowerShell):
copy .env.example .env
# macOS/Linux:
cp .env.example .env
```

Open `.env` and set:
- `DISCORD_TOKEN` = the **same** bot token the MCP jar uses (from your main `.env`).
- `ZAI_API_KEY` = your **Z.ai** API key. Enables member-facing AI (GLM-5.2). Leave blank to disable; everything else still works.
- `ZAI_BASE_URL` = `https://api.z.ai/api/coding/paas/v4` (default — Z.ai's OpenAI-compatible endpoint).
- `GLM_MODEL` = `glm-5.2` (default).
- `JINA_API_KEY` = *(optional)* a [Jina Reader](https://jina.ai/reader) key. When a member shares a link and @mentions the bot, it fetches the page content (JS-rendered pages too) so it can answer questions about it. Works without a key (rate-limited); leave blank to skip link fetch.

> Channels, roles, reaction-role menus, level rewards, and the AI system prompt live in
> [`config.json`](config.json) — **not** `.env`. `setup.sh` writes the four core env vars
> above for you (hardcoding `glm-5.2` + the Z.ai URL); `JINA_API_KEY` you add by hand.

## Step 4 — Test it locally

```bash
python -u bot.py
```

`-u` unbuffers stdout. The bot also forces UTF-8 on its own stdout/stderr (so emoji role
names never crash a Windows cp1252 console) and writes timestamped log lines.

You should see:
```
[2026-07-21 01:47:53] [INFO    ] discord.client: logging in using static token
[2026-07-21 01:47:57] [INFO    ] bersama: Logged in as BersamaAi#2383 (1528479915635245258)
[2026-07-21 01:47:57] [INFO    ] bersama: Reaction-role menus: ['1528687776801886249']
[2026-07-21 01:47:57] [INFO    ] bersama: AI (GLM via Z.ai): ON (glm-5.2)
[2026-07-21 01:47:58] [INFO    ] bersama: Synced 3 slash commands.
```

Try it in the server: react in **#get-roles**, type `/rank`, post `!rules`, or `@BersamaAi hello`.
Press **Ctrl+C** to stop.

## Step 5 — Run it 24/7

> **Current deployment:** the bot is already live on a **GCP VM** under systemd service
> `bersama` (`Restart=always`, survives crashes/reboots). The options below are alternatives
> (Windows PC / Pi / other clouds) — `setup.sh` sets up the same systemd service on any Linux box.

The bot **must stay running** or those features stop. Pick ONE option.

### Option A — Always-on Windows PC (simplest, free)
Use **NSSM** to run it as a Windows service that restarts on crash/reboot:
```powershell
# Install NSSM: https://nssm.cc/download
nssm install BersamaAiBot "C:\path\to\.venv\Scripts\python.exe" "-u C:\path\to\bersama-bot\bot.py"
nssm set BersamaAiBot AppDirectory "C:\path\to\bersama-bot"
nssm set BersamaAiBot AppEnvironmentExtra "DISCORD_TOKEN=..." "ZAI_API_KEY=..."
# Capture timestamped logs to a rotating file:
nssm set BersamaAiBot AppStdout "C:\path\to\bersama-bot\bersama.log"
nssm set BersamaAiBot AppStderr "C:\path\to\bersama-bot\bersama.log"
nssm set BersamaAiBot AppRotateFiles 1
nssm set BersamaAiBot AppRotateOnline 1
nssm set BersamaAiBot AppRotateBytes 10485760
nssm start BersamaAiBot
```
Downside: the PC must never sleep / shut down. The bot also force-restarts itself
(`os._exit(1)`) if the Discord Gateway is unreachable for 10+ minutes, and NSSM's
default restart-on-failure relaunches it with a clean connection.

### Option B — Raspberry Pi (one-time ~RM 200–400, ~RM 1–2/month power)
A Pi 3B or 4 plugged in at home, running 24/7. Same `setup.sh` as the cloud options below:
```bash
sudo apt-get update && sudo apt-get install -y git python3 python3-venv
git clone https://github.com/pmgwee/BersamaAi-community.git
cd BersamaAi-community/bersama-bot
bash setup.sh          # venv + deps + .env + systemd, all in one
```

### Option C — GCP or AWS (you have free credits; no home machine needed)
A tiny always-on Linux VM is plenty — the bot idles under ~150 MB RAM, so 1 vCPU / 1 GB
is more than enough. The bot is **outbound-only**, so you do NOT open any inbound port on
either cloud (both allow egress by default — no firewall/security-group changes).

**GCP** (recommended — simplest UX; `e2-micro` stays always-free-eligible after your credits run out):
1. Console → **Compute Engine → VM Instances → Create**.
2. Machine: **e2-micro** · Boot disk: **Ubuntu 22.04 or 24.04** · Region: `us-central1` / `us-east1` / `us-west1` (always-free-eligible).
3. No firewall change needed.

**AWS** (identical result):
1. Console → **EC2 → Launch instance**.
2. AMI: **Ubuntu Server 22.04/24.04** · Instance type: **t3.micro** (Free Tier / credits).
3. The default security group allows outbound — no inbound rule needed.

Then on the VM (same for both):
```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/pmgwee/BersamaAi-community.git
cd BersamaAi-community/bersama-bot
bash setup.sh          # installs python+deps, writes .env, enables systemd
```
`setup.sh` installs + starts a systemd service (`bersama`) with `Restart=always`, so the
bot survives crashes and reboots. Verify: `sudo systemctl status bersama` · logs: `tail -f bersama.log`.
   - *Other cheap clouds: any ~RM14–36/month VPS (Shinjiru, LightNode, DigitalOcean) works identically — just run `bash setup.sh`.*

## Step 6 — Turn on Discord's free native AutoMod (auto-moderation)

> **Status: not yet enabled.** The bot has no auto-mod code by design — it's delegated to
> Discord's native AutoMod. Turn it on when ready; until then, auto-mod is off.

This replaces MEE6's auto-mod at **no cost**, with no bot needed:

1. In Discord → **Server Settings → AutoMod**.
2. Create a rule, e.g. **Block spam/links/profanity**:
   - **Spam protection:** set the message-rate threshold.
   - **Block words:** add profanity/slur lists.
   - **Block unwanted links:** enable.
3. Set the action: **Alert moderators** (posts to **#mod-log**) and/or **Timeout the user** / **Delete message**.

For anything native AutoMod can't express, ask Claude (via the MCP) to handle it on demand.

---

## Cost & safety notes

- **AI cost:** the bot uses **GLM-5.2 via Z.ai** (set `ZAI_API_KEY`). The Z.ai GLM Coding Plan is ~US$3/month — far cheaper than Anthropic. The bot also bounds usage four ways — a **30 s per-user cooldown** (`AI_COOLDOWN`), a **server-wide cap of 20 calls/min** (`AI_GLOBAL_MAX`), at most **3 concurrent calls** (`AI_CONCURRENCY`), **input truncated to 1 500 chars** (`AI_INPUT_MAX`), plus an 800-token reply cap. Tune any of these constants at the top of `bot.py`. To disable AI entirely, clear `ZAI_API_KEY`.
- **Token safety:** `DISCORD_TOKEN` is a full-admin credential. Never commit `.env` to git, never paste it in public. This folder's `.gitignore` already excludes it.
- **Shared token:** because the bot and the MCP share one token, if you ever **reset/regenerate** the token in the Developer Portal, update it in **both** the MCP `.env` and this bot's `.env`.

## Hardening (pre-launch security / ops pass)

Before going 24/7 the bot was put through an adversarial code review; these are now
baked in (see `bot.py`):

- **Single-guild allowlist.** `on_message`, a global `@bot.check`, and an `on_guild_join`
  auto-leave ensure the bot only ever serves *this* guild. Without it, anyone who copies
  the bot's client ID could add it to a private server and drain the Z.ai budget / farm the
  leaderboard. (Still: set the bot to **Private** in the Developer Portal for defense-in-depth.)
- **No pings from AI replies.** `allowed_mentions=AllowedMentions.none()` + a mention-stripping
  regex mean prompt-injected GLM output can never mass-ping roles or `@everyone`.
- **SSRF guard on link fetch.** When the AI enriches its context by pulling a page a member
  shared (Jina Reader, up to 2 most-recent links), `_is_safe_fetch_url` blocks non-http(s),
  localhost, private/loopback/link-local IPs, and cloud-metadata hosts — a malicious link
  can't prod the VM's internals through the bot.
- **Z.ai call timeout (30 s).** A hung provider request can no longer pin one of the 3 AI
  slots for up to 30 minutes; it times out, refunds its global-quota slot, and replies gracefully.
- **Fair cooldowns.** The per-user 30 s lockout and the global 20/min slot are only consumed
  by *real* attempts — empty pings, "AI disabled", and "busy" replies no longer penalise the user.
- **Loud permission errors.** `Forbidden` (bot role too low) is logged as a `PERMISSIONS:`
  banner in the auto-role / level-up / reaction paths, not buried as a generic HTTP error.
- **Timestamped, UTF-8, crash-safe logging.** All diagnostics go through `logging` (per-line
  flush, timestamps); stdout is forced to UTF-8 so emoji role/user names never crash the
  Windows console.
- **Config audit on boot.** Every channel/role/reaction/level ID is checked against the live
  guild at startup — stale config warns loudly instead of failing silently.
- **Gateway heartbeat.** If the Gateway is unreachable for two consecutive 5-min checks, the
  process exits so the service manager restarts it.
- **Inputs validated at boot.** Missing `DISCORD_TOKEN` or `ai_system_prompt` fail fast with a
  clear message instead of a mid-run `KeyError`.

> Not applied (deliberate): a 10-minute membership-age gate before the AI can be used
> (anti-alt). It's a one-line add in `handle_ai` if abuse appears; skipped for now so first-time
> members can try the AI immediately, and because the global cap already bounds the cost.

## Database backups (ops)

Leveling XP lives in `bersama.db` (SQLite, WAL mode). [`backup_db.py`](backup_db.py) takes a
**live, WAL-safe** snapshot via SQLite's online-backup API — it can run while the bot is
writing without corruption or locking.

- **What it does:** copies `bersama.db` → `./backups/bersama-YYYYMMDD-HHMMSS.db`, keeping
  the newest `KEEP = 14` snapshots (older ones pruned).
- **Schedule it yourself** — `setup.sh` does **not** install this cron (it only sets up the
  systemd service), so it's easy to forget. Add to the VM crontab (`crontab -e`):
  ```cron
  0 4 * * * cd $HOME/BersamaAi-community/bersama-bot && .venv/bin/python backup_db.py >> backups.log 2>&1
  ```
- **Restore:** stop the bot, copy a snapshot back, restart:
  ```bash
  sudo systemctl stop bersama && cp backups/bersama-20260721-040000.db bersama.db && sudo systemctl start bersama
  ```

## Troubleshooting

| Symptom | Fix |
|---|---|
| Bot online but reacts/levels don't work | You forgot **Message Content** / **Server Members** intent in Step 1. |
| `[BersamaAi] AI (GLM via Z.ai): OFF` | `openai` not installed, or `ZAI_API_KEY` empty. Re-run `pip install -r requirements.txt`. |
| `Missing permissions to manage role` | The bot's role (**BersamaAi**) must sit **above** the roles it assigns. Drag it up in **Server Settings → Roles**. (Its Administrator permission usually covers this already.) |
| Reaction roles work on add but not remove | Normal — the `on_raw_reaction_remove` event needs the Members intent, which Step 1 enables. |
| Slash commands don't appear | Slash commands sync to one guild on startup. Restart the bot; can take up to a minute to show in the client. |

## What's intentionally NOT here

- **Music** — discontinued across the whole Discord ecosystem (YouTube killed it in Feb 2023; even MEE6 dropped music). Don't expect it.
- **Heavy game/economy plugins** — out of scope; add later with a separate bot if wanted.
- **Custom dashboards** — MEE6's web panel has no equivalent; you configure via `config.json` here.
