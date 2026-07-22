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
import ipaddress
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

log = logging.getLogger("bersama")

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

TOKEN = os.environ.get("DISCORD_TOKEN")        # same token as the MCP jar; validated in main()
ZAI_API_KEY = os.environ.get("ZAI_API_KEY", "")
ZAI_BASE_URL = os.environ.get("ZAI_BASE_URL", "https://api.z.ai/api/coding/paas/v4")
GLM_MODEL = os.environ.get("GLM_MODEL", "glm-5.2")

GUILD_ID = int(CONFIG["guild_id"])
CHANNELS = {k: int(v) for k, v in CONFIG["channels"].items()}
ROLES = {k: int(v) for k, v in CONFIG["roles"].items()}
NEWS_CHANNELS = [int(x) for x in CONFIG.get("news_channels", [])]

# Engagement loop: seed these on every webhook-authored news card so members
# have something to click (raises reaction signal ~5–10×, the TLDR pattern).
# MUST match pipeline/engagement.py SEED_EMOJIS so the sweep subtracts exactly
# these (the bot's own reactions carry the `me` flag and are excluded upstream).
SEED_EMOJIS = ("👍", "🔥", "👎")   # useful / amazing / not-for-me


def _norm_emoji(emoji: str) -> str:
    """Strip U+FE0F variation selectors ANYWHERE (trailing or inside ZWJ / keycap /
    flag sequences) so client-side rendering differences never break reaction matching."""
    return emoji.replace("️", "")


# Normalize emoji keys once so client-side rendering differences never break matching.
REACTION_ROLES = {
    msg_id: {_norm_emoji(emo): int(role_id) for emo, role_id in mapping.items()}
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
AI_TIMEOUT = 30           # seconds to wait for one Z.ai response (bounds hung calls)
AI_CONTEXT_MSGS = 50      # recent channel messages fed as conversation context (raise for longer "memory"; cost scales linearly)
AI_CONTEXT_PER_MSG = 500  # cap each context message (text + link-preview embed) length
AI_LINK_FETCH = True       # fetch full page content for recent links via Jina Reader (JS pages too)
AI_LINK_FETCH_MAX = 2      # max links fetched per @mention (most recent first)
AI_LINK_FETCH_CHARS = 4000  # truncate each fetched page (bounds token cost; ~1k tokens/page)
AI_LINK_FETCH_TIMEOUT = 8  # seconds per fetch — best-effort, never blocks the bot

# Replies never need to ping anyone — suppress all mentions on every AI send so
# prompt-injected GLM output can't mass-ping roles / @everyone.
AI_ALLOWED = discord.AllowedMentions.none()
# Belt-and-suspenders: strip mention syntax from GLM output before sending.
_MENTION_RE = re.compile(r"<@!?\d+>|<@&\d+>|@(?:everyone|here)\b")
# Bare http(s) URLs in message text — used to optionally fetch full page content.
_URL_RE = re.compile(r"https?://[^\s<>\"'\])]+", re.IGNORECASE)

DB_PATH = BASE / "bersama.db"


# --------------------------------------------------------------------------- #
# SQLite — proper open/close context manager (no handle leaks), WAL mode.
# --------------------------------------------------------------------------- #
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")          # set once, not per connection
        c.execute("PRAGMA synchronous=NORMAL")        # safe pairing with WAL
        c.execute(
            """CREATE TABLE IF NOT EXISTS xp (
                user_id   INTEGER PRIMARY KEY,
                xp        INTEGER DEFAULT 0,
                last_msg  REAL    DEFAULT 0
            )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_xp_desc ON xp(xp DESC)")


def validate_config():
    if not TOKEN:
        raise SystemExit(
            "[BersamaAi] DISCORD_TOKEN is not set. Copy .env.example to .env and paste "
            "the bot token (the SAME token the MCP jar uses)."
        )
    required_channels = [
        "welcome", "introductions", "rules", "get_roles", "ai_chat",
        "level_ups", "ask_anything", "curated_resources", "tools_directory",
    ]
    missing = [k for k in required_channels if k not in CHANNELS]
    if missing:
        raise SystemExit(f"[BersamaAi] config.json missing channels: {missing}")
    if "newcomer" not in ROLES:
        raise SystemExit("[BersamaAi] config.json missing 'newcomer' role")
    if ai_client is not None and not CONFIG.get("ai_system_prompt"):
        raise SystemExit(
            "[BersamaAi] config.json missing 'ai_system_prompt' — required when AI is enabled."
        )


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


@bot.event
async def on_guild_join(guild: discord.Guild):
    """Defense-in-depth: this bot serves ONE guild. If the shared token ever gets the
    bot added elsewhere, leave immediately so AI budget / XP can't be drained there."""
    if guild.id != GUILD_ID:
        log.warning("Added to non-allowlisted guild %s (%s) — leaving.", guild.id, guild.name)
        try:
            await guild.leave()
        except discord.HTTPException as exc:
            log.error("Failed to leave guild %s: %s", guild.id, exc)


@bot.check
async def _guild_only(ctx: commands.Context) -> bool:
    """Reject prefix commands from any guild that isn't ours."""
    return ctx.guild is not None and ctx.guild.id == GUILD_ID

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
    global _synced, _hb_started, _seed_started
    log.info("Logged in as %s (%s)", bot.user, getattr(bot.user, "id", "?"))
    log.info("Reaction-role menus: %s", list(REACTION_ROLES))
    log.info("AI (GLM via Z.ai): %s", f"ON ({GLM_MODEL})" if ai_client else "OFF")
    log.info("Seed reactions: %s",
             f"{len(NEWS_CHANNELS)} news channel(s)" if NEWS_CHANNELS else "OFF (no news_channels in config)")

    if not _hb_started:
        _hb_started = True
        heartbeat.start()          # must run inside the loop; on_ready guarantees that
    if not _seed_started and NEWS_CHANNELS:
        _seed_started = True
        seed_reactions.start()

    # Audit configured IDs against the live guild so stale config fails loudly, not silently.
    guild_obj = bot.get_guild(GUILD_ID)
    if guild_obj is not None:
        for name, cid in CHANNELS.items():
            if guild_obj.get_channel(cid) is None:
                log.warning("config channel '%s' (id=%s) not found in guild", name, cid)
        for name, rid in ROLES.items():
            if guild_obj.get_role(rid) is None:
                log.warning("config role '%s' (id=%s) not found in guild", name, rid)
        for lvl, rid in LEVEL_REWARDS.items():
            if guild_obj.get_role(rid) is None:
                log.warning("level-reward role for level %s (id=%s) not found in guild", lvl, rid)
        for msg_id, mapping in REACTION_ROLES.items():
            for emo, rid in mapping.items():
                if guild_obj.get_role(rid) is None:
                    log.warning("reaction-role %r on msg %s -> missing role id=%s", emo, msg_id, rid)

    if _synced:
        return
    try:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        _synced = True
        log.info("Synced %d slash commands.", len(synced))
    except discord.HTTPException as exc:
        log.error("FAILED to sync slash commands: %s", exc)


# ---- Welcome ------------------------------------------------------------- #
@bot.event
async def on_member_join(member: discord.Member):
    try:
        role = member.guild.get_role(ROLES["newcomer"])
        if role:
            await member.add_roles(role, reason="Auto-role on join")
    except discord.Forbidden:
        log.error(
            "PERMISSIONS: cannot add 'newcomer' role on join — bot role is too low or "
            "Manage Roles is missing. Auto-role will fail on EVERY join until fixed."
        )
    except discord.HTTPException as exc:
        log.error("on_member_join role error: %s", exc)
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
                f"— just type `@{bot.user.name}` + your question."
            )
        except discord.HTTPException as exc:
            log.error("welcome send error: %s", exc)


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
    emoji = _norm_emoji(str(payload.emoji))   # normalize variant selectors
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
        log.error("PERMISSIONS: cannot manage reaction role %r — bot role too low?", role.name)
    except discord.HTTPException as exc:
        log.error("reaction-role HTTP error: %s", exc)


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
    if message.guild.id != GUILD_ID:        # hard allowlist — ignore any other guild
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
        log.error("XP db error for uid=%s: %s", uid, exc)
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
            log.error("level-up send error: %s", exc)
    reward = LEVEL_REWARDS.get(level)
    if reward:
        role = guild.get_role(reward)
        if role:
            try:
                await member.add_roles(role, reason=f"Reached level {level}")
            except discord.Forbidden:
                log.error(
                    "PERMISSIONS: cannot add level-%s reward role %r — bot role too low.",
                    level, role.name,
                )
            except discord.HTTPException as exc:
                log.error("level-up role error: %s", exc)


# ---- AI (@mention and !ai) — GLM via Z.ai (OpenAI-compatible) ------------ #
def _ai_global_reserve() -> float | None:
    """Rolling-window gate. If a call is allowed, reserve+return its timestamp (to be
    refunded on failure via _refund_slot); if the window is full, return None.
    Also reaps stale per-user cooldowns so _ai_last_use can't leak forever."""
    now = time.time()
    while _ai_calls and now - _ai_calls[0] > AI_GLOBAL_WINDOW:
        _ai_calls.popleft()
    if len(_ai_last_use) > 256:
        cutoff = now - 3600
        for u in [u for u, t in _ai_last_use.items() if t < cutoff]:
            del _ai_last_use[u]
    if len(_ai_calls) >= AI_GLOBAL_MAX:
        return None
    _ai_calls.append(now)
    return now


def _refund_slot(slot: float) -> None:
    """Release a global slot that never reached Z.ai successfully (timeout/error)."""
    try:
        _ai_calls.remove(slot)
    except ValueError:
        pass


def _is_safe_fetch_url(url: str) -> bool:
    """SSRF guard: only allow public http(s) URLs — block localhost, private/loopback/link-local
    IPs, and cloud-metadata hosts so a malicious link can't probe the VM's internals."""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower()
    if not host or host in ("localhost", "metadata.google.internal") or host.endswith(".internal"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    except ValueError:
        pass  # hostname (not an IP literal) — allowed
    return True


async def _fetch_url_markdown(url: str) -> str:
    """Fetch a URL's content as clean markdown via Jina Reader (handles JS-rendered pages like
    Luma). Best-effort: returns truncated text or '' on any failure/timeout. Never raises."""
    if not _is_safe_fetch_url(url):
        return ""
    try:
        headers = {"Accept": "text/plain"}
        key = os.environ.get("JINA_API_KEY")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        timeout = aiohttp.ClientTimeout(total=AI_LINK_FETCH_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://r.jina.ai/" + url, headers=headers) as resp:
                if resp.status != 200:
                    return ""
                text = await resp.text(errors="replace")
        return text.strip()[:AI_LINK_FETCH_CHARS]
    except Exception:
        return ""


def _format_msg_for_context(m: discord.Message) -> str:
    """One-line summary of a message for LLM context: "<author>: <text> [link preview: …]".
    Includes Discord's auto-generated link embed (title + description) so the bot understands
    pages members share (e.g. an event) without needing to browse the web itself."""
    bot_id = bot.user.id if bot.user else None
    if m.author.id == bot_id:
        who = "BersamaAi (you, earlier)"
    elif m.author.bot:
        return ""
    else:
        who = m.author.display_name
    text = m.clean_content.replace("\n", " ").strip()
    for emb in m.embeds:
        title = (emb.title or "").strip()
        desc = (emb.description or "").replace("\n", " ").strip()
        if title or desc:
            text += " [link preview: " + title + (f" — {desc[:240]}" if desc else "") + "]"
    if not text:
        return ""
    return f"{who}: {text[:AI_CONTEXT_PER_MSG]}"


async def _gather_channel_context(message: discord.Message) -> str:
    """Recent messages before this one (oldest→newest) so the bot can answer questions about
    the live conversation — e.g. "is that event worth joining?" right after a member shared a
    link + commentary. Includes each message's text + Discord link embed, and (optionally) the
    full page content of the most recent links via Jina Reader for richer answers."""
    try:
        recent = [m async for m in message.channel.history(limit=AI_CONTEXT_MSGS + 1, before=message)]
    except discord.HTTPException:
        return ""
    recent.reverse()  # oldest first
    transcript = "\n".join(line for line in (_format_msg_for_context(m) for m in recent) if line)

    # Optionally enrich with full page content for the most recent links (handles JS-rendered
    # pages like Luma — gives the LLM agenda/FAQ/etc., not just the embed preview).
    if AI_LINK_FETCH and transcript:
        seen: set[str] = set()
        unique: list[str] = []
        for u in _URL_RE.findall(transcript):
            if u not in seen and _is_safe_fetch_url(u):
                seen.add(u)
                unique.append(u)
        targets = unique[-AI_LINK_FETCH_MAX:]  # most recent N links
        if targets:
            pages = await asyncio.gather(*[_fetch_url_markdown(u) for u in targets])
            fetched = [f"\n[{u} — full page content:]\n{md}" for u, md in zip(targets, pages) if md]
            if fetched:
                transcript += "\n\n" + "\n".join(fetched)
    return transcript


def _ai_messages_for(question: str, message: discord.Message, context: str) -> list[dict]:
    """Build the LLM message list: persona (system) + recent channel context + the question."""
    parts: list[str] = []
    if context:
        parts.append(
            "Recent conversation in this channel (oldest first) plus any fetched link content. "
            "For specific facts (names, times, places, links) use only this context; if a detail "
            "isn't here, say you're not sure rather than inventing it. The question is on the last line:\n"
            + context
        )
    parts.append(f"---\n{message.author.display_name} (@you): {question}")
    return [
        {"role": "system", "content": CONFIG["ai_system_prompt"]},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


async def handle_ai(message: discord.Message, override_text: str | None = None):
    uid = message.author.id
    now = time.time()
    waited = now - _ai_last_use.get(uid, 0)
    if waited < AI_COOLDOWN:
        await _safe_reply(message, f"⏳ Slow down — try again in {int(AI_COOLDOWN - waited)}s (fair-use limit).")
        return

    # Build + validate the question (truncate to bound cost).
    question = override_text if override_text is not None else message.content
    for token in (f"<@{bot.user.id}>", f"<@!{bot.user.id}>"):
        question = question.replace(token, "")
    question = question.strip()[:AI_INPUT_MAX]
    if not question:
        await _safe_reply(
            message,
            f"Ask me something! Example: `@{bot.user.name} what's the best free AI for slides?`",
        )
        return

    if ai_client is None:
        await _safe_reply(
            message,
            "🤖 The AI assistant isn't configured yet — a moderator will enable it soon. "
            f"Meanwhile, ask in <#{CHANNELS['ask_anything']}>!",
        )
        return

    slot = _ai_global_reserve()
    if slot is None:
        log.warning("AI global cap hit — rejecting uid=%s", uid)
        await _safe_reply(message, "🏭 The AI is really busy right now — please try again in a minute.")
        return

    # Count this as a real attempt only now (after every early return), so no-op pings
    # and "AI disabled"/"busy" paths don't lock the user out for 30s.
    _ai_last_use[uid] = time.time()

    # Give the bot awareness of the live conversation so it can answer questions like
    # "is that event worth joining?" right after a member shared a link + details.
    context = await _gather_channel_context(message)
    messages_for_llm = _ai_messages_for(question, message, context)

    async with _ai_sem:
        async with message.channel.typing():   # typing only while actually generating
            try:
                resp = await asyncio.wait_for(
                    ai_client.chat.completions.create(
                        model=GLM_MODEL,
                        max_completion_tokens=800,  # Z.ai GLM-5.2 ignores max_tokens; this is the honored param
                        messages=messages_for_llm,
                    ),
                    timeout=AI_TIMEOUT,
                )
                answer = (resp.choices[0].message.content or "").strip() or "…"
            except asyncio.TimeoutError:
                _refund_slot(slot)
                log.warning("AI timeout (%ss) for uid=%s", AI_TIMEOUT, uid)
                answer = "⚠️ The AI took too long — please try again in a moment."
            except Exception as exc:  # noqa: BLE001 — never crash the bot on an AI error
                _refund_slot(slot)
                log.error("AI error for uid=%s: %s", uid, exc)
                answer = "⚠️ The AI hit an error. Please try again in a moment."

    # Defense-in-depth: strip any mention syntax GLM might emit (allowed_mentions is the real fix).
    answer = _MENTION_RE.sub("", answer).strip() or "…"

    for i in range(0, len(answer), AI_MAX_CHARS):
        if not await _safe_reply(message, answer[i:i + AI_MAX_CHARS]):
            break


async def _safe_reply(message: discord.Message, text: str) -> bool:
    """Reply, swallowing HTTP errors. Returns False if the send failed.
    Suppresses all mentions (AI_ALLOWED) so injected output can't ping."""
    try:
        await message.reply(text, allowed_mentions=AI_ALLOWED)
        return True
    except discord.HTTPException as exc:
        log.error("reply send error: %s", exc)
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
    if isinstance(error, commands.CheckFailure):
        return          # silently ignore prefix commands rejected by the guild allowlist
    log.error("command error (%s): %s", ctx.command, error)
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
            f"**AI:** mention me — `@{bot.user.name} <question>`\n"
            f"**Roles:** react in <#{CHANNELS['get_roles']}>"
        ),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.error("slash error: %s", error)
    msg = "⚠️ Something went wrong — a moderator will look into it."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


# ---- Liveness (24/7) ----------------------------------------------------- #
_heartbeat_misses = 0
_hb_started = False


@tasks.loop(minutes=5)
async def heartbeat():
    """Force a restart if the Gateway has been unreachable for two consecutive checks.
    NSSM/systemd Restart=always then relaunch us with a clean connection."""
    global _heartbeat_misses
    lat = bot.latency
    broken = (lat != lat) or (lat > 60.0)   # NaN, or no heartbeat for >60s
    if broken:
        _heartbeat_misses += 1
        log.error("HEARTBEAT: gateway latency=%s (miss #%d)", lat, _heartbeat_misses)
        if _heartbeat_misses >= 2:
            log.critical("HEARTBEAT: gateway broken for 10+ min — forcing process restart.")
            os._exit(1)
    else:
        if _heartbeat_misses:
            log.info("HEARTBEAT: recovered (latency=%dms).", int(lat * 1000))
        _heartbeat_misses = 0


@heartbeat.before_loop
async def _heartbeat_before():
    await bot.wait_until_ready()


# ---- Engagement loop: seed reactions on news cards ----------------------- #
_seed_started = False


@tasks.loop(minutes=15)
async def seed_reactions():
    """Every 15 min, add 👍🔥😐 to any webhook-authored news card missing them.

    Stateless + idempotent — no posted_log read, no cross-repo coupling: "a
    webhook-authored message in a news channel" is the entire filter. Only
    webhook messages pass (`webhook_id is not None`); the bot's own messages,
    the MCP jar's messages, and member messages all have webhook_id=None, so it
    never reacts to itself. Re-runs add nothing — existing reactions are checked
    first. Emoji are normalized through _norm_emoji so variation-selector
    differences never cause a double-add."""
    if not NEWS_CHANNELS:
        return
    seeded = 0
    for cid in NEWS_CHANNELS:
        ch = bot.get_channel(cid) or await bot.fetch_channel(cid)
        if ch is None:
            continue
        try:
            async for msg in ch.history(limit=20):
                if msg.webhook_id is None:
                    continue  # only seed the webhook-authored news cards
                have = {_norm_emoji(str(r.emoji)) for r in msg.reactions}
                for emo in SEED_EMOJIS:
                    if _norm_emoji(emo) not in have:
                        try:
                            await msg.add_reaction(emo)
                            seeded += 1
                        except discord.HTTPException as exc:
                            log.warning("seed_reactions: add %r on msg %s failed: %s", emo, msg.id, exc)
        except discord.HTTPException as exc:
            log.warning("seed_reactions: channel %s history failed: %s", cid, exc)
    # Log a count only when something was added — quiet on steady-state (every card
    # already seeded), loud the first run after deploy so success is observable.
    if seeded:
        log.info("seed_reactions: added %d reaction(s) across %d news channel(s)", seeded, len(NEWS_CHANNELS))


@seed_reactions.before_loop
async def _seed_before():
    await bot.wait_until_ready()


def _configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        root.addHandler(handler)
    # Silence chatty per-request loggers so bersama.log isn't flooded with an httpx INFO
    # line on every GLM call. Warnings/errors still come through.
    for noisy in ("httpx", "openai", "discord.http"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main():
    # Windows consoles default to cp1252; force UTF-8 so logging an emoji role/user
    # name never raises UnicodeEncodeError. No-op on Linux/macOS.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    _configure_logging()
    validate_config()
    init_db()
    global _ai_sem
    _ai_sem = asyncio.Semaphore(AI_CONCURRENCY)
    bot.run(TOKEN, log_handler=None)   # we configured root logging in _configure_logging()


if __name__ == "__main__":
    main()
