# bot.py — discord.py-self forwarder
# • Focused logging (INFO / DEBUG)
# • Resilient HTTP rebuild
# • Hard exit after N consecutive REST failures (Docker restarts container)
# • Heart-beat file updated only when loops succeed — Docker health-check
#   restarts container if loops hang

import os, re, random, asyncio, time, sys, socket, ssl, inspect, logging, pathlib, datetime
from datetime import UTC
from dotenv import load_dotenv

import discord
from discord import HTTPException, Embed
from discord.ext import commands, tasks
from discord.http import HTTPClient, Route
from discord.utils import MISSING
from curl_cffi.curl import CurlError
import aiohttp

load_dotenv()

# ── HEART-BEAT (for Docker health-check) ──────────────────────────────────
HB_PATH = pathlib.Path("/tmp/ttforwarder_heartbeat")

def update_hb() -> None:
    """Touch heartbeat file with current UTC timestamp."""
    try:
        HB_PATH.write_text(datetime.datetime.now(UTC).isoformat())
    except Exception:
        pass  # container continues even if /tmp is temporarily read-only

# ── LOGGING ───────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE  = os.getenv("LOG_FILE", "ttforwarder.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ttforwarder")
log.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
for noisy in ("discord", "aiohttp", "asyncio", "curl_cffi"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# ── CONFIG ───────────────────────────────────────────────────────────────
TOK_TRIBEIQ     = os.getenv("TOK_TRIBEIQ", "")
TOK_EYESINTHEHOOK = os.getenv("TOK_EYESINTHEHOOK", "")
TOK_HOODNARRATOR = os.getenv("TOK_HOODNARRATOR", "")
TOK_READYTOSPY  = os.getenv("TOK_READYTOSPY", "")
RICK_APP_ID     = int(os.getenv("RICK_APP_ID"))

CMD_CH     = int(os.getenv("CMD_CH"))
TT_CH_ID   = int(os.getenv("TT_CH_ID"))
X_CH_ID    = int(os.getenv("X_CH_ID"))
CALL_CH_ID = int(os.getenv("CALL_CH_ID"))
BURP_CH_ID = int(os.getenv("BURP_CH_ID"))
GL_CH_ID   = int(os.getenv("GL_CH_ID"))

# These are kept for backward compatibility but no longer used directly
INTERVAL_SECONDS = int(os.getenv("MONITOR_INTERVAL", "60"))
BURP_INTERVAL    = int(os.getenv("BURP_INTERVAL", "60"))

# Increased delays and more variation for command-based bots
COMMAND_DELAY    = int(os.getenv("COMMAND_DELAY", "30"))  # Increased from 15 to 30
COPY_DELAY_MIN   = int(os.getenv("COPY_DELAY_MIN", "5"))  # Increased from 3 to 5
COPY_DELAY_MAX   = int(os.getenv("COPY_DELAY_MAX", "15")) # Increased from 5 to 15
BURP_COOLDOWN    = int(os.getenv("BURP_COOLDOWN", "600"))

FAIL_THRESHOLD   = 3   # after N consecutive REST failures → os._exit(1)

# ── STATE ────────────────────────────────────────────────────────────────
isMonitoringTT   = True
isMonitoringBurp = True
processed_tt_embeds, processed_tweets = set(), set()
burp_cycle_processed = set()
last_trending_time, last_burp_time = 0.0, 0.0

# Dictionary to store bot instances by label
bot_instances = {}

# Role rotation tracking
tt_command_bot = None  # Bot that sends /tt commands
tt_scanner_bot = None  # Bot that scans and forwards tweets
burp_command_bot = None  # Bot that sends /burp commands
burp_scanner_bot = None  # Bot that scans and forwards burp tokens

# Lists of available bots for each role
tt_command_candidates = ["TRIBEIQ", "EYESINTHEHOOK"]  # Bots that can send /tt
tt_scanner_candidates = ["HOODNARRATOR", "READYTOSPY"]  # Bots that can scan tweets
burp_command_candidates = ["TRIBEIQ", "EYESINTHEHOOK"]  # Bots that can send /burp
burp_scanner_candidates = ["HOODNARRATOR", "READYTOSPY"]  # Bots that can scan burp tokens

# Track all active bots
active_bots = set()

ALLOWED_AUTHORS = {
    219212900743512065, # Simple
    303754867044777985, # Dej
    979973170251567136, # Kit
    366245216174342144,
    402836835627302922,
    290597129016311809,
    358568364375015424,
}

NET_ERR = (
    discord.InvalidData, HTTPException, CurlError,
    aiohttp.ClientError, OSError, socket.gaierror, ssl.SSLError,
)

TW_URL_RE = re.compile(r"(https?://twitter\.com/[^\s\)\]]+)")
TOKEN_RE  = re.compile(r"(0x[a-fA-F0-9]{40}|[A-Za-z0-9]+)$")

async def safe_command_call(cmd, channel, guild, max_retries=3):
    """Safely call a Discord command with retry mechanism."""
    # Static variables to track last call times
    if not hasattr(safe_command_call, "last_tt_call"):
        safe_command_call.last_tt_call = 0
    if not hasattr(safe_command_call, "last_burp_call"):
        safe_command_call.last_burp_call = 0

    # Check if this is a tt or burp command
    cmd_name = getattr(cmd, "name", "").lower()
    current_time = time.time()

    # Enforce cooldown for tt commands (3 minutes = 180 seconds)
    if cmd_name == "tt":
        time_since_last_call = current_time - safe_command_call.last_tt_call
        if time_since_last_call < 180:  # 3 minutes
            log.warning("[COMMAND_CALL] Discarding tt command - called too soon (%.2f seconds since last call, minimum is 180 seconds)",
                       time_since_last_call)
            return None
        safe_command_call.last_tt_call = current_time

    # Enforce cooldown for burp commands (10 minutes = 600 seconds)
    elif cmd_name == "burp":
        time_since_last_call = current_time - safe_command_call.last_burp_call
        if time_since_last_call < 600:  # 10 minutes
            log.warning("[COMMAND_CALL] Discarding burp command - called too soon (%.2f seconds since last call, minimum is 600 seconds)",
                       time_since_last_call)
            return None
        safe_command_call.last_burp_call = current_time

    retries = 0
    last_error = None

    while retries < max_retries:
        try:
            return await cmd.__call__(channel=channel, guild=guild)
        except NET_ERR as e:
            last_error = e
            retries += 1
            backoff = min(2 ** retries, 60)  # Exponential backoff, max 60 seconds
            log.warning("[COMMAND_CALL] %r, retrying in %s seconds (%s/%s)", 
                       e, backoff, retries, max_retries)
            await asyncio.sleep(backoff)

    # If we've exhausted retries, raise the last exception
    log.error("[COMMAND_CALL] Failed after %s retries: %r", max_retries, last_error)
    raise last_error

async def safe_fetch_commands(ch, max_retries=5):
    retries = 0
    while retries < max_retries:
        try:
            return await ch.application_commands()
        except ValueError:
            await asyncio.sleep(1)
        except NET_ERR:
            retries += 1
            backoff = min(2 ** retries, 60)  # Exponential backoff, max 60 seconds
            log.warning("[FETCH_COMMANDS] Network error, retrying in %s seconds (%s/%s)", 
                       backoff, retries, max_retries)
            await asyncio.sleep(backoff)

    # If we've exhausted retries, raise the last exception
    log.error("[FETCH_COMMANDS] Failed after %s retries", max_retries)
    return []  # Return empty list as fallback

# ── ROLE ROTATION FUNCTIONS ───────────────────────────────────────────────
def rotate_tt_roles():
    """Rotate which bots handle TT commands and scanning."""
    global tt_command_bot, tt_scanner_bot, active_bots

    # Store old command bot for later
    old_command_bot = tt_command_bot

    # Get available command candidates (excluding current scanner bot)
    available_cmd_bots = [bot for bot in tt_command_candidates if bot in active_bots and bot != tt_scanner_bot]

    # Get available scanner candidates (excluding current command bot)
    available_scan_bots = [bot for bot in tt_scanner_candidates if bot in active_bots and bot != tt_command_bot]

    # If we have available bots, rotate roles
    if available_cmd_bots and available_scan_bots:
        # If there's a current command bot, move to next in rotation
        if tt_command_bot in available_cmd_bots:
            idx = available_cmd_bots.index(tt_command_bot)
            tt_command_bot = available_cmd_bots[(idx + 1) % len(available_cmd_bots)]
        else:
            tt_command_bot = available_cmd_bots[0]

        # If there's a current scanner bot, move to next in rotation
        if tt_scanner_bot in available_scan_bots:
            idx = available_scan_bots.index(tt_scanner_bot)
            tt_scanner_bot = available_scan_bots[(idx + 1) % len(available_scan_bots)]
        else:
            tt_scanner_bot = available_scan_bots[0]

        log.info("[TT_ROTATION] New roles - Command: %s, Scanner: %s", tt_command_bot, tt_scanner_bot)

        # If command bot changed, transfer tasks to the new bot
        if old_command_bot != tt_command_bot:
            # Use bot_instances dictionary instead of discord.client._clients
            # Reset the _tt_started flag on the old bot if it exists
            if old_command_bot in bot_instances:
                old_bot = bot_instances[old_command_bot]
                # Stop the task loop if it exists
                if hasattr(old_bot, '_tt_loop') and old_bot._tt_loop.is_running():
                    old_bot._tt_loop.cancel()
                    log.info("[%s] TT command task loop stopped", old_command_bot)
                old_bot._tt_started = False
                log.info("[%s] TT command tasks detached from old bot", old_command_bot)

            # Attach tasks to the new command bot
            if tt_command_bot in bot_instances:
                new_bot = bot_instances[tt_command_bot]
                # Reset the flag first to ensure tasks are attached
                new_bot._tt_started = False
                _attach_tt_tasks(new_bot)
                log.info("[%s] TT command tasks transferred to new bot", tt_command_bot)
    else:
        log.warning("[TT_ROTATION] Not enough available bots for rotation")

def rotate_burp_roles():
    """Rotate which bots handle BURP commands and scanning."""
    global burp_command_bot, burp_scanner_bot, active_bots

    # Store old command bot for later
    old_command_bot = burp_command_bot

    # Get available command candidates (excluding current scanner bot)
    available_cmd_bots = [bot for bot in burp_command_candidates if bot in active_bots and bot != burp_scanner_bot]

    # Get available scanner candidates (excluding current command bot)
    available_scan_bots = [bot for bot in burp_scanner_candidates if bot in active_bots and bot != burp_command_bot]

    # If we have available bots, rotate roles
    if available_cmd_bots and available_scan_bots:
        # If there's a current command bot, move to next in rotation
        if burp_command_bot in available_cmd_bots:
            idx = available_cmd_bots.index(burp_command_bot)
            burp_command_bot = available_cmd_bots[(idx + 1) % len(available_cmd_bots)]
        else:
            burp_command_bot = available_cmd_bots[0]

        # If there's a current scanner bot, move to next in rotation
        if burp_scanner_bot in available_scan_bots:
            idx = available_scan_bots.index(burp_scanner_bot)
            burp_scanner_bot = available_scan_bots[(idx + 1) % len(available_scan_bots)]
        else:
            burp_scanner_bot = available_scan_bots[0]

        log.info("[BURP_ROTATION] New roles - Command: %s, Scanner: %s", burp_command_bot, burp_scanner_bot)

        # If command bot changed, transfer tasks to the new bot
        if old_command_bot != burp_command_bot:
            # Use bot_instances dictionary instead of discord.client._clients
            # Reset the _burp_started flag on the old bot if it exists
            if old_command_bot in bot_instances:
                old_bot = bot_instances[old_command_bot]
                # Stop the task loop if it exists
                if hasattr(old_bot, '_burp_loop') and old_bot._burp_loop.is_running():
                    old_bot._burp_loop.cancel()
                    log.info("[%s] BURP command task loop stopped", old_command_bot)
                old_bot._burp_started = False
                log.info("[%s] BURP command tasks detached from old bot", old_command_bot)

            # Attach tasks to the new command bot
            if burp_command_bot in bot_instances:
                new_bot = bot_instances[burp_command_bot]
                # Reset the flag first to ensure tasks are attached
                new_bot._burp_started = False
                _attach_burp_tasks(new_bot)
                log.info("[%s] BURP command tasks transferred to new bot", burp_command_bot)
    else:
        log.warning("[BURP_ROTATION] Not enough available bots for rotation")

def initialize_roles_if_needed():
    """Initialize roles if they haven't been set yet."""
    global tt_command_bot, tt_scanner_bot, burp_command_bot, burp_scanner_bot, active_bots

    # Initialize TT roles if needed
    if (tt_command_bot is None or tt_scanner_bot is None) and len(active_bots) >= 2:
        available_cmd_bots = [bot for bot in tt_command_candidates if bot in active_bots]
        available_scan_bots = [bot for bot in tt_scanner_candidates if bot in active_bots]

        if available_cmd_bots and available_scan_bots:
            tt_command_bot = available_cmd_bots[0]
            tt_scanner_bot = available_scan_bots[0]
            log.info("[INIT_ROLES] TT roles initialized - Command: %s, Scanner: %s (active_bots: %s)", 
                    tt_command_bot, tt_scanner_bot, active_bots)

    # Initialize BURP roles if needed
    if (burp_command_bot is None or burp_scanner_bot is None) and len(active_bots) >= 2:
        available_cmd_bots = [bot for bot in burp_command_candidates if bot in active_bots]
        available_scan_bots = [bot for bot in burp_scanner_candidates if bot in active_bots]

        if available_cmd_bots and available_scan_bots:
            burp_command_bot = available_cmd_bots[0]
            burp_scanner_bot = available_scan_bots[0]
            log.info("[INIT_ROLES] BURP roles initialized - Command: %s, Scanner: %s (active_bots: %s)", 
                    burp_command_bot, burp_scanner_bot, active_bots)

# ── BOT FACTORY ───────────────────────────────────────────────────────────
def make_bot(label: str) -> commands.Bot:
    bot = commands.Bot(command_prefix="!", self_bot=True)
    bot.label = label

    # Add bot to bot_instances dictionary
    global bot_instances
    bot_instances[label] = bot

    @bot.event
    async def on_ready():
        global active_bots, tt_command_bot, burp_command_bot
        log.info("[%s] ready as %s", label, bot.user)
        update_hb()                       # first heartbeat

        # Add this bot to active bots
        active_bots.add(label)

        # Initialize roles if needed
        initialize_roles_if_needed()

        # Schedule a task to check and attach tasks after a short delay
        # This ensures roles are initialized before we check if this bot should attach tasks
        asyncio.create_task(check_and_attach_tasks(bot, label))

    async def check_and_attach_tasks(bot, label, max_retries=5, retry_delay=1.0):
        """Check if this bot should attach tasks and do so if needed.
        Retry a few times to ensure roles are initialized."""
        global tt_command_bot, burp_command_bot

        for retry in range(max_retries):
            # Initialize roles again in case they weren't initialized yet
            initialize_roles_if_needed()

            # Check if roles are assigned
            if tt_command_bot is None or burp_command_bot is None:
                log.debug("[%s] Roles not fully initialized yet, retry %d/%d (tt_command_bot=%s, burp_command_bot=%s)", 
                         label, retry + 1, max_retries, tt_command_bot, burp_command_bot)
                await asyncio.sleep(retry_delay)
                continue

            # Attach TT tasks if this bot is the current TT command bot
            if label == tt_command_bot and not getattr(bot, "_tt_started", False):
                _attach_tt_tasks(bot)
                log.info("[%s] selected for TT command tasks (retry %d)", label, retry)

            # Attach BURP tasks if this bot is the current BURP command bot
            if label == burp_command_bot and not getattr(bot, "_burp_started", False):
                _attach_burp_tasks(bot)
                log.info("[%s] selected for BURP command tasks (retry %d)", label, retry)

            # If we got here, we've checked and potentially attached tasks
            log.debug("[%s] Successfully checked and attached tasks (retry %d)", label, retry)
            return

        # If we've exhausted retries and roles still aren't initialized
        if tt_command_bot is None or burp_command_bot is None:
            log.warning("[%s] Failed to initialize roles after %d retries", 
                       label, max_retries)
        else:
            # Final check in case roles were initialized on the last retry
            if label == tt_command_bot and not getattr(bot, "_tt_started", False):
                _attach_tt_tasks(bot)
                log.info("[%s] selected for TT command tasks (final check)", label)

            if label == burp_command_bot and not getattr(bot, "_burp_started", False):
                _attach_burp_tasks(bot)
                log.info("[%s] selected for BURP command tasks (final check)", label)

    # ── Handlers for scanning bots ──────────────────────────────────────────────
    # Check if this bot is assigned to scanner role for either TT or BURP
    if True:  # We'll check the role inside the event handlers

        @bot.event
        async def on_message_edit(_, after):
            if (
                    isMonitoringBurp
                    and label == burp_scanner_bot  # Only process if this bot is the burp scanner
                    and after.author.id == RICK_APP_ID
                    and after.channel.id == BURP_CH_ID
                    and after.embeds
            ):
                desc = after.embeds[0].description or ""
                for line in desc.splitlines():
                    if "Δ" not in line:
                        continue

                    # extract URL
                    url_m = re.search(r"https?://\S+", line)
                    if not url_m:
                        log.debug("Skip embed line (no URL)")
                        continue
                    raw = re.sub(r"[^A-Za-z0-9x]+$", "", url_m.group(0).rstrip("/"))

                    # extract token
                    tok_m = TOKEN_RE.search(raw)
                    if not tok_m:
                        log.debug("Skip URL (no token): %s", raw)
                        continue
                    token = tok_m.group(1)

                    # extract percentage
                    pct_m = re.search(r"Δ\s*([-.\d]+)%", line)
                    if not pct_m:
                        log.debug("Skip embed line (no Δ%%): %s", line)
                        continue
                    pct = float(pct_m.group(1))

                    # ——— NEW LOGIC ———
                    # If it's non-negative and we've already seen it, skip.
                    if pct >= 0 and token in burp_cycle_processed:
                        log.debug(
                            "Skip token %s — already processed non-negative Δ (%.2f%%)",
                            token, pct
                        )
                        continue

                    # Otherwise, we should copy it:
                    #  • negative Δ: always
                    #  • new token with Δ ≥ 0: first time
                    burp_cycle_processed.add(token)

                    if pct < 0:
                        log.info("Burping token (negative Δ): %s (%.1f%%)", token, pct)
                    else:
                        log.info("Burping new token: %s (%.1f%%)", token, pct)

                    # Add variation between 1 and 15 seconds
                    await asyncio.sleep(random.uniform(1, 15))
                    await bot.get_channel(GL_CH_ID).send(token)
            # — TT embed tweets —
            if (
                    isMonitoringTT 
                    and label == tt_scanner_bot  # Only process if this bot is the tt scanner
                    and after.author.id == RICK_APP_ID
                    and after.channel.id == CMD_CH
            ):
                if after.id in processed_tt_embeds:
                    log.debug("Skip embed %s — duplicate", after.id)
                    return None
                processed_tt_embeds.add(after.id)

                for line in (after.embeds[0].description or "").splitlines():
                    m = TW_URL_RE.search(line)
                    if not m:
                        continue
                    url = m.group(1)
                    if url in processed_tweets:
                        log.debug("Skip tweet duplicate: %s", url)
                        continue
                    processed_tweets.add(url)
                    log.info("[%s] Forwarding tweet: %s", label, url)
                    # Add variation between 1 and 15 seconds
                    await asyncio.sleep(random.uniform(10, 35))
                    await bot.get_channel(X_CH_ID).send(url)
                return None
            return None

        @bot.event
        async def on_message(msg: discord.Message):
            global isMonitoringTT, isMonitoringBurp

            # ignore self
            if msg.author.id == bot.user.id:
                return None

            txt = msg.content.strip().lower()

            # — TT controls —
            if msg.channel.id == TT_CH_ID and msg.author.id in ALLOWED_AUTHORS:
                if txt == "tt start":
                    isMonitoringTT = True
                    # Restart TT tasks on the current command bot
                    if label == tt_command_bot:
                        # Reset the flag to ensure tasks are attached
                        bot._tt_started = False
                        _attach_tt_tasks(bot)
                        log.info("[%s] TT tasks restarted after 'tt start' command", label)
                    return await msg.channel.send("TT on")
                if txt == "tt stop":
                    isMonitoringTT = False
                    return await msg.channel.send("TT off")
                if txt.startswith("tt config"):
                    _, _, cd, dmin, dmax = txt.split()[:5]
                    globals().update(
                        COMMAND_DELAY=int(cd),
                        COPY_DELAY_MIN=int(dmin),
                        COPY_DELAY_MAX=int(dmax),
                    )
                    log.info("TT config: %s %s-%s", cd, dmin, dmax)
                    return await msg.channel.send("TT config updated")

            # — Burp controls —
            if msg.channel.id == BURP_CH_ID and msg.author.id in ALLOWED_AUTHORS:
                if txt == "burp start":
                    isMonitoringBurp = True
                    # Restart BURP tasks on the current command bot
                    if label == burp_command_bot:
                        # Reset the flag to ensure tasks are attached
                        bot._burp_started = False
                        _attach_burp_tasks(bot)
                        log.info("[%s] BURP tasks restarted after 'burp start' command", label)
                    return await msg.channel.send("Burp on")
                if txt == "burp stop":
                    isMonitoringBurp = False
                    return await msg.channel.send("Burp off")
                if txt.startswith("burp config"):
                    globals().update(BURP_COOLDOWN=int(txt.split()[2]))
                    log.info("Burp cooldown: %s", BURP_COOLDOWN)
                    return await msg.channel.send("Burp cooldown updated")

            # — New Burp‐forwarding logic on fresh messages —
            if (
                    isMonitoringBurp
                    and label == burp_scanner_bot  # Only process if this bot is the burp scanner
                    and msg.channel.id == BURP_CH_ID
                    and msg.author.id in ALLOWED_AUTHORS
                    and msg.embeds
            ):
                desc = msg.embeds[0].description or ""
                for line in desc.splitlines():
                    if "Δ" not in line:
                        continue

                    # extract URL
                    url_m = re.search(r"https?://\S+", line)
                    if not url_m:
                        log.debug("Skip embed line (no URL)")
                        continue
                    raw = re.sub(r"[^A-Za-z0-9x]+$", "", url_m.group(0).rstrip("/"))

                    # extract token
                    tok_m = TOKEN_RE.search(raw)
                    if not tok_m:
                        log.debug("Skip URL (no token): %s", raw)
                        continue
                    token = tok_m.group(1)

                    # extract percentage
                    pct_m = re.search(r"Δ\s*([-.\d]+)%", line)
                    if not pct_m:
                        log.debug("Skip embed line (no Δ%%): %s", line)
                        continue
                    pct = float(pct_m.group(1))

                    # skip repeats of non-negative Δ
                    if pct >= 0 and token in burp_cycle_processed:
                        log.debug(
                            "Skip token %s — already processed non-negative Δ (%.2f%%)",
                            token, pct
                        )
                        continue

                    # otherwise, forward
                    burp_cycle_processed.add(token)
                    if pct < 0:
                        log.info("Burping token (negative Δ): %s (%.1f%%)", token, pct)
                    else:
                        log.info("Burping new token: %s (%.1f%%)", token, pct)

                    # Add variation between 1 and 15 seconds
                    await asyncio.sleep(random.uniform(1, 15))
                    await bot.get_channel(GL_CH_ID).send(token)

                return None

            # — TT embed tweets —
            if (
                    isMonitoringTT 
                    and label == tt_scanner_bot  # Only process if this bot is the tt scanner
                    and msg.author.id == RICK_APP_ID
                    and msg.channel.id == CMD_CH 
                    and msg.embeds
            ):
                print(msg)
                if msg.id in processed_tt_embeds:
                    log.debug("Skip embed %s — duplicate", msg.id)
                    return None
                processed_tt_embeds.add(msg.id)

                for line in (msg.embeds[0].description or "").splitlines():
                    m = TW_URL_RE.search(line)
                    if not m:
                        continue
                    url = m.group(1)
                    if url in processed_tweets:
                        log.debug("Skip tweet duplicate: %s", url)
                        continue
                    processed_tweets.add(url)
                    log.info("Forwarding tweet: %s", url)
                    # Add variation between 1 and 15 seconds
                    await asyncio.sleep(random.uniform(10, 35))
                    await bot.get_channel(X_CH_ID).send(url)
                return None

            # — Raw tweets —
            if msg.channel.id == TT_CH_ID and not msg.author.bot and label == tt_scanner_bot:  # Only process if this bot is the tt scanner
                for m in TW_URL_RE.finditer(msg.content):
                    url = m.group(1)
                    if url in processed_tweets:
                        log.debug("Skip tweet duplicate: %s", url)
                        continue
                    processed_tweets.add(url)

                    dest = CALL_CH_ID if re.search(r"\b0x[a-fA-F0-9]{40}\b", msg.content) else X_CH_ID
                    cname = "CALL" if dest == CALL_CH_ID else "X"
                    log.info("Forwarding tweet to %s: %s", cname, url)

                    # Add variation between 10 and 35 seconds
                    await asyncio.sleep(random.uniform(10, 35))
                    await bot.get_channel(dest).send(url)
                    return None
                return None
            return None
    return bot

# ── TASKS & RECOVERY ──────────────────────────────────────────────────────
def _recreate_http(bot: commands.Bot):
    """Helper function to recreate the HTTP client."""
    async def recreate_http():
        old, token = bot.http, bot.http.token
        connector  = None if getattr(old, "connector", None) in (None, MISSING) else old.connector
        proxy, proxy_auth = getattr(old, "proxy", None), getattr(old, "proxy_auth", None)
        try: await old.close()
        except Exception: pass

        # Initialize HTTPClient without the loop parameter as it's no longer accepted
        new_http = HTTPClient(proxy=proxy, proxy_auth=proxy_auth)
        if connector is not None:
            new_http.connector = connector
        await new_http.static_login(token)

        bot.http = new_http
        bot._connection.http = new_http
        if hasattr(bot, "_state"):
            bot._state.http = new_http

    return recreate_http

def _attach_tt_tasks(bot: commands.Bot):
    """Attach trending topics tasks to the bot."""
    bot._tt_started = True
    global last_trending_time
    log.info("[%s] Attaching TT tasks - loop will start momentarily", bot.label)

    recreate_http = _recreate_http(bot)
    tt_fails = 0
    # Store the current interval and variation
    current_interval = random.choice([180, 300, 480])
    current_variation = random.uniform(0.1, 2.0)

    @tasks.loop(seconds=1)  # We'll handle the interval manually
    async def tt_loop():
        nonlocal tt_fails, current_interval, current_variation
        global last_trending_time
        try:
            # Check if this bot is still the command bot and monitoring is active
            if not isMonitoringTT or bot.is_closed() or not getattr(bot, "_tt_started", False) or tt_command_bot != bot.label:
                return

            current_time = time.time()
            # Log current values for debugging
            log.debug("[TT_LOOP] Debug values: last_trending_time=%.2f, current_time=%.2f, diff=%.2f, threshold=%d+%.2f",
                     last_trending_time, current_time, current_time - last_trending_time, current_interval, current_variation)

            if current_time - last_trending_time < COMMAND_DELAY:
                log.debug("[TT_LOOP] Skipping due to COMMAND_DELAY: diff=%.2f < %d",
                         current_time - last_trending_time, COMMAND_DELAY)
                return

            # If this is the first run or enough time has passed since last run
            time_diff = current_time - last_trending_time
            threshold = current_interval + current_variation
            if last_trending_time == 0 or time_diff >= threshold:
                log.debug("[TT_LOOP] Condition met: last_trending_time=%s, time_diff=%.2f, threshold=%.2f",
                         "0" if last_trending_time == 0 else "%.2f" % last_trending_time, time_diff, threshold)
                # Execute the command
                ch = await bot.fetch_channel(CMD_CH)
                cmd = discord.utils.get(await safe_fetch_commands(ch), name="tt")
                if cmd:
                    # Log the interval that was used
                    log.info("[TT_LOOP] firing /tt in #%s with interval %ds+%.2fs", ch.name, current_interval, current_variation)
                    result = await safe_command_call(cmd, ch, ch.guild)
                    #await bot.get_channel(TT_CH_ID).send("tt test")

                    # Only update state if the command was actually executed (not discarded)
                    if result is not None:
                        # Update the last trending time
                        last_trending_time = current_time

                        # Select a new interval and variation for the next run
                        current_interval = random.choice([180, 300, 480])
                        current_variation = random.uniform(0.1, 2.0)  # Add 0.1 to 2.0 seconds of variation

                        # Rotate TT roles after successful command execution
                        rotate_tt_roles()
                        log.info("[TT_LOOP] Rotated TT roles after command execution")

                        tt_fails = 0
                        update_hb()                                   # successful loop
                    else:
                        log.info("[TT_LOOP] Command was discarded due to cooldown, not updating state")
            else:
                log.debug("[TT_LOOP] Condition not met: time_diff=%.2f < threshold=%.2f, waiting...",
                         time_diff, threshold)
        except NET_ERR as e:
            tt_fails += 1
            log.warning("[TT_LOOP] REST %r — recreate HTTP (%s/%s)", e, tt_fails, FAIL_THRESHOLD)
            await recreate_http()
            if tt_fails >= FAIL_THRESHOLD:
                log.critical("[TT_LOOP] %s fails — hard exit", FAIL_THRESHOLD)
                os._exit(1)

    # Store the task loop in the bot instance so it can be accessed later
    bot._tt_loop = tt_loop
    tt_loop.start()

def _attach_burp_tasks(bot: commands.Bot):
    """Attach burp tasks to the bot."""
    bot._burp_started = True
    global last_burp_time
    log.info("[%s] Attaching BURP tasks - loop will start momentarily", bot.label)

    recreate_http = _recreate_http(bot)
    burp_fails = 0
    # Store the current variation
    current_variation = random.uniform(5.0, 30.0)

    @tasks.loop(seconds=1)  # We'll handle the interval manually
    async def burp_loop():
        nonlocal burp_fails, current_variation
        global last_burp_time
        try:
            # Check if this bot is still the command bot and monitoring is active
            if not isMonitoringBurp or bot.is_closed() or not getattr(bot, "_burp_started", False) or burp_command_bot != bot.label:
                return

            current_time = time.time()
            # Log current values for debugging
            log.debug("[BURP_LOOP] Debug values: last_burp_time=%.2f, current_time=%.2f, diff=%.2f, threshold=%d+%.2f",
                     last_burp_time, current_time, current_time - last_burp_time, BURP_COOLDOWN, current_variation)

            if current_time - last_burp_time < BURP_COOLDOWN:
                log.debug("[BURP_LOOP] Skipping due to BURP_COOLDOWN: diff=%.2f < %d",
                         current_time - last_burp_time, BURP_COOLDOWN)
                return

            # Check if enough time has passed with the current variation
            time_diff = current_time - last_burp_time
            threshold = BURP_COOLDOWN + current_variation
            if last_burp_time == 0 or time_diff >= threshold:
                log.debug("[BURP_LOOP] Condition met: last_burp_time=%s, time_diff=%.2f, threshold=%.2f",
                         "0" if last_burp_time == 0 else "%.2f" % last_burp_time, time_diff, threshold)
                # Execute the command
                ch = await bot.fetch_channel(BURP_CH_ID)
                cmd = discord.utils.get(await safe_fetch_commands(ch), name="burp")
                if cmd:
                    # Log the cooldown that was used
                    log.info("[BURP_LOOP] firing /burp in #%s with cooldown %ds+%.2fs", ch.name, BURP_COOLDOWN, current_variation)
                    result = await safe_command_call(cmd, ch, ch.guild)
                    #await bot.get_channel(BURP_CH_ID).send("burp test")

                    # Only update state if the command was actually executed (not discarded)
                    if result is not None:
                        # Update the last burp time
                        last_burp_time = current_time

                        # Select a new variation for the next run
                        current_variation = random.uniform(5.0, 30.0)  # Add 5 to 30 seconds of variation

                        # Rotate BURP roles after successful command execution
                        rotate_burp_roles()
                        log.info("[BURP_LOOP] Rotated BURP roles after command execution")

                        burp_fails = 0
                        update_hb()                                   # successful loop
                    else:
                        log.info("[BURP_LOOP] Command was discarded due to cooldown, not updating state")
            else:
                log.debug("[BURP_LOOP] Condition not met: time_diff=%.2f < threshold=%.2f, waiting...",
                         time_diff, threshold)
        except NET_ERR as e:
            burp_fails += 1
            log.warning("[BURP_LOOP] REST %r — recreate HTTP (%s/%s)", e, burp_fails, FAIL_THRESHOLD)
            await recreate_http()
            if burp_fails >= FAIL_THRESHOLD:
                log.critical("[BURP_LOOP] %s fails — hard exit", FAIL_THRESHOLD)
                os._exit(1)

    # Store the task loop in the bot instance so it can be accessed later
    bot._burp_loop = burp_loop
    burp_loop.start()

# ── RUNNER loop ───────────────────────────────────────────────────────────
async def runner(token, label):
    while True:
        bot = make_bot(label)
        try:
            await bot.login(token)
            log.info("[%s] login OK", label)
            await bot.connect(reconnect=True)
        except (*NET_ERR, Exception) as e:
            log.warning("[%s] error %r — restart in 5 s", label, e)
        finally:
            # Remove this bot from active bots and bot_instances
            global active_bots, tt_command_bot, tt_scanner_bot, burp_command_bot, burp_scanner_bot, bot_instances

            if label in active_bots:
                active_bots.remove(label)
                log.info("[%s] removed from active bots", label)

            # Remove from bot_instances dictionary
            if label in bot_instances:
                # Stop any running task loops before removing the bot
                bot_instance = bot_instances[label]

                # Stop TT task loop if it exists and is running
                if hasattr(bot_instance, '_tt_loop') and bot_instance._tt_loop.is_running():
                    bot_instance._tt_loop.cancel()
                    log.info("[%s] TT command task loop stopped on disconnect", label)

                # Stop BURP task loop if it exists and is running
                if hasattr(bot_instance, '_burp_loop') and bot_instance._burp_loop.is_running():
                    bot_instance._burp_loop.cancel()
                    log.info("[%s] BURP command task loop stopped on disconnect", label)

                del bot_instances[label]

                # If this bot was assigned to any role, we need to reassign roles
                roles_changed = False

                if tt_command_bot == label:
                    tt_command_bot = None
                    roles_changed = True
                    log.info("[%s] was TT command bot, role now unassigned", label)

                if tt_scanner_bot == label:
                    tt_scanner_bot = None
                    roles_changed = True
                    log.info("[%s] was TT scanner bot, role now unassigned", label)

                if burp_command_bot == label:
                    burp_command_bot = None
                    roles_changed = True
                    log.info("[%s] was BURP command bot, role now unassigned", label)

                if burp_scanner_bot == label:
                    burp_scanner_bot = None
                    roles_changed = True
                    log.info("[%s] was BURP scanner bot, role now unassigned", label)

                # If roles changed, try to reassign them
                if roles_changed:
                    initialize_roles_if_needed()

            try: await bot.close()
            except Exception: pass
            await asyncio.sleep(5)

async def main():
    await asyncio.gather(
        runner(TOK_TRIBEIQ, "TRIBEIQ"),
        runner(TOK_EYESINTHEHOOK, "EYESINTHEHOOK"),
        runner(TOK_HOODNARRATOR, "HOODNARRATOR"),
        runner(TOK_READYTOSPY, "READYTOSPY"),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Ctrl-C — exiting")
