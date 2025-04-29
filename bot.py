# pip install -U discord.py-self python-dotenv
import os
import re
import random
import asyncio
import time
from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks

load_dotenv()

# ── CONFIG ────────────────────────────────────────────────
XACANNA_TOKEN      = os.getenv("XACANNA_TOKEN")
CHADBRICK_TOKEN    = os.getenv("CHADBRICK_TOKEN")
RICK_APP_ID        = int(os.getenv("RICK_APP_ID"))
DEJ_ID             = 303754867044777985

COMMAND_CHANNEL_ID = int(os.getenv("COMMAND_CHANNEL_ID"))
TT_CHANNEL_ID      = int(os.getenv("TT_CHANNEL_ID"))
X_CHANNEL_ID       = int(os.getenv("X_CHANNEL_ID"))
CALL_CHANNEL_ID    = int(os.getenv("CALL_CHANNEL_ID"))
BURP_CHANNEL_ID    = int(os.getenv("BURP_CHANNEL_ID"))
GLOBAL_CHANNEL_ID  = int(os.getenv("GLOBAL_CHANNEL_ID"))

INTERVAL_SECONDS   = int(os.getenv("MONITOR_INTERVAL", "60"))
BURP_INTERVAL      = int(os.getenv("BURP_INTERVAL", "60"))
COMMAND_DELAY      = int(os.getenv("COMMAND_DELAY", "15"))
COPY_DELAY_MIN     = int(os.getenv("COPY_DELAY_MIN", "3"))
COPY_DELAY_MAX     = int(os.getenv("COPY_DELAY_MAX", "5"))
BURP_COOLDOWN      = int(os.getenv("BURP_COOLDOWN", "600"))

# ── STATE & DEDUPE ────────────────────────────────────────
isMonitoringTT       = True
isMonitoringBurp     = True
processed_tt_embeds  = set()
processed_tweets     = set()
processed_burp_embeds= set()
burp_cycle_processed = set()
last_trending_time   = 0.0
last_burp_time       = 0.0

# ── INTENTS (dynamic for compat) ─────────────────────────
try:
    intents = discord.Intents.default()
    intents.message_content = True
except AttributeError:
    intents = None

# ── SELF-BOT INSTANCES ───────────────────────────────────
bot_kwargs = {"command_prefix": "!", "self_bot": True}
if intents:
    bot_kwargs["intents"] = intents

xacanna   = commands.Bot(**bot_kwargs)
chadbrick = commands.Bot(**bot_kwargs)

# placeholders to be filled in on_ready
cmd_ch_tt   = None
cmd_ch_burp = None
tt_cmd      = None
burp_cmd    = None

# ── UTILITY ───────────────────────────────────────────────
async def safe_fetch_commands(ch):
    while True:
        try:
            return await ch.application_commands()
        except ValueError as e:
            if "invalid literal for int()" in str(e):
                print("[RATE LIMIT] retrying fetch_commands in 1s…")
                await asyncio.sleep(1)
                continue
            raise

# ── XACANNA: schedule /tt & /burp ───────────────────────
@xacanna.event
async def on_ready():
    global cmd_ch_tt, cmd_ch_burp, tt_cmd, burp_cmd

    print(f"Xacanna ready as {xacanna.user}")

    cmd_ch_tt   = await xacanna.fetch_channel(COMMAND_CHANNEL_ID)
    cmd_ch_burp = await xacanna.fetch_channel(BURP_CHANNEL_ID)

    # fetch both command lists in parallel
    tt_task   = asyncio.create_task(safe_fetch_commands(cmd_ch_tt))
    burp_task = asyncio.create_task(safe_fetch_commands(cmd_ch_burp))
    tt_list, burp_list = await asyncio.gather(tt_task, burp_task)

    tt_cmd   = discord.utils.get(tt_list,   name="tt")
    burp_cmd = discord.utils.get(burp_list, name="burp")

    print("TT cmds :", [c.name for c in tt_list])
    print("Burp cmds:", [c.name for c in burp_list])

    if not tt_cmd or not burp_cmd:
        print("❌ couldn’t find /tt and/or /burp")
        return

    tt_loop.start()
    burp_loop.start()

@tasks.loop(seconds=INTERVAL_SECONDS)
async def tt_loop():
    global last_trending_time
    if not isMonitoringTT or tt_cmd is None:
        return
    now = time.time()
    if now - last_trending_time < COMMAND_DELAY:
        return
    last_trending_time = now
    try:
        await tt_cmd.__call__(channel=cmd_ch_tt, guild=cmd_ch_tt.guild)
        print(f"[TT_LOOP] fired /tt in #{cmd_ch_tt.name}")
    except Exception as e:
        print(f"[TT_LOOP ERROR] {e!r}")

@tasks.loop(seconds=BURP_INTERVAL)
async def burp_loop():
    global last_burp_time
    if not isMonitoringBurp or burp_cmd is None:
        return
    now = time.time()
    if now - last_burp_time < BURP_COOLDOWN:
        return
    last_burp_time = now
    try:
        await burp_cmd.__call__(channel=cmd_ch_burp, guild=cmd_ch_burp.guild)
        print(f"[BURP_LOOP] fired /burp in #{cmd_ch_burp.name}")
    except Exception as e:
        print(f"[BURP_LOOP ERROR] {e!r}")

# ── CHADBRICK: listen & forward ──────────────────────────
@chadbrick.event
async def on_ready():
    print(f"Chadbrick ready as {chadbrick.user}")

# just keep a Python set in memory
burp_cycle_processed: set[str] = set()

@chadbrick.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if not (
        isMonitoringBurp
        and after.author.id == RICK_APP_ID
        and after.channel.id == BURP_CHANNEL_ID
        and after.embeds
    ):
        return

    lines = [l for l in (after.embeds[0].description or "").splitlines() if l.strip()]
    stat_lines = [l for l in lines if "Δ" in l]

    for stat in stat_lines:
        url_m = re.search(r"https?://[^\)\s]+", stat)
        if not url_m:
            continue
        token = url_m.group(0).rstrip("/").split("/")[-1]

        # SKIP if we've already burped this token
        if token in burp_cycle_processed:
            continue

        # parse percentage (if you need it)
        pct_m = re.search(r"Δ\s*([-.\d]+)%", stat)
        pct = float(pct_m.group(1)) if pct_m else 0.0

        # your business rule: only burp if pct >= 0
        if pct < 0:
            continue

        # mark as processed
        burp_cycle_processed.add(token)

        print(f"Burping token: {token} ({pct}%)")
        await asyncio.sleep(random.uniform(COPY_DELAY_MIN, COPY_DELAY_MAX))
        await chadbrick.get_channel(GLOBAL_CHANNEL_ID).send(token)



@chadbrick.event
async def on_message(msg: discord.Message):
    global isMonitoringTT, isMonitoringBurp
    global COMMAND_DELAY, COPY_DELAY_MIN, COPY_DELAY_MAX, BURP_COOLDOWN
    global last_trending_time, last_burp_time

    if msg.author.id == chadbrick.user.id:
        return

    txt = msg.content.strip().lower()

    # TT control
    if msg.channel.id == TT_CHANNEL_ID:
        if txt == "tt start":
            isMonitoringTT = True
            return await msg.channel.send("TT monitoring started.")
        if txt == "tt stop":
            isMonitoringTT = False
            return await msg.channel.send("TT monitoring stopped.")
        if txt.startswith("tt config"):
            parts = txt.split()
            if len(parts) >= 5:
                COMMAND_DELAY   = int(parts[2])
                COPY_DELAY_MIN  = int(parts[3])
                COPY_DELAY_MAX  = int(parts[4])
                return await msg.channel.send(
                    f"TT delays set: parse {COMMAND_DELAY}s, copy {COPY_DELAY_MIN}-{COPY_DELAY_MAX}s"
                )

    # Burp control
    if msg.channel.id == BURP_CHANNEL_ID:
        if txt == "burp start":
            isMonitoringBurp = True
            return await msg.channel.send("Burp monitoring started.")
        if txt == "burp stop":
            isMonitoringBurp = False
            return await msg.channel.send("Burp monitoring stopped.")
        if txt.startswith("burp config"):
            parts = txt.split()
            if len(parts) >= 3:
                BURP_COOLDOWN = int(parts[2])
                return await msg.channel.send(f"Burp cooldown set to {BURP_COOLDOWN}s")

    # Handle /tt embeds
    if (isMonitoringTT
        and msg.author.id == RICK_APP_ID
        and msg.channel.id == COMMAND_CHANNEL_ID
        and msg.embeds):
        emb = msg.embeds[0]
        if msg.id not in processed_tt_embeds:
            now_ts = time.time()
            last_trending_time = now_ts
            processed_tt_embeds.add(msg.id)

            lines = [l for l in (emb.description or "").splitlines() if l.strip()]
            for line in lines:
                m = re.search(r"https?://twitter\.com/[^\s)]+", line)
                if not m:
                    continue
                url = m.group(0)
                if url in processed_tweets:
                    print("Skipping duplicate URL:", url)
                    continue
                processed_tweets.add(url)
                print("Forwarding URL:", url)
                await asyncio.sleep(random.uniform(COPY_DELAY_MIN, COPY_DELAY_MAX))
                await chadbrick.get_channel(X_CHANNEL_ID).send(url)
        return

    if not (isMonitoringBurp
            and msg.author.id == RICK_APP_ID
            and msg.channel.id == BURP_CHANNEL_ID):
        return

    print(msg.embeds[0])
    lines = [l for l in (msg.embeds[0].description or "").splitlines() if l.strip()]
    print(lines)
    # extract only lines containing Δ
    stat_lines = [l for l in lines if "Δ" in l]

    for stat in stat_lines:
        # pull the URL out of this same stat line
        url_m = re.search(r"https?://\S+", stat)
        if not url_m:
            continue
        full_url = url_m.group(0).rstrip(")")
        # capture final path segment of alphanumeric chars only
        tok_m = re.search(r"/([A-Za-z0-9]+)$", full_url)
        if not tok_m:
            continue
        token = tok_m.group(1)

        pct_m = re.search(r"Δ\s*([-.\d]+)%", stat)
        pct = float(pct_m.group(1)) if pct_m else 0.0

        if token in burp_cycle_processed and pct >= 0:
            continue

        burp_cycle_processed.add(token)
        print(f"Burping token: {token} ({pct}%)")
        await asyncio.sleep(random.uniform(COPY_DELAY_MIN, COPY_DELAY_MAX))
        await chadbrick.get_channel(GLOBAL_CHANNEL_ID).send(token)

    # Forward raw tweets
    if msg.channel.id == TT_CHANNEL_ID and not msg.author.bot:
        for m in re.finditer(r"https?://twitter\.com/[\w]+/status/\d+", msg.content):
            url = m.group(0)
            if url in processed_tweets:
                print("Skipping duplicate URL:", url)
                continue
            processed_tweets.add(url)
            print("Forwarding URL:", url)
            if (re.search(r"\b0x[a-fA-F0-9]{40}\b", msg.content)
                or re.search(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b", msg.content)):
                print("On-chain address → #call only")
                await chadbrick.get_channel(CALL_CHANNEL_ID).send(url)
            else:
                await asyncio.sleep(random.uniform(COPY_DELAY_MIN, COPY_DELAY_MAX))
                await chadbrick.get_channel(X_CHANNEL_ID).send(url)

# ── RUN BOTH BOTS ────────────────────────────────────────
async def main():
    await asyncio.gather(
        xacanna.start(XACANNA_TOKEN),
        chadbrick.start(CHADBRICK_TOKEN)
    )

asyncio.run(main())
