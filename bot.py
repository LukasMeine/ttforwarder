# bot.py — discord.py-self forwarder
# • Focused logging (INFO / DEBUG)
# • Resilient HTTP rebuild
# • Hard exit after N consecutive REST failures (Docker restarts container)
# • Heart-beat file updated only when loops succeed — Docker health-check
#   restarts container if loops hang

import os, re, random, asyncio, time, sys, socket, ssl, inspect, logging, pathlib, datetime
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
        HB_PATH.write_text(datetime.datetime.utcnow().isoformat())
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
TOK_XACANNA   = os.getenv("XACANNA_TOKEN")
TOK_CHADBRICK = os.getenv("CHADBRICK_TOKEN")
RICK_APP_ID   = int(os.getenv("RICK_APP_ID"))

CMD_CH     = int(os.getenv("COMMAND_CHANNEL_ID"))
TT_CH_ID   = int(os.getenv("TT_CHANNEL_ID"))
X_CH_ID    = int(os.getenv("X_CHANNEL_ID"))
CALL_CH_ID = int(os.getenv("CALL_CHANNEL_ID"))
BURP_CH_ID = int(os.getenv("BURP_CHANNEL_ID"))
GL_CH_ID   = int(os.getenv("GLOBAL_CHANNEL_ID"))

INTERVAL_SECONDS = int(os.getenv("MONITOR_INTERVAL", "60"))
BURP_INTERVAL    = int(os.getenv("BURP_INTERVAL", "60"))
COMMAND_DELAY    = int(os.getenv("COMMAND_DELAY", "15"))
COPY_DELAY_MIN   = int(os.getenv("COPY_DELAY_MIN", "3"))
COPY_DELAY_MAX   = int(os.getenv("COPY_DELAY_MAX", "5"))
BURP_COOLDOWN    = int(os.getenv("BURP_COOLDOWN", "600"))

FAIL_THRESHOLD   = 3   # after N consecutive REST failures → os._exit(1)

# ── STATE ────────────────────────────────────────────────────────────────
isMonitoringTT   = True
isMonitoringBurp = True
processed_tt_embeds, processed_tweets = set(), set()
burp_cycle_processed = set()
last_trending_time, last_burp_time = 0.0, 0.0

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

async def safe_fetch_commands(ch):
    while True:
        try:
            return await ch.application_commands()
        except ValueError:
            await asyncio.sleep(1)

# ── BOT FACTORY ───────────────────────────────────────────────────────────
def make_bot(label: str) -> commands.Bot:
    bot = commands.Bot(command_prefix="!", self_bot=True)
    bot.label = label

    @bot.event
    async def on_ready():
        log.info("[%s] ready as %s", label, bot.user)
        update_hb()                       # first heartbeat
        if label == "XACANNA" and not getattr(bot, "_tt_started", False):
            _attach_tt_tasks(bot)

    # ── CHADBRICK handlers (burp + tweet forward) ──────────────────────
    if label == "CHADBRICK":

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

                    await asyncio.sleep(random.uniform(COPY_DELAY_MIN, COPY_DELAY_MAX))
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
                    await asyncio.sleep(random.uniform(COPY_DELAY_MIN, COPY_DELAY_MAX))
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

                    await asyncio.sleep(random.uniform(COPY_DELAY_MIN, COPY_DELAY_MAX))
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
                    await asyncio.sleep(random.uniform(COPY_DELAY_MIN, COPY_DELAY_MAX))
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

                    await asyncio.sleep(random.uniform(COPY_DELAY_MIN, COPY_DELAY_MAX))
                    await bot.get_channel(dest).send(url)
                    return None
                return None
            return None
    return bot

# ── TASKS & RECOVERY ──────────────────────────────────────────────────────
def _attach_tt_tasks(bot: commands.Bot):
    bot._tt_started = True
    global last_trending_time, last_burp_time

    async def recreate_http():
        old, token = bot.http, bot.http.token
        connector  = None if getattr(old, "connector", None) in (None, MISSING) else old.connector
        proxy, proxy_auth = getattr(old, "proxy", None), getattr(old, "proxy_auth", None)
        try: await old.close()
        except Exception: pass

        has_loop = "loop" in inspect.signature(HTTPClient.__init__).parameters
        new_http = HTTPClient(*(bot.loop,) if has_loop else tuple(),
                              connector=connector, proxy=proxy, proxy_auth=proxy_auth)
        await new_http.static_login(token)

        bot.http = new_http
        bot._connection.http = new_http
        if hasattr(bot, "_state"):
            bot._state.http = new_http

    tt_fails = burp_fails = 0

    @tasks.loop(seconds=INTERVAL_SECONDS)
    async def tt_loop():
        nonlocal tt_fails
        global last_trending_time
        try:
            if not isMonitoringTT or bot.is_closed():
                return
            if time.time() - last_trending_time < COMMAND_DELAY:
                return
            last_trending_time = time.time()

            ch = await bot.fetch_channel(CMD_CH)
            cmd = discord.utils.get(await safe_fetch_commands(ch), name="tt")
            if cmd:
                await cmd.__call__(channel=ch, guild=ch.guild)
                log.info("[TT_LOOP] fired /tt in #%s", ch.name)
            tt_fails = 0
            update_hb()                                   # successful loop
        except NET_ERR as e:
            tt_fails += 1
            log.warning("[TT_LOOP] REST %r — recreate HTTP (%s/%s)", e, tt_fails, FAIL_THRESHOLD)
            await recreate_http()
            if tt_fails >= FAIL_THRESHOLD:
                log.critical("[TT_LOOP] %s fails — hard exit", FAIL_THRESHOLD)
                os._exit(1)

    @tasks.loop(seconds=BURP_INTERVAL)
    async def burp_loop():
        nonlocal burp_fails
        global last_burp_time
        try:
            if not isMonitoringBurp or bot.is_closed():
                return
            if time.time() - last_burp_time < BURP_COOLDOWN:
                return
            last_burp_time = time.time()

            ch = await bot.fetch_channel(BURP_CH_ID)
            cmd = discord.utils.get(await safe_fetch_commands(ch), name="burp")
            if cmd:
                await cmd.__call__(channel=ch, guild=ch.guild)
                log.info("[BURP_LOOP] fired /burp in #%s", ch.name)
            burp_fails = 0
            update_hb()                                   # successful loop
        except NET_ERR as e:
            burp_fails += 1
            log.warning("[BURP_LOOP] REST %r — recreate HTTP (%s/%s)", e, burp_fails, FAIL_THRESHOLD)
            await recreate_http()
            if burp_fails >= FAIL_THRESHOLD:
                log.critical("[BURP_LOOP] %s fails — hard exit", FAIL_THRESHOLD)
                os._exit(1)

    tt_loop.start()
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
        runner(TOK_XACANNA, "XACANNA"),
        runner(TOK_CHADBRICK, "CHADBRICK"),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Ctrl-C — exiting")
