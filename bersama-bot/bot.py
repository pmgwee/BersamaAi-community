#!/usr/bin/env python3
"""
BersamaAi event bot — the event-driven half of the MEE6 clone.

Runs ALONGSIDE the SaseQ discord-mcp jar on the SAME bot token
(Discord officially allows concurrent Gateway sessions per token).

Handles the 5 event-driven features the imperative MCP cannot:
  - Welcome message + auto-role on member join
  - Self-assign reaction roles
  - Leveling (XP per message + rank roles + /rank, /leaderboard)
  - Prefix commands (!rules, !resources, !ai)
  - @mention / !ai -> GLM AI via Z.ai (async, rate-limited, cost-bounded)

Auto-moderation is left to Discord's free native AutoMod (see README.md).

Privileged intents REQUIRED in the Developer Portal
(Bot > Privileged Gateway Intents): Server Members + Message Content.
"""
import asyncio
import json
import os
import random
import sqlite3
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# OpenAI SDK is optional — the bot runs without it (AI just disabled).
# Z.ai exposes an OpenAI-compatible endpoint, so we drive it with this SDK.
try:
    from openai import AsyncOpenAI  # type: ignore
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")          # auto-load .env when present (local runs)
CONFIG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))

TOKEN = os.environ["DISCORD_TOKEN"]            # same token as the MCP jar
ZAI_API_KEY = os.environ.get("ZAI_API_KEY", "")
ZAI_BASE_URL = os.environ.get("ZAI_BASE_URL", "https://api.z.ai/api/coding/paas/v4")
GLM_MODEL = os.environ.get("GLM_MODEL", "glm-5.2")

GUILD_ID = int(CONFIG["guild_id"])
CHANNELS = {k: int(v) for k, v in CONFIG["channels"].items()}
ROLES = {k: int(v) for k, v in CONFIG["roles"].items()}
# Normalize emoji keys once (strip variation selector U+FE0F) so client-side
# differences never break reaction-role matching.
REACTION_ROLES = {
    msg_id: {emo.rstrip("️"): role_id for emo, role_id in mapping.items()}
    for msg_id, mapping in CONFIG["reaction_roles"].items()
}
LEVEL_REWARDS = {int(k): int(v) for k, v in CONFIG["level_rewards"].items()}

XP_MIN, XP_MAX = 15, 25
XP_COOLDOWN = 60          # seconds between XP-earning messages per user
AI_COOLDOWN = 30          # seconds between AI calls per user
AI_MAX_CHARS = 1900       # Discord message-length safety for chunked replies
AI_INPUT_MAX = 1500       # truncate user input to bound cost
AI_GLOBAL_MAX = 20        # max AI calls per rolling window (server-wide)
AI_GLOBAL_WINDOW = 60     # ...seconds
AI_CONCURRENCY = 3        # max simultaneous in-flight AI calls

DB_PATH = BASE / "bersama.db"


# --------------------------------------------------------------------------- #
# SQLite — proper open/close context manager (no handle leaks), WAL mode.
# --------------------------------------------------------------------------- #
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS xp (
                user_id   INTEGER PRIMARY KEY,
                xp        INTEGER DEFAULT 0,
                last_msg  REAL    DEFAULT 0
            )"""
        )


def validate_config():
    required_channels = [
        "welcome", "introductions", "rules", "get_roles", "ai_chat",
        "level_ups", "ask_anything", "curated_resources", "tools_directory",
    ]
    missing = [k for k in required_channels if k not in CHANNELS]
    if missing:
        raise SystemExit(f"[BersamaAi] config.json missing channels: {missing}")
    if "newcomer" not in ROLES:
        raise SystemExit("[BersamaAi] config.json missing 'newcomer' role")


# --------------------------------------------------------------------------- #
# Leveling math (MEE6 curve) — level_from_xp and xp_for_level are consistent:
# level_from_xp(xp_for_level(N)) == N for all N >= 0.
# --------------------------------------------------------------------------- #
def level_from_xp(total_xp: int) -> int:
    level = 0
    while total_xp >= 5 * (level ** 2) + 50 * level + 100:
        total_xp -= 5 * (level ** 2) + 50 * level + 100
        level += 1
    return level


def xp_for_level(level: int) -> int:
    return sum(5 * (i ** 2) + 50 * i + 100 for i in range(level))


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #
intents = discord.Intents.default()
intents.members = True            # privileged — enable in Dev Portal
intents.message_content = True    # privileged — enable in Dev Portal
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot.remove_command("help")        # we provide /help

_ai_last_use: dict[int, float] = {}
_ai_calls: deque[float] = deque()              # global rolling-window gate
_ai_sem: asyncio.Semaphore | None = None       # created in main() once a loop exists
_synced = False

# Single shared async OpenAI-compatible client pointed at Z.ai (None when AI disabled).
ai_client = (
    AsyncOpenAI(api_key=ZAI_API_KEY, base_url=ZAI_BASE_URL)
    if (HAS_OPENAI and ZAI_API_KEY)
    else None
)


@bot.event
async def on_ready():
    global _synced
    print(f"[BersamaAi] Logged in as {bot.user} ({getattr(bot.user, 'id', '?')})")
    print(f"[BersamaAi] Reaction-role menus: {list(REACTION_ROLES)}")
    print(f"[BersamaAi] AI (GLM via Z.ai): {'ON (' + GLM_MODEL + ')' if ai_client else 'OFF'}")
    if _synced:
        return
    try:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        _synced = True
        print(f"[BersamaAi] Synced {len(synced)} slash commands.")
    except discord.HTTPException as exc:
        print(f"[BersamaAi] FAILED to sync slash commands: {exc}")


# ---- Welcome ------------------------------------------------------------- #
@bot.event
async def on_member_join(member: discord.Member):
    try:
        role = member.guild.get_role(ROLES["newcomer"])
        if role:
            await member.add_roles(role, reason="Auto-role on join")
    except discord.HTTPException as exc:
        print(f"[BersamaAi] on_member_join role error: {exc}")
    ch = member.guild.get_channel(CHANNELS["welcome"])
    if ch:
        try:
            await ch.send(
                f"👋 Welcome {member.mention} to **BersamaAi**!\n\n"
                f"**Get started:**\n"
                f"1️⃣ Read the rules in <#{CHANNELS['rules']}>\n"
                f"2️⃣ Pick your roles in <#{CHANNELS['get_roles']}>\n"
                f"3️⃣ Say hi in <#{CHANNELS['introductions']}>\n\n"
                f"💬 You can also ask our AI anything in <#{CHANNELS['ai_chat']}> "
                f"— just type `@BersamaAi` + your question."
            )
        except discord.HTTPException as exc:
            print(f"[BersamaAi] welcome send error: {exc}")


# ---- Reaction roles ------------------------------------------------------ #
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    await handle_reaction(payload, add=True)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    await handle_reaction(payload, add=False)


async def handle_reaction(payload: discord.RawReactionActionEvent, add: bool):
    menu = REACTION_ROLES.get(str(payload.message_id))
    if menu is None:
        return
    emoji = str(payload.emoji).rstrip("️")   # normalize variant selector
    role_id = menu.get(emoji)
    if role_id is None:
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    # payload.member is present on add, None on remove -> fetch.
    member = payload.member if payload.member is not None else await safe_fetch_member(guild, payload.user_id)
    if member is None or member.bot:
        return
    role = guild.get_role(int(role_id))
    if role is None:
        return
    try:
        if add:
            await member.add_roles(role, reason="Reaction role")
        else:
            await member.remove_roles(role, reason="Reaction role removed")
    except discord.Forbidden:
        print(f"[BersamaAi] Missing permissions to manage role {role.name}")
    except discord.HTTPException as exc:
        print(f"[BersamaAi] Reaction-role HTTP error: {exc}")


async def safe_fetch_member(guild: discord.Guild, user_id: int):
    try:
        return await guild.fetch_member(user_id)
    except discord.HTTPException:
        return None


# ---- Messages: XP + AI + commands ---------------------------------------- #
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    await award_xp(message)
    # Treat as an AI request only on an EXPLICIT @mention in the text body,
    # not on a reply-ping (which also adds the bot to message.mentions).
    raw = message.content
    explicitly_mentioned = (f"<@{bot.user.id}>" in raw) or (f"<@!{bot.user.id}>" in raw)
    if explicitly_mentioned and not raw.lstrip().startswith("!"):
        await handle_ai(message)
        return
    await bot.process_commands(message)


async def award_xp(message: discord.Message):
    uid = message.author.id
    now = time.time()
    gain = random.randint(XP_MIN, XP_MAX)
    try:
        with db() as c:
            row = c.execute("SELECT xp, last_msg FROM xp WHERE user_id=?", (uid,)).fetchone()
            if row:
                if now - row["last_msg"] < XP_COOLDOWN:
                    return
                before = row["xp"]
                new_xp = before + gain
                c.execute("UPDATE xp SET xp=?, last_msg=? WHERE user_id=?", (new_xp, now, uid))
            else:
                before = 0
                new_xp = gain
                c.execute("INSERT INTO xp (user_id, xp, last_msg) VALUES (?,?,?)", (uid, new_xp, now))
    except sqlite3.Error as exc:
        print(f"[BersamaAi] XP db error: {exc}")
        return

    old_level = level_from_xp(before)
    new_level = level_from_xp(new_xp)
    if new_level > old_level:
        # Iterate every crossed level so multi-level jumps still grant each reward.
        for lvl in range(old_level + 1, new_level + 1):
            await on_level_up(message.author, message.guild, lvl)


async def on_level_up(member: discord.Member, guild: discord.Guild, level: int):
    ch = guild.get_channel(CHANNELS.get("level_ups"))
    if ch:
        try:
            await ch.send(f"🎉 {member.mention} just reached **level {level}**! Keep it up. 💪")
        except discord.HTTPException as exc:
            print(f"[BersamaAi] level-up send error: {exc}")
    reward = LEVEL_REWARDS.get(level)
    if reward:
        role = guild.get_role(reward)
        if role:
            try:
                await member.add_roles(role, reason=f"Reached level {level}")
            except discord.HTTPException as exc:
                print(f"[BersamaAi] level-up role error: {exc}")


# ---- AI (@mention and !ai) — GLM via Z.ai (OpenAI-compatible) ------------ #
def _ai_global_allowed() -> bool:
    """Rolling-window gate. Returns True and records a slot if a call is allowed."""
    now = time.time()
    while _ai_calls and now - _ai_calls[0] > AI_GLOBAL_WINDOW:
        _ai_calls.popleft()
    if len(_ai_calls) >= AI_GLOBAL_MAX:
        return False
    _ai_calls.append(now)
    return True


async def handle_ai(message: discord.Message, override_text: str | None = None):
    uid = message.author.id
    now = time.time()
    waited = now - _ai_last_use.get(uid, 0)
    if waited < AI_COOLDOWN:
        await _safe_reply(message, f"⏳ Slow down — try again in {int(AI_COOLDOWN - waited)}s (fair-use limit).")
        return
    _ai_last_use[uid] = now   # throttle further retries regardless of outcome

    # Build + validate the question (truncate to bound cost).
    question = override_text if override_text is not None else message.content
    for token in (f"<@{bot.user.id}>", f"<@!{bot.user.id}>"):
        question = question.replace(token, "")
    question = question.strip()[:AI_INPUT_MAX]
    if not question:
        await _safe_reply(
            message,
            "Ask me something! Example: `@BersamaAi what's the best free AI for slides?`",
        )
        return

    if ai_client is None:
        await _safe_reply(
            message,
            "🤖 The AI assistant isn't configured yet — a moderator will enable it soon. "
            f"Meanwhile, ask in <#{CHANNELS['ask_anything']}>!",
        )
        return

    if not _ai_global_allowed():
        await _safe_reply(message, "🏭 The AI is really busy right now — please try again in a minute.")
        return

    async with message.channel.typing():
        async with _ai_sem:
            try:
                resp = await ai_client.chat.completions.create(
                    model=GLM_MODEL,
                    max_tokens=800,
                    messages=[
                        {"role": "system", "content": CONFIG["ai_system_prompt"]},
                        {"role": "user", "content": question},
                    ],
                )
                answer = (resp.choices[0].message.content or "").strip() or "…"
            except Exception as exc:  # noqa: BLE001 — never crash the bot on an AI error
                print(f"[BersamaAi] AI error: {exc}")
                answer = "⚠️ The AI hit an error. Please try again in a moment."

    for i in range(0, len(answer), AI_MAX_CHARS):
        if not await _safe_reply(message, answer[i:i + AI_MAX_CHARS]):
            break


async def _safe_reply(message: discord.Message, text: str) -> bool:
    """Reply, swallowing HTTP errors. Returns False if the send failed."""
    try:
        await message.reply(text)
        return True
    except discord.HTTPException as exc:
        print(f"[BersamaAi] reply send error: {exc}")
        return False


# ---- Prefix commands ----------------------------------------------------- #
@bot.command(name="rules")
async def cmd_rules(ctx: commands.Context):
    await ctx.send(f"📋 Read the rules here: <#{CHANNELS['rules']}>")


@bot.command(name="resources")
async def cmd_resources(ctx: commands.Context):
    await ctx.send(
        f"📚 Curated resources: <#{CHANNELS['curated_resources']}> · "
        f"Tools directory: <#{CHANNELS['tools_directory']}>"
    )


@bot.command(name="ai")
async def cmd_ai(ctx: commands.Context, *, question: str = ""):
    await handle_ai(ctx.message, override_text=question)


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandNotFound):
        return
    print(f"[BersamaAi] command error ({ctx.command}): {error}")
    try:
        await ctx.send("⚠️ Couldn't run that command. Try `/help` or ask a moderator.")
    except discord.HTTPException:
        pass


# ---- Slash commands ------------------------------------------------------ #
@bot.tree.command(name="rank", description="See your level, XP, and rank.")
async def cmd_rank(interaction: discord.Interaction):
    with db() as c:
        row = c.execute("SELECT xp FROM xp WHERE user_id=?", (interaction.user.id,)).fetchone()
    total = row["xp"] if row else 0
    level = level_from_xp(total)
    floor = xp_for_level(level)
    need = 5 * (level ** 2) + 50 * level + 100
    into = total - floor
    bar_len = 20
    filled = int(into / need * bar_len) if need else 0
    bar = "█" * filled + "░" * (bar_len - filled)
    embed = discord.Embed(
        title=f"📊 {interaction.user.display_name}'s Rank",
        color=interaction.user.colour,
        description=(
            f"**Level {level}**\n"
            f"Total XP: **{total}**\n"
            f"`{bar}` {into}/{need} XP → level {level + 1}"
        ),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="Top members by XP.")
async def cmd_leaderboard(interaction: discord.Interaction):
    with db() as c:
        rows = c.execute("SELECT user_id, xp FROM xp ORDER BY xp DESC LIMIT 10").fetchall()
    if not rows:
        await interaction.response.send_message("No XP earned yet — start chatting! 💬")
        return
    lines = []
    for i, r in enumerate(rows, 1):
        member = interaction.guild.get_member(r["user_id"])
        name = member.display_name if member else f"<@{r['user_id']}>"
        lines.append(f"**{i}.** {name} — Lv {level_from_xp(r['xp'])} ({r['xp']} XP)")
    embed = discord.Embed(
        title="🏆 Leaderboard",
        description="\n".join(lines),
        color=0xF1C40F,
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="help", description="Show BersamaAi bot help.")
async def cmd_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 BersamaAi Bot",
        color=0x3498DB,
        description=(
            "**Slash:** `/rank` `/leaderboard` `/help`\n"
            "**Prefix:** `!rules` `!resources` `!ai <question>`\n"
            "**AI:** mention me — `@BersamaAi <question>`\n"
            f"**Roles:** react in <#{CHANNELS['get_roles']}>"
        ),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"[BersamaAi] slash error: {error}")
    msg = "⚠️ Something went wrong — a moderator will look into it."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


def main():
    validate_config()
    init_db()
    global _ai_sem
    _ai_sem = asyncio.Semaphore(AI_CONCURRENCY)
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
