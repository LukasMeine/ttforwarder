# bot.py  – discord.py-self – rebuild-on-failure – burp-token fix
import os, re, random, asyncio, time, sys, socket, ssl
from dotenv import load_dotenv

import discord                       # discord.py-self
from discord import HTTPException
from discord.ext import commands, tasks
from discord.http import HTTPClient
from curl_cffi.curl import CurlError
import aiohttp

load_dotenv()

# ── CONFIG ──────────────────────────────────────────────
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

# ── GLOBAL STATE ───────────────────────────────────────
isMonitoringTT  = True
isMonitoringBurp = True
processed_tt_embeds, processed_tweets = set(), set()
burp_cycle_processed = set()
last_trending_time, last_burp_time = 0.0, 0.0

NET_ERR = (
    discord.InvalidData, HTTPException, CurlError,
    aiohttp.ClientError, OSError, socket.gaierror, ssl.SSLError,
)

# clean URL / token regex
TW_URL_RE = re.compile(r"(https?://twitter\.com/[^\s\)\]]+)")
TOKEN_RE  = re.compile(r"(0x[a-fA-F0-9]{40}|[A-Za-z0-9]+)$")

# ── UTILITIES ───────────────────────────────────────────
async def safe_fetch_commands(ch):
    while True:
        try:
            return await ch.application_commands()
        except ValueError:
            await asyncio.sleep(1)

def make_bot(label: str) -> commands.Bot:
    bot = commands.Bot(command_prefix="!", self_bot=True)
    bot.label = label

    # ── READY ──────────────────────────────────────────
    @bot.event
    async def on_ready():
        print(f"[{label}] ready as {bot.user}")
        if label == "XACANNA" and not getattr(bot, "_tt_started", False):
            _attach_tt_tasks(bot)

    # ── CHADBRICK-only handlers ───────────────────────
    if label == "CHADBRICK":

        @bot.event
        async def on_message_edit(_, after):
            if (
                isMonitoringBurp and after.author.id == RICK_APP_ID
                and after.channel.id == BURP_CH_ID and after.embeds
            ):
                for line in (after.embeds[0].description or "").splitlines():
                    if "Δ" not in line:
                        continue
                    url_m = re.search(r"https?://\S+", line)
                    if not url_m:
                        continue

                    raw = url_m.group(0).rstrip("/")                       # drop trailing '/'
                    raw = re.sub(r"[^A-Za-z0-9x]+$", "", raw)              # drop trailing symbols
                    tok_m = TOKEN_RE.search(raw)
                    if not tok_m:
                        continue
                    token = tok_m.group(1)

                    if token in burp_cycle_processed:
                        continue

                    pct = float(re.search(r"Δ\s*([-.\d]+)%", line).group(1))
                    if pct < 0:
                        continue

                    burp_cycle_processed.add(token)
                    print(f"Burping token: {token} ({pct}%)")
                    await asyncio.sleep(random.uniform(COPY_DELAY_MIN, COPY_DELAY_MAX))
                    await bot.get_channel(GL_CH_ID).send(token)

        @bot.event
        async def on_message(msg: discord.Message):
            global isMonitoringTT, isMonitoringBurp
            if msg.author.id == bot.user.id:
                return

            txt = msg.content.strip().lower()

            # TT control
            if msg.channel.id == TT_CH_ID:
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
                    return await msg.channel.send("TT config updated")

            # Burp control
            if msg.channel.id == BURP_CH_ID:
                if txt == "burp start":
                    isMonitoringBurp = True
                    return await msg.channel.send("Burp on")
                if txt == "burp stop":
                    isMonitoringBurp = False
                    return await msg.channel.send("Burp off")
                if txt.startswith("burp config"):
                    globals().update(BURP_COOLDOWN=int(txt.split()[2]))
                    return await msg.channel.send("Burp cooldown updated")

            # TT-embed forwarding
            if (
                isMonitoringTT and msg.author.id == RICK_APP_ID
                and msg.channel.id == CMD_CH and msg.embeds
            ):
                if msg.id in processed_tt_embeds:
                    return
                processed_tt_embeds.add(msg.id)
                for line in (msg.embeds[0].description or "").splitlines():
                    m = TW_URL_RE.search(line)
                    if not m:
                        continue
                    url = m.group(1)
                    if url in processed_tweets:
                        continue
                    processed_tweets.add(url)
                    print("Forwarding URL:", url)
                    await asyncio.sleep(random.uniform(COPY_DELAY_MIN, COPY_DELAY_MAX))
                    await bot.get_channel(X_CH_ID).send(url)
                return

            # Raw tweets
            if msg.channel.id == TT_CH_ID and not msg.author.bot:
                for m in TW_URL_RE.finditer(msg.content):
                    url = m.group(1)
                    if url in processed_tweets:
                        continue
                    processed_tweets.add(url)
                    dest = CALL_CH_ID if re.search(r"\b0x[a-fA-F0-9]{40}\b", msg.content) else X_CH_ID
                    print("Forwarding URL:", url)
                    await asyncio.sleep(random.uniform(COPY_DELAY_MIN, COPY_DELAY_MAX))
                    await bot.get_channel(dest).send(url)

    return bot

def _attach_tt_tasks(bot: commands.Bot):
    bot._tt_started = True
    global last_trending_time, last_burp_time

    async def recreate_http():
        old = bot.http
        try: await old.close()
        except Exception: pass
        bot.http = HTTPClient(old.token)

    @tasks.loop(seconds=INTERVAL_SECONDS)
    async def tt_loop():
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
                print(f"[TT_LOOP] fired /tt in #{ch.name}")
        except NET_ERR as e:
            print(f"[TT_LOOP] REST {e!r} – recreate HTTP")
            await recreate_http()

    @tasks.loop(seconds=BURP_INTERVAL)
    async def burp_loop():
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
                print(f"[BURP_LOOP] fired /burp in #{ch.name}")
        except NET_ERR as e:
            print(f"[BURP_LOOP] REST {e!r} – recreate HTTP")
            await recreate_http()

    tt_loop.start()
    burp_loop.start()

# ── RUNNER: create new bot on any gateway failure ───────
async def runner(token, label):
    while True:
        bot = make_bot(label)
        try:
            await bot.login(token)
            print(f"[{label}] login OK")
            await bot.connect(reconnect=True)
        except (*NET_ERR,) as e:
            print(f"[{label}] gateway/net {e!r} – rebuild in 5 s")
            try: await bot.close()
            except Exception: pass
            await asyncio.sleep(5)
        except discord.LoginFailure:
            print(f"[{label}] bad token – abort")
            sys.exit(1)

async def main():
    await asyncio.gather(
        runner(TOK_XACANNA, "XACANNA"),
        runner(TOK_CHADBRICK, "CHADBRICK"),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Ctrl-C – exiting")
