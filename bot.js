// discord-twitter-monitor.js
// Two Discord self-bots in one file:
// 1) Xacanna: periodically sends "/tt trending" commands
// 2) Chadbrick: listens for plain-text commands in TT and BURP channels, and forwards tweets & tokens

const { Client, GatewayIntentBits } = require('discord.js');
const axios = require('axios');
require('dotenv').config();

// =====================
// TOKENS & CHANNELS
// =====================
const XACANNA_TOKEN        = process.env.XACANNA_TOKEN;
const CHADBRICK_TOKEN= process.env.CHADBRICK_TOKEN;
const RICK_APP_ID          = process.env.RICK_APP_ID;

const COMMAND_CHANNEL_ID   = process.env.COMMAND_CHANNEL_ID; // where /tt replies appear
const TT_CHANNEL_ID        = process.env.TT_CHANNEL_ID;      // where users post tweets
const X_CHANNEL_ID         = process.env.X_CHANNEL_ID;       // where /tt URLs go
const CALL_CHANNEL_ID      = process.env.CALL_CHANNEL_ID;    // where CA URLs go

const BURP_CHANNEL_ID      = process.env.BURP_CHANNEL_ID;    // 1HLDB channel for /burp
const GLOBAL_CHANNEL_ID    = process.env.GLOBAL_CHANNEL_ID;  // where /burp tokens go

// =====================
// INTERVALS & DELAYS
// =====================
const INTERVAL_SECONDS     = parseInt(process.env.MONITOR_INTERVAL || '60', 10);
let commandDelay           = parseInt(process.env.COMMAND_DELAY   || '15', 10) * 1000;
let copyMinDelay           = parseInt(process.env.COPY_DELAY_MIN  || '3', 10)  * 1000;
let copyMaxDelay           = parseInt(process.env.COPY_DELAY_MAX  || '5', 10)  * 1000;
let BURP_COOLDOWN_MS       = 600 * 1000;  // 600s default

// =====================
// STATE & DEDUPE SETS
// =====================
let isMonitoringTT     = true;
let isMonitoringBurp   = true;

let processedEmbeds    = new Set();
let processedTweets    = new Set();        // dedupe URLs across TT
let processedBurpEmbeds= new Set();
let burpCycleProcessed = new Set();

let lastTrendingTime   = 0;
let lastBurpTime       = 0;

// =====================
// HELPERS
// =====================
const sleep = ms => new Promise(r => setTimeout(r, ms));
const randomDelay = (min, max) => Math.floor(Math.random()*(max-min+1)) + min;

async function sendMessageAs(content, token, channelId) {
  try {
    await axios.post(
        `https://discord.com/api/v9/channels/${channelId}/messages`,
        { content },
        { headers: { Authorization: ` ${token}`, "Content-Type": "application/json" } }
    );
  } catch (err) {
    console.error(`Error sending to ${channelId}:`, err);
  }
}

// =====================
// XACANNA: trigger "/tt trending"
// =====================
const xacanna = new Client({ intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildMessages] });
xacanna.once('ready', () => {
  console.log(`Xacanna ready as ${xacanna.user.tag}`);
  //xacanna should send the commands p
});
xacanna.login(process.env.LOWSCANNABOT);

// =====================
// CHADBRICK: listen & forward
// =====================
const chadbrick = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.MessageContent
  ]
});

chadbrick.once('ready', () => {
  console.log(`Chadbrick ready as ${chadbrick.user.tag}`);
});

chadbrick.on('messageCreate', async msg => {
  const txt = msg.content.trim().toLowerCase();

  // --- 1) TT commands (any user) in TT_CHANNEL_ID ---
  if (msg.channel.id === TT_CHANNEL_ID) {
    if (txt === 'tt start') {
      isMonitoringTT = true;
      return sendMessageAs('TT monitoring started.', CHADBRICK_TOKEN, TT_CHANNEL_ID);
    }
    if (txt === 'tt stop') {
      isMonitoringTT = false;
      return sendMessageAs('TT monitoring stopped.', CHADBRICK_TOKEN, TT_CHANNEL_ID);
    }
    if (txt.startsWith('tt config')) {
      const [ , , cd, mn, mx ] = txt.split(/\s+/);
      if (cd && mn && mx) {
        commandDelay  = parseInt(cd,10)*1000;
        copyMinDelay  = parseInt(mn,10)*1000;
        copyMaxDelay  = parseInt(mx,10)*1000;
        return sendMessageAs(
            `TT delays set: parse ${commandDelay/1000}s, copy ${copyMinDelay/1000}-${copyMaxDelay/1000}s`,
            CHADBRICK_TOKEN,
            TT_CHANNEL_ID
        );
      }
    }
  }

  // --- 2) Burp commands (any user) in BURP_CHANNEL_ID ---
  if (msg.channel.id === BURP_CHANNEL_ID) {
    if (txt === 'burp start') {
      isMonitoringBurp = true;
      return sendMessageAs('Burp monitoring started.', CHADBRICK_TOKEN, BURP_CHANNEL_ID);
    }
    if (txt === 'burp stop') {
      isMonitoringBurp = false;
      return sendMessageAs('Burp monitoring stopped.', CHADBRICK_TOKEN, BURP_CHANNEL_ID);
    }
    if (txt.startsWith('burp config')) {
      const [ , , cd ] = txt.split(/\s+/);
      if (cd) {
        BURP_COOLDOWN_MS = parseInt(cd,10)*1000;
        return sendMessageAs(
            `Burp cooldown set to ${BURP_COOLDOWN_MS/1000}s`,
            CHADBRICK_TOKEN,
            BURP_CHANNEL_ID
        );
      }
    }
  }

  // --- 3) Handle /burp embeds if enabled ---
// --- 3) Handle /burp embeds if enabled ---
  if (
      isMonitoringBurp &&
      msg.author.id === RICK_APP_ID &&
      msg.channel.id === BURP_CHANNEL_ID &&
      msg.embeds.length
  ) {
    const embed = msg.embeds[0];
    if (!processedBurpEmbeds.has(msg.id)) {
      const now     = Date.now();
      const cooling = (now - lastBurpTime) < BURP_COOLDOWN_MS;
      if (!cooling) {
        burpCycleProcessed.clear();
        lastBurpTime = now;
      }
      processedBurpEmbeds.add(msg.id);

      // split into lines
      const lines    = (embed.description||'').split(/\r?\n/).filter(l=>l.trim());
      // stats lines vs URL lines
      const statLines = lines.filter(l => /Δ\s*[-\d.]+%/.test(l));
      const urlLines  = lines.filter(l => /^https?:\/\/\S+/.test(l));

      console.log(lines, statLines, urlLines);

      // iterate them in parallel
      for (let i = 0; i < statLines.length && i < urlLines.length; i++) {
        const stat = statLines[i];
        const fullUrl = urlLines[i];

        // extract the token ID at the end of the path
        const token = fullUrl.split('/').pop();

        // parse the % change
        const pct = parseFloat((stat.match(/Δ\s*([-.\d]+)%/)||[])[1]||'0');

        // skip if seen positive already
        if (burpCycleProcessed.has(token) && pct >= 0) {
          console.log('Skipping duplicate burp token:', token);
          continue;
        }
        burpCycleProcessed.add(token);

        console.log(`Burping token: ${token} (${pct}%)`);
        await sleep(randomDelay(3000, 5000));
        await sendMessageAs(token, CHADBRICK_TOKEN, GLOBAL_CHANNEL_ID);
      }

      if (processedBurpEmbeds.size > 10000) processedBurpEmbeds.clear();
    }
    return;
  }


  // --- 4) Handle /tt embeds if enabled ---
  if (
      isMonitoringTT &&
      msg.author.id === RICK_APP_ID &&
      msg.channel.id === COMMAND_CHANNEL_ID &&
      msg.embeds.length
  ) {
    const embed = msg.embeds[0];
    if (!processedEmbeds.has(msg.id)) {
      const now = Date.now();
      if (now - lastTrendingTime < commandDelay) return;
      lastTrendingTime = now;
      processedEmbeds.add(msg.id);

      const lines = (embed.description||'')
          .split(/\r?\n/).filter(l=>l.trim());

      for (const line of lines) {
        const m = line.match(/https?:\/\/twitter\.com\/[^\s)]+/);
        if (!m) continue;
        const url = m[0];

        if (processedTweets.has(url)) {
          console.log('Skipping duplicate URL:', url);
          continue;
        }
        processedTweets.add(url);

        console.log('Forwarding URL:', url);
        await sleep(randomDelay(copyMinDelay,copyMaxDelay));
        await sendMessageAs(url, CHADBRICK_TOKEN, X_CHANNEL_ID);
      }

      if (processedEmbeds.size > 10000) processedEmbeds.clear();
    }
    return;
  }

  if (
      isMonitoringTT &&
      msg.channel.id === TT_CHANNEL_ID &&
      !msg.author.bot
  ) {
    const regex = /https?:\/\/twitter\.com\/[\w]+\/status\/\d+/g;
    let m;
    while ((m = regex.exec(msg.content)) !== null) {
      const url = m[0];
      if (processedTweets.has(url)) {
        console.log('Skipping duplicate URL:', url);
        continue;
      }
      processedTweets.add(url);

      console.log('Forwarding URL:', url);
      const ethCA = msg.content.match(/\b0x[a-fA-F0-9]{40}\b/);
      const solCA = msg.content.match(/\b[1-9A-HJ-NP-Za-km-z]{32,44}\b/);
      if (ethCA || solCA) {
        const addr = ethCA ? ethCA[0] : solCA[0];
        console.log('Found on-chain address, sending only to #call:', addr);
        await sendMessageAs(url, CHADBRICK_TOKEN, CALL_CHANNEL_ID);
        continue;
      }

      await sleep(randomDelay(copyMinDelay,copyMaxDelay));
      await sendMessageAs(url, CHADBRICK_TOKEN, X_CHANNEL_ID);
    }
    if (processedTweets.size > 10000) processedTweets.clear();
  }
});

chadbrick.login(process.env.LOWSCANNABOT);
