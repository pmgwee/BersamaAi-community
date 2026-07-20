# BersamaAi Event Bot

The **event-driven half** of BersamaAi's MEE6 clone. It runs *alongside* the
SaseQ discord-mcp jar on the **same bot token** (Discord allows concurrent
Gateway sessions per token) and powers the five features the MCP cannot do,
because the MCP only acts on demand and never "sees" events happen:

| Feature | Where it lives |
|---|---|
| Welcome message + auto-role on join | **this bot** (`on_member_join`) |
| Self-assign reaction roles | **this bot** (`on_raw_reaction_add/remove`) |
| Leveling / XP / `/rank` / `/leaderboard` | **this bot** (`on_message` + SQLite) |
| Prefix commands (`!rules`, `!resources`, `!ai`) | **this bot** |
| @BersamaAi / `!ai` → GLM AI | **this bot** (Z.ai, OpenAI-compatible) |
| Auto-moderation (spam/links/profanity) | **Discord's free native AutoMod** (see Step 6) |
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

## Step 4 — Test it locally

```bash
python bot.py
```

You should see:
```
[BersamaAi] Logged in as BersamaAi#....
[BersamaAi] Reaction-role menus: ['1528687776801886249']
[BersamaAi] AI (GLM via Z.ai): ON   (or OFF)
```

Try it in the server: react in **#get-roles**, type `/rank`, post `!rules`, or `@BersamaAi hello`.
Press **Ctrl+C** to stop.

## Step 5 — Run it 24/7

The bot **must stay running** or those five features stop. Pick ONE option.

### Option A — Always-on Windows PC (simplest, free)
Use **NSSM** to run it as a Windows service that restarts on crash/reboot:
```powershell
# Install NSSM: https://nssm.cc/download
nssm install BersamaAiBot "C:\path\to\.venv\Scripts\python.exe" "C:\path\to\bersama-bot\bot.py"
nssm set BersamaAiBot AppDirectory "C:\path\to\bersama-bot"
nssm set BersamaAiBot AppEnvironmentExtra "DISCORD_TOKEN=..." "ZAI_API_KEY=..."
nssm start BersamaAiBot
```
Downside: the PC must never sleep / shut down.

### Option B — Raspberry Pi (one-time ~RM 200–400, ~RM 1–2/month power)
A Pi 3B or 4 plugged in at home, running 24/7:
```bash
sudo apt install python3 python3-venv -y
git clone <your-repo> bersama-bot && cd bersama-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env   # fill DISCORD_TOKEN + ZAI_API_KEY
```
Auto-start via systemd — create `/etc/systemd/system/bersama.service`:
```ini
[Unit]
Description=BersamaAi Bot
After=network.target

[Service]
WorkingDirectory=/home/pi/bersama-bot
ExecStart=/home/pi/bersama-bot/.venv/bin/python bot.py
Restart=always
User=pi
EnvironmentFile=/home/pi/bersama-bot/.env

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable --now bersama
sudo systemctl status bersama   # check it's running
```

### Option C — Oracle Cloud Always Free (cloud, free forever, no home machine)
1. Sign up at <https://www.oracle.com/cloud/free> → create an **Always Free AMD VM** (Ubuntu).
2. Open the OCI Console → Instance → **Edit security list / add Ingress** isn't even needed (bot is outbound-only).
3. SSH in and follow the **Option B** commands (it's Ubuntu, same as a Pi).
4. Use the same systemd unit. The VM runs 24/7 free.
   - *Other cheap clouds: any ~RM14–36/month VPS (Shinjiru, LightNode, DigitalOcean) works identically.*

## Step 6 — Turn on Discord's free native AutoMod (auto-moderation)

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
