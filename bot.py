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
from discord import HTTPException
from discord.ext import commands, tasks
from discord.http import HTTPClient
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
tt_bot_selected = None
burp_bot_selected = None

ALLOWED_AUTHORS = {
    219212900743512065, # Simple
    303754867044777985, # Dej
    979973170251567136, # Kit
}

NET_ERR = (
    discord.InvalidData, HTTPException, CurlError,
    aiohttp.ClientError, OSError, socket.gaierror, ssl.SSLError,
)

TW_URL_RE = re.compile(r"(https?://twitter\.com/[^\s\)\]]+)")
TOKEN_RE  = re.compile(r"(0x[a-fA-F0-9]{40}|[A-Za-z0-9]+)$")

async def safe_command_call(cmd, channel, guild, max_retries=3):
    """Safely call a Discord command with retry mechanism."""
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

# ── BOT FACTORY ───────────────────────────────────────────────────────────
def make_bot(label: str) -> commands.Bot:
    bot = commands.Bot(command_prefix="!", self_bot=True)
    bot.label = label

    @bot.event
    async def on_ready():
        global tt_bot_selected, burp_bot_selected
        log.info("[%s] ready as %s", label, bot.user)
        update_hb()                       # first heartbeat

        # Randomly select one bot for TT tasks
        if label in ["TRIBEIQ", "EYESINTHEHOOK"] and not getattr(bot, "_tt_started", False):
            if tt_bot_selected is None:
                # First bot to come online gets a 50% chance
                if random.random() < 0.5:
                    tt_bot_selected = label
                    _attach_tt_tasks(bot)
                    log.info("[%s] selected for TT tasks", label)
            elif tt_bot_selected is None and (label == "TRIBEIQ" or label == "EYESINTHEHOOK"):
                # If no bot was selected yet and this is the second bot, select it
                tt_bot_selected = label
                _attach_tt_tasks(bot)
                log.info("[%s] selected for TT tasks (default)", label)

        # Randomly select one bot for BURP tasks
        if label in ["HOODNARRATOR", "READYTOSPY"] and not getattr(bot, "_burp_started", False):
            if burp_bot_selected is None:
                # First bot to come online gets a 50% chance
                if random.random() < 0.5:
                    burp_bot_selected = label
                    bot._burp_started = True
                    _attach_burp_tasks(bot)
                    log.info("[%s] selected for BURP tasks", label)
            elif burp_bot_selected is None and (label == "HOODNARRATOR" or label == "READYTOSPY"):
                # If no bot was selected yet and this is the second bot, select it
                burp_bot_selected = label
                bot._burp_started = True
                _attach_burp_tasks(bot)
                log.info("[%s] selected for BURP tasks (default)", label)

    # ── CHADBRICK/HOODNARRATOR/READYTOSPY handlers (burp + tweet forward) ──────────────────────
    if label in ["HOODNARRATOR", "READYTOSPY"]:

        @bot.event
        async def on_message_edit(_, after):
            if (
                    isMonitoringBurp
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
            if isMonitoringTT and after.author.id == RICK_APP_ID and after.channel.id == CMD_CH:

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
                    log.info("Forwarding tweet: %s", url)
                    # Add variation between 1 and 15 seconds
                    await asyncio.sleep(random.uniform(1, 15))
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
                    isMonitoringTT and msg.author.id == RICK_APP_ID
                    and msg.channel.id == CMD_CH and msg.embeds
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
                    await asyncio.sleep(random.uniform(1, 15))
                    await bot.get_channel(X_CH_ID).send(url)
                return None

            # — Raw tweets —
            if msg.channel.id == TT_CH_ID and not msg.author.bot:
                for m in TW_URL_RE.finditer(msg.content):
                    url = m.group(1)
                    if url in processed_tweets:
                        log.debug("Skip tweet duplicate: %s", url)
                        continue
                    processed_tweets.add(url)

                    dest = CALL_CH_ID if re.search(r"\b0x[a-fA-F0-9]{40}\b", msg.content) else X_CH_ID
                    cname = "CALL" if dest == CALL_CH_ID else "X"
                    log.info("Forwarding tweet to %s: %s", cname, url)

                    # Add variation between 1 and 15 seconds
                    await asyncio.sleep(random.uniform(1, 15))
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

    recreate_http = _recreate_http(bot)
    tt_fails = 0
    # Store the current interval and variation
    current_interval = random.choice([60, 180, 300])
    current_variation = random.uniform(0.1, 2.0)

    @tasks.loop(seconds=1)  # We'll handle the interval manually
    async def tt_loop():
        nonlocal tt_fails, current_interval, current_variation
        global last_trending_time
        try:
            if not isMonitoringTT or bot.is_closed():
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
                    log.info("[TT_LOOP] fired /tt in #%s with interval %ds+%.2fs", ch.name, current_interval, current_variation)
                    await safe_command_call(cmd, ch, ch.guild)

                    # Update the last trending time
                    last_trending_time = current_time

                    # Select a new interval and variation for the next run
                    current_interval = random.choice([180, 300, 480])
                    current_variation = random.uniform(0.1, 2.0)  # Add 0.1 to 2.0 seconds of variation

                    tt_fails = 0
                    update_hb()                                   # successful loop
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

    tt_loop.start()

def _attach_burp_tasks(bot: commands.Bot):
    """Attach burp tasks to the bot."""
    bot._burp_started = True
    global last_burp_time

    recreate_http = _recreate_http(bot)
    burp_fails = 0
    # Store the current variation
    current_variation = random.uniform(5.0, 30.0)

    @tasks.loop(seconds=1)  # We'll handle the interval manually
    async def burp_loop():
        nonlocal burp_fails, current_variation
        global last_burp_time
        try:
            if not isMonitoringBurp or bot.is_closed():
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
                    log.info("[BURP_LOOP] fired /burp in #%s with cooldown %ds+%.2fs", ch.name, BURP_COOLDOWN, current_variation)
                    await safe_command_call(cmd, ch, ch.guild)

                    # Update the last burp time
                    last_burp_time = current_time

                    # Select a new variation for the next run
                    current_variation = random.uniform(5.0, 30.0)  # Add 5 to 30 seconds of variation

                    burp_fails = 0
                    update_hb()                                   # successful loop
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
