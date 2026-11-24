#!/usr/bin/env node
/**
 * WhatsApp HTTP bridge for Lionclaw.
 *
 * The Python adapter (olano/platforms/whatsapp.py) talks to this process
 * over HTTP on --port (default 3000).  Endpoints implemented here:
 *
 *   GET  /health
 *        → { status: 'qr'|'connected'|'connecting'|'logged_out', qr?: string,
 *            uptime: number, paired: boolean, jid?: string }
 *
 *   GET  /messages
 *        → drains the inbound queue, returns array of message events:
 *          { messageId, chatId, chatName, isGroup, senderId, senderName,
 *            body, hasMedia, mediaType, mediaUrls, ts, botIds,
 *            mentionedIds, quotedParticipant, quotedId, quotedText,
 *            quotedMediaType, quotedMediaMime, quotedMediaUrls }
 *        Inbound media (voice notes, images, video, documents) is decrypted
 *        and written to the media dir here, and `mediaUrls` carries the
 *        absolute paths — the Python adapter cannot do this itself, because
 *        WhatsApp serves media encrypted and only Baileys holds the mediaKey.
 *        The same is done for the attachment on a *quoted* message
 *        (`quotedMediaUrls`), so replying to a photo hands the agent the photo
 *        and not just the fact that a reply happened.
 *
 *   POST /send         { chatId, message, replyTo? }   → { messageId }
 *   POST /edit         { chatId, messageId, message }  → { ok: true }
 *   POST /send-media   { chatId, filePath, mediaType, caption?, fileName?, ptt? }
 *                                                       → { messageId }
 *        (mediaType 'audio' with ptt:true sends a push-to-talk voice bubble;
 *         requires an Ogg/Opus file — the Python side checks the extension)
 *   POST /typing       { chatId }                       → { ok: true }
 *   POST /pair-code    { number }                       → { code }
 *        (phone-number pairing: the user types the code into WhatsApp's
 *         "Link with phone number" instead of scanning the QR; unpaired only)
 *   GET  /chat/:id     → { name, isGroup, participants: [...] }
 *   GET  /groups       → [{ chatId, name, isGroup: true, participants: n }]
 *        (every group this account participates in, via Baileys
 *         groupFetchAllParticipating)
 *
 * CLI:    node index.js --port 3000 --session <dir> --mode self-chat
 * Env:    WHATSAPP_REPLY_PREFIX     prepended to every outbound text
 *         WHATSAPP_INCOMING_BUFFER  max queued inbound messages (default 500)
 *         WHATSAPP_DEBUG            "1" → verbose stderr logging
 *         WHATSAPP_MEDIA_DIR        where inbound media is written
 *                                   (default: <session>/../media)
 *         WHATSAPP_MEDIA_MAX_BYTES  skip larger attachments (default 26214400)
 *         WHATSAPP_MEDIA_RETENTION_HOURS  prune written media after this many
 *                                   hours (default 24, 0 disables)
 *
 * Pairing UX: when WhatsApp asks for a QR, the string is exposed on /health
 * as `qr` AND printed to stderr as ASCII art (qrcode-terminal) so the same
 * binary works for `olano connect whatsapp` and the dashboard.
 */

'use strict';

const path = require('path');
const fs = require('fs');
const http = require('http');
const url = require('url');

let baileys, qrcodeTerminal, Boom;
try {
  baileys = require('@whiskeysockets/baileys');
  Boom = require('@hapi/boom').Boom;
  qrcodeTerminal = require('qrcode-terminal');
} catch (err) {
  process.stderr.write(
    `[whatsapp-bridge] missing dependency: ${err.message}\n` +
    `Run: cd whatsapp-bridge && npm install\n`,
  );
  process.exit(2);
}

const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  downloadMediaMessage,
} = baileys;

// ── CLI args ─────────────────────────────────────────────────────────────────
function parseArgs(argv) {
  const out = { port: 3000, session: null, mode: 'self-chat', mediaDir: null };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--port') out.port = parseInt(argv[++i], 10);
    else if (a === '--session') out.session = argv[++i];
    else if (a === '--mode') out.mode = argv[++i];
    else if (a === '--media-dir') out.mediaDir = argv[++i];
    else if (a === '--help' || a === '-h') {
      process.stdout.write(
        'Usage: node index.js [--port N] [--session DIR] [--mode MODE] [--media-dir DIR]\n',
      );
      process.exit(0);
    }
  }
  return out;
}

const ARGS = parseArgs(process.argv);

// The adapter always passes --session; this default only serves a bridge run
// by hand. It mirrors olano/config/home.py: the env pin, then whichever data
// dir exists (~/.olano, then a visible ~/olano unless a source checkout
// occupies it, then ~/.lionclaw), defaulting to ~/.olano.
function defaultOlanoHome() {
  const envHome = process.env.OLANO_HOME || process.env.LIONCLAW_HOME;
  if (envHome) return envHome;
  const home = process.env.HOME || process.env.USERPROFILE || '.';
  const hidden = path.join(home, '.olano');
  const visible = path.join(home, 'olano');
  const isCheckout =
    fs.existsSync(path.join(visible, 'pyproject.toml')) || fs.existsSync(path.join(visible, '.git'));
  const candidates = isCheckout ? [hidden] : [hidden, visible];
  candidates.push(path.join(home, '.lionclaw'));
  const existing = candidates.find((dir) => fs.existsSync(dir));
  return existing || hidden;
}

const SESSION_DIR =
  ARGS.session || path.join(defaultOlanoHome(), 'platforms', 'whatsapp', 'session');
const REPLY_PREFIX = process.env.WHATSAPP_REPLY_PREFIX || '';
const INCOMING_BUFFER = parseInt(process.env.WHATSAPP_INCOMING_BUFFER || '500', 10);
const DEBUG = process.env.WHATSAPP_DEBUG === '1';

// Where decrypted inbound media lands. Defaults next to the session dir (the
// same place bridge.log lives), so every spawn path — the adapter, the
// dashboard pair endpoint, the connect CLI — gets a working location without
// having to pass an extra argument.
const MEDIA_DIR =
  ARGS.mediaDir || process.env.WHATSAPP_MEDIA_DIR || path.join(SESSION_DIR, '..', 'media');
// A voice note is tens of KB and a photo a few hundred; WhatsApp will happily
// hand us a 100 MB video, which we would buffer in memory to decrypt. Skip
// those instead — the adapter still reports the attachment by type.
const MEDIA_MAX_BYTES = parseInt(process.env.WHATSAPP_MEDIA_MAX_BYTES || '26214400', 10);
const MEDIA_RETENTION_HOURS = parseFloat(process.env.WHATSAPP_MEDIA_RETENTION_HOURS || '24');
// Decryption fetches from WhatsApp's CDN; without a ceiling one stalled
// download holds up every message queued behind it.
const MEDIA_TIMEOUT_MS = parseInt(process.env.WHATSAPP_MEDIA_TIMEOUT_MS || '60000', 10);

fs.mkdirSync(SESSION_DIR, { recursive: true });

function log(...args) {
  if (DEBUG) process.stderr.write('[whatsapp-bridge] ' + args.join(' ') + '\n');
}

function info(msg) {
  process.stderr.write('[whatsapp-bridge] ' + msg + '\n');
}

// ── State ────────────────────────────────────────────────────────────────────
let sock = null;
let connectionStatus = 'connecting'; // 'connecting' | 'qr' | 'connected' | 'logged_out'
let lastQR = null;
let pairedJid = null;
const startedAt = Date.now();
const inboundQueue = []; // [{...messageEvent}]

// Resolve once on connection so /messages can stamp `botIds` for @mention logic.
//
// `sock.user` exposes both JID forms WhatsApp assigns the paired account:
// the phone-number JID (`id`) and the privacy-preserving LID (`lid`). Groups
// with phone-number privacy active address every participant — including
// the bot itself — by LID, so a mention/reply/quote referencing the bot in
// those groups carries the LID, not the phone JID. Returning only `id` here
// means that never matches, silently breaking require_mention in such
// groups. Return both so downstream set-intersection matches either form.
function botIds() {
  const ids = [];
  if (sock?.user?.id) ids.push(normalizeJid(sock.user.id));
  if (sock?.user?.lid) ids.push(normalizeJid(sock.user.lid));
  return ids;
}

// Quiet pino-shaped logger. Module level because both makeWASocket and
// downloadMediaMessage take one, and Baileys calls methods on it unguarded.
const silentLogger = { level: 'silent', child: () => silentLogger };
for (const m of ['fatal', 'error', 'warn', 'info', 'debug', 'trace']) {
  silentLogger[m] = () => {};
}

function normalizeJid(jid) {
  if (!jid) return '';
  // Baileys jids look like "12345:7@s.whatsapp.net".  Strip the device suffix.
  return String(jid).replace(/:\d+@/, '@');
}

function pushInbound(event) {
  inboundQueue.push(event);
  while (inboundQueue.length > INCOMING_BUFFER) inboundQueue.shift();
}

// ── Baileys connection ───────────────────────────────────────────────────────
async function connect() {
  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);

  // Pin to whatever WA-Web version Baileys' fetchLatestBaileysVersion()
  // resolves to.  Without this WhatsApp's servers reject the handshake with
  // code 405 ("Method Not Allowed") and the bridge never reaches the QR
  // phase.
  let version;
  try {
    const fetched = await fetchLatestBaileysVersion();
    version = fetched.version;
    log('Using WA-Web version', version.join('.'), 'isLatest=', fetched.isLatest);
  } catch (e) {
    log('fetchLatestBaileysVersion failed, using Baileys default:', e.message);
  }

  sock = makeWASocket({
    version,
    auth: state,
    logger: silentLogger,
    syncFullHistory: false,
    markOnlineOnConnect: false,
    browser: ['Lionclaw', 'Chrome', '1.0.0'],
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      lastQR = qr;
      connectionStatus = 'qr';
      info('QR code generated. Scan with WhatsApp → Settings → Linked Devices.');
      try {
        qrcodeTerminal.generate(qr, { small: true }, (code) => {
          process.stderr.write('\n' + code + '\n');
        });
      } catch (e) {
        log('qrcode-terminal failed:', e.message);
      }
    }
    if (connection === 'open') {
      connectionStatus = 'connected';
      lastQR = null;
      pairedJid = normalizeJid(sock?.user?.id);
      info(`Connected. Paired as ${pairedJid}`);
    }
    if (connection === 'close') {
      const code =
        lastDisconnect?.error instanceof Boom
          ? lastDisconnect.error.output?.statusCode
          : undefined;
      const loggedOut = code === DisconnectReason.loggedOut;
      if (loggedOut) {
        connectionStatus = 'logged_out';
        pairedJid = null;
        info('Logged out by WhatsApp. Delete the session dir and re-pair.');
        // Don't auto-reconnect on logout; bail so the parent sees us exit.
        process.exit(1);
      } else {
        connectionStatus = 'connecting';
        info(`Connection closed (code=${code ?? 'n/a'}). Reconnecting in 2s…`);
        setTimeout(() => connect().catch((e) => info('reconnect failed: ' + e.message)), 2000);
      }
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return;
    for (const msg of messages) {
      if (!msg.message) continue;
      if (msg.key?.fromMe) continue;
      try {
        // Awaited, not fired off: the adapter drains /messages within a second
        // and a voice note whose path lands after that read is a message the
        // agent already answered empty. Ordering is preserved for free.
        const media = classifyMedia(msg.message);
        const mediaUrls = await downloadInboundMedia(msg, media);
        // A reply to a photo carries no media of its own, so this is a second
        // download decision, not a branch of the first.
        const quotedMedia = getQuotedMedia(msg.message);
        const quotedMediaUrls = await downloadQuotedMedia(msg, quotedMedia);
        pushInbound(toMessageEvent(msg, mediaUrls, quotedMedia, quotedMediaUrls));
      } catch (e) {
        log('upsert decode failed:', e.message);
      }
    }
  });
}

// ── Message decoding ─────────────────────────────────────────────────────────
function pickText(m) {
  if (!m) return '';
  return (
    m.conversation ||
    m.extendedTextMessage?.text ||
    m.imageMessage?.caption ||
    m.videoMessage?.caption ||
    m.documentMessage?.caption ||
    m.documentWithCaptionMessage?.message?.documentMessage?.caption ||
    ''
  );
}

function classifyMedia(m) {
  if (!m) return { hasMedia: false, mediaType: '' };
  if (m.imageMessage)
    return { hasMedia: true, mediaType: 'image/jpeg', kind: 'image', node: m.imageMessage };
  if (m.videoMessage)
    return { hasMedia: true, mediaType: 'video/mp4', kind: 'video', node: m.videoMessage };
  if (m.audioMessage) {
    const isPtt = m.audioMessage.ptt;
    return {
      hasMedia: true,
      mediaType: isPtt ? 'audio/ogg; codecs=opus' : 'audio/mpeg',
      kind: 'audio',
      node: m.audioMessage,
    };
  }
  if (m.documentMessage)
    return {
      hasMedia: true,
      mediaType: m.documentMessage.mimetype || 'application/octet-stream',
      kind: 'document',
      node: m.documentMessage,
    };
  if (m.documentWithCaptionMessage?.message?.documentMessage)
    return {
      hasMedia: true,
      mediaType:
        m.documentWithCaptionMessage.message.documentMessage.mimetype ||
        'application/octet-stream',
      kind: 'document',
      node: m.documentWithCaptionMessage.message.documentMessage,
    };
  return { hasMedia: false, mediaType: '' };
}

// ── Inbound media download ───────────────────────────────────────────────────
//
// WhatsApp never serves media in the clear: the message carries a CDN url plus
// a `mediaKey`, and only Baileys can decrypt the stream. So the Python adapter
// physically cannot fetch a voice note itself — it can only be handed a local
// file. Until this existed the bridge reported `mediaUrls: []` for everything,
// which made an inbound voice note (a body-less message) reach the agent as an
// empty prompt: speech-to-text never got a file to transcribe.
//
// The extension must be truthful, not merely plausible: the speech provider
// infers the container from the filename it is posted under, so an `audio/mpeg`
// saved as `.ogg` is rejected outright rather than transcribed.
const AUDIO_EXTENSIONS = {
  'audio/ogg': '.ogg',
  'audio/opus': '.ogg',
  'audio/mpeg': '.mp3',
  'audio/mp3': '.mp3',
  'audio/mp4': '.m4a',
  'audio/m4a': '.m4a',
  'audio/aac': '.m4a',
  'audio/x-m4a': '.m4a',
  'audio/wav': '.wav',
  'audio/x-wav': '.wav',
  'audio/webm': '.webm',
  'audio/amr': '.amr',
  'audio/flac': '.flac',
};
const IMAGE_EXTENSIONS = {
  'image/jpeg': '.jpg',
  'image/jpg': '.jpg',
  'image/png': '.png',
  'image/webp': '.webp',
  'image/gif': '.gif',
  'image/bmp': '.bmp',
  'image/heic': '.heic',
};
const VIDEO_EXTENSIONS = {
  'video/mp4': '.mp4',
  'video/quicktime': '.mov',
  'video/3gpp': '.3gp',
  'video/webm': '.webm',
  'video/x-matroska': '.mkv',
};

// The mimetype arrives from the sender's client, so strip parameters
// ("audio/ogg; codecs=opus") before looking it up.
function baseMime(mimetype) {
  return String(mimetype || '')
    .split(';')[0]
    .trim()
    .toLowerCase();
}

// A remote sender chooses the document filename, so it is untrusted input:
// `path.basename` plus a strict character filter is what keeps "../../creds"
// from becoming a write outside MEDIA_DIR.
function safeFileName(name) {
  const base = path.basename(String(name || '').replace(/\0/g, ''));
  const cleaned = base.replace(/[^A-Za-z0-9._-]/g, '_').replace(/^\.+/, '');
  if (!cleaned || cleaned === '_') return '';
  return cleaned.slice(-120);
}

function mediaExtension(kind, mimetype, fileName) {
  const mime = baseMime(mimetype);
  if (kind === 'audio') return AUDIO_EXTENSIONS[mime] || '.ogg';
  if (kind === 'image') return IMAGE_EXTENSIONS[mime] || '.jpg';
  if (kind === 'video') return VIDEO_EXTENSIONS[mime] || '.mp4';
  const ext = path.extname(safeFileName(fileName));
  return ext && ext.length <= 12 ? ext.toLowerCase() : '.bin';
}

function randomHex(n) {
  return require('crypto').randomBytes(Math.ceil(n / 2)).toString('hex').slice(0, n);
}

// Documents keep their human-readable name behind a `doc_<hex12>_` prefix —
// the same convention the Python document cache uses, which is what lets the
// adapter show the sender's own filename to the agent.
function mediaFileNameFor(kind, mimetype, fileName) {
  const hex = randomHex(12);
  if (kind === 'document') {
    const safe = safeFileName(fileName);
    if (safe) return `doc_${hex}_${safe}`;
    return `doc_${hex}${mediaExtension(kind, mimetype, fileName)}`;
  }
  const prefix = kind === 'image' ? 'img' : kind;
  return `${prefix}_${hex}${mediaExtension(kind, mimetype, fileName)}`;
}

let lastMediaPrune = 0;

// Nothing else ever deletes these files, and the adapter has already copied
// what it wants to keep into the agent's workspace by the time they age out.
function pruneMediaDir() {
  if (!(MEDIA_RETENTION_HOURS > 0)) return;
  const now = Date.now();
  if (now - lastMediaPrune < 15 * 60 * 1000) return;
  lastMediaPrune = now;
  const cutoff = now - MEDIA_RETENTION_HOURS * 3600 * 1000;
  let entries;
  try {
    entries = fs.readdirSync(MEDIA_DIR);
  } catch {
    return;
  }
  for (const entry of entries) {
    const full = path.join(MEDIA_DIR, entry);
    try {
      const st = fs.statSync(full);
      if (st.isFile() && st.mtimeMs < cutoff) fs.unlinkSync(full);
    } catch {
      /* raced with another prune or still being written — leave it */
    }
  }
}

async function downloadInboundMedia(msg, media, what = 'inbound') {
  if (!media?.hasMedia || !media.node) return [];
  if (typeof downloadMediaMessage !== 'function') {
    log('baileys build exposes no downloadMediaMessage — inbound media skipped');
    return [];
  }
  const declared = Number(media.node.fileLength) || 0;
  if (MEDIA_MAX_BYTES > 0 && declared > MEDIA_MAX_BYTES) {
    info(
      `Skipping ${what} ${media.kind} attachment: ${declared} bytes exceeds ` +
        `WHATSAPP_MEDIA_MAX_BYTES (${MEDIA_MAX_BYTES}).`,
    );
    return [];
  }

  let timer;
  try {
    const download = downloadMediaMessage(
      msg,
      'buffer',
      {},
      // An old message's CDN copy can be gone; `reuploadRequest` lets Baileys
      // ask the sender's device to put it back instead of failing.
      { logger: silentLogger, reuploadRequest: sock?.updateMediaMessage },
    );
    const buffer = await Promise.race([
      download,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error('media download timed out')), MEDIA_TIMEOUT_MS);
      }),
    ]);
    if (!buffer || !buffer.length) {
      info(`Inbound ${what} ${media.kind} decrypted to zero bytes — skipped.`);
      return [];
    }
    if (MEDIA_MAX_BYTES > 0 && buffer.length > MEDIA_MAX_BYTES) {
      info(
        `Skipping ${what} ${media.kind} attachment: ${buffer.length} bytes exceeds ` +
          `WHATSAPP_MEDIA_MAX_BYTES (${MEDIA_MAX_BYTES}).`,
      );
      return [];
    }
    fs.mkdirSync(MEDIA_DIR, { recursive: true });
    const name = mediaFileNameFor(media.kind, media.node.mimetype || media.mediaType, media.node.fileName);
    const full = path.join(MEDIA_DIR, name);
    // Write to a temp name first: the adapter polls /messages every second and
    // must never be handed the path of a half-written file.
    const tmp = `${full}.part`;
    fs.writeFileSync(tmp, buffer, { mode: 0o600 });
    fs.renameSync(tmp, full);
    log(`Saved ${what} ${media.kind} (${buffer.length} bytes) to ${full}`);
    pruneMediaDir();
    return [full];
  } catch (e) {
    // Never lose the message over its attachment: the adapter still sees
    // hasMedia/mediaType and tells the sender it could not read the file.
    info(`Inbound ${what} ${media.kind} download failed: ${e?.message || e}`);
    return [];
  } finally {
    clearTimeout(timer);
  }
}

// Baileys hangs the reply/mention metadata off whichever message variant was
// sent, so every reader has to walk the same chain.
function getContextInfo(m) {
  return (
    m?.extendedTextMessage?.contextInfo ||
    m?.imageMessage?.contextInfo ||
    m?.videoMessage?.contextInfo ||
    m?.audioMessage?.contextInfo ||
    m?.documentMessage?.contextInfo
  );
}

function getQuotedParticipant(m) {
  const ctx = getContextInfo(m);
  return ctx?.participant ? normalizeJid(ctx.participant) : '';
}

function getMentionedIds(m) {
  const ctx = getContextInfo(m);
  return (ctx?.mentionedJid || []).map(normalizeJid);
}

// When the user long-presses a message and replies, WhatsApp sends the quoted
// message's id (stanzaId) AND its full content (quotedMessage) inline — no
// lookup needed. Both are forwarded so the agent can see what is being
// replied to; without them a reply reads as an ordinary message.
function getQuotedId(m) {
  return getContextInfo(m)?.stanzaId || '';
}

function getQuotedText(m) {
  return pickText(getContextInfo(m)?.quotedMessage);
}

// `contextInfo.quotedMessage` is the quoted message's *content* node, the same
// shape `msg.message` has — so the ordinary media classifier reads it as-is.
function getQuotedMedia(m) {
  return classifyMedia(getContextInfo(m)?.quotedMessage);
}

// `downloadMediaMessage` wants a whole WAMessage, and a quoted message is only
// ever handed to us as content plus a stanza id. The decryption `mediaKey` is
// inside the content node, so plain decryption would work with any envelope —
// what this rebuild buys is `reuploadRequest`, which addresses a device by
// message key to ask for a CDN copy that has since expired. A quote points at
// an older message by definition, so it hits that path far more often than a
// fresh attachment does.
function quotedMessageStub(msg, contextInfo) {
  const remoteJid = msg.key?.remoteJid || '';
  const participant = contextInfo.participant || '';
  const isGroup = remoteJid.endsWith('@g.us');
  return {
    key: {
      remoteJid,
      id: contextInfo.stanzaId || '',
      // A reply to something the bot itself sent quotes our own message; get
      // this wrong and `reuploadRequest` asks the wrong device for the media.
      fromMe: !!participant && botIds().includes(normalizeJid(participant)),
      ...(isGroup && participant ? { participant } : {}),
    },
    message: contextInfo.quotedMessage,
  };
}

async function downloadQuotedMedia(msg, quotedMedia) {
  if (!quotedMedia?.hasMedia) return [];
  const ctx = getContextInfo(msg.message);
  if (!ctx?.quotedMessage || !ctx.stanzaId) return [];
  return downloadInboundMedia(quotedMessageStub(msg, ctx), quotedMedia, 'quoted');
}

function toMessageEvent(msg, mediaUrls = [], quotedMedia = null, quotedMediaUrls = []) {
  const chatId = msg.key.remoteJid || '';
  const isGroup = chatId.endsWith('@g.us');
  const m = msg.message || {};
  const body = pickText(m);
  const { hasMedia, mediaType, node } = classifyMedia(m);

  // WhatsApp's LID privacy system (Linked IDentifier) anonymises the sender
  // when phone-number privacy is on or for community-style chats: msg.key
  // .participant is then "<random>@lid" and the real phone JID is exposed
  // separately as msg.key.senderPn.  We forward BOTH so the Python allowlist
  // can match either form.
  const senderId = normalizeJid(
    msg.key.senderPn || msg.key.participant || chatId,
  );
  const senderLid = normalizeJid(
    msg.key.senderLid || (msg.key.participant && msg.key.participant.endsWith('@lid') ? msg.key.participant : '')
  );
  const senderPn = normalizeJid(msg.key.senderPn || '');

  return {
    messageId: msg.key.id,
    chatId,
    chatName: msg.pushName || chatId,
    isGroup,
    senderId,
    senderPn,
    senderLid,
    senderName: msg.pushName || '',
    body,
    hasMedia,
    mediaType,
    // Absolute paths to the decrypted files (empty when there is no media, or
    // when the download was skipped/failed — the adapter degrades to a note).
    mediaUrls,
    // The sender's own mimetype and filename, so the adapter can label the
    // attachment truthfully instead of guessing from the message kind.
    mediaMime: node?.mimetype || mediaType || '',
    mediaFileName: safeFileName(node?.fileName) || '',
    ts: Math.floor((Number(msg.messageTimestamp) || Date.now() / 1000)),
    botIds: botIds(),
    mentionedIds: getMentionedIds(m),
    quotedParticipant: getQuotedParticipant(m),
    quotedId: getQuotedId(m),
    quotedText: getQuotedText(m),
    // The attachment on the message being replied to. A caption-less photo
    // makes `quotedText` empty, so without these a reply to a picture reached
    // the agent as "a reply to something" and nothing else. No `fileName`
    // field: the bridge already bakes the sender's name into the document's
    // path (`doc_<hex>_<name>`), which is where the adapter reads it from.
    quotedMediaType: quotedMedia?.mediaType || '',
    quotedMediaMime: quotedMedia?.node?.mimetype || quotedMedia?.mediaType || '',
    quotedMediaUrls,
  };
}

// ── HTTP server ──────────────────────────────────────────────────────────────
function readBody(req, max = 25 * 1024 * 1024) {
  return new Promise((resolve, reject) => {
    let len = 0;
    const chunks = [];
    req.on('data', (c) => {
      len += c.length;
      if (len > max) {
        req.destroy();
        reject(new Error('payload too large'));
        return;
      }
      chunks.push(c);
    });
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch (e) {
        reject(e);
      }
    });
    req.on('error', reject);
  });
}

function send(res, code, obj) {
  const body = Buffer.from(JSON.stringify(obj));
  res.writeHead(code, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': body.length,
  });
  res.end(body);
}

function withReplyPrefix(text) {
  if (!REPLY_PREFIX) return text;
  return text.startsWith(REPLY_PREFIX) ? text : REPLY_PREFIX + text;
}

async function sendText(chatId, text, replyTo) {
  if (!sock) throw new Error('not connected');
  const opts = {};
  if (replyTo) {
    opts.quoted = { key: { remoteJid: chatId, id: replyTo, fromMe: false }, message: { conversation: '' } };
  }
  const res = await sock.sendMessage(chatId, { text: withReplyPrefix(text) }, opts);
  return res?.key?.id || '';
}

async function editText(chatId, messageId, text) {
  if (!sock) throw new Error('not connected');
  // Baileys edit API: send with `edit` key referencing the original.
  await sock.sendMessage(chatId, {
    text: withReplyPrefix(text),
    edit: { remoteJid: chatId, id: messageId, fromMe: true },
  });
  return true;
}

// Audio containers WhatsApp accepts, by file extension.  A push-to-talk
// bubble (ptt) is only rendered for Ogg/Opus; everything else has to go as a
// regular audio message, which still plays inline but shows as a file row.
const AUDIO_MIMETYPES = {
  '.ogg': 'audio/ogg; codecs=opus',
  '.opus': 'audio/ogg; codecs=opus',
  '.mp3': 'audio/mpeg',
  '.m4a': 'audio/mp4',
  '.mp4': 'audio/mp4',
  '.aac': 'audio/aac',
  '.wav': 'audio/wav',
  '.flac': 'audio/flac',
};

function audioMimetype(filePath) {
  return AUDIO_MIMETYPES[path.extname(filePath).toLowerCase()] || 'audio/mp4';
}

async function sendMedia(chatId, filePath, mediaType, caption, fileName, ptt) {
  if (!sock) throw new Error('not connected');
  if (!fs.existsSync(filePath)) throw new Error(`file not found: ${filePath}`);
  const data = fs.readFileSync(filePath);

  let payload;
  if (mediaType === 'image') {
    payload = { image: data, caption: caption || undefined };
  } else if (mediaType === 'video') {
    payload = { video: data, caption: caption || undefined };
  } else if (mediaType === 'audio') {
    const mimetype = audioMimetype(filePath);
    // Only Ogg/Opus can be a voice note.  Honouring ptt:true for an mp3 would
    // produce a bubble WhatsApp cannot play, so the container has the last
    // word — the caller's preference degrades to a normal audio message.
    const asVoiceNote = ptt === true && mimetype.startsWith('audio/ogg');
    payload = { audio: data, mimetype, ptt: asVoiceNote };
  } else {
    payload = {
      document: data,
      fileName: fileName || path.basename(filePath),
      mimetype: 'application/octet-stream',
      caption: caption || undefined,
    };
  }
  const res = await sock.sendMessage(chatId, payload);
  return res?.key?.id || '';
}

async function listGroups() {
  if (!sock) throw new Error('not connected');
  // groupFetchAllParticipating returns { [jid]: metadata } for every group
  // this account is currently a member of.
  const all = await sock.groupFetchAllParticipating();
  return Object.entries(all || {}).map(([jid, meta]) => ({
    chatId: jid,
    name: meta?.subject || jid,
    isGroup: true,
    participants: (meta?.participants || []).length,
  }));
}

async function chatInfo(chatId) {
  if (!sock) throw new Error('not connected');
  const isGroup = chatId.endsWith('@g.us');
  if (!isGroup) {
    return { name: chatId, isGroup: false, participants: [] };
  }
  try {
    const meta = await sock.groupMetadata(chatId);
    return {
      name: meta.subject || chatId,
      isGroup: true,
      participants: (meta.participants || []).map((p) => normalizeJid(p.id)),
    };
  } catch (e) {
    return { name: chatId, isGroup: true, participants: [] };
  }
}

const server = http.createServer(async (req, res) => {
  const u = url.parse(req.url || '/', true);
  try {
    if (req.method === 'GET' && u.pathname === '/health') {
      return send(res, 200, {
        status: connectionStatus,
        qr: lastQR,
        uptime: Math.floor((Date.now() - startedAt) / 1000),
        paired: connectionStatus === 'connected',
        jid: pairedJid,
      });
    }

    if (req.method === 'GET' && u.pathname === '/messages') {
      const drained = inboundQueue.splice(0, inboundQueue.length);
      return send(res, 200, drained);
    }

    if (req.method === 'POST' && u.pathname === '/send') {
      const body = await readBody(req);
      if (!body.chatId || !body.message) return send(res, 400, { error: 'chatId and message required' });
      const id = await sendText(body.chatId, body.message, body.replyTo);
      return send(res, 200, { messageId: id });
    }

    if (req.method === 'POST' && u.pathname === '/edit') {
      const body = await readBody(req);
      if (!body.chatId || !body.messageId || !body.message)
        return send(res, 400, { error: 'chatId, messageId, message required' });
      await editText(body.chatId, body.messageId, body.message);
      return send(res, 200, { ok: true });
    }

    if (req.method === 'POST' && u.pathname === '/send-media') {
      const body = await readBody(req);
      if (!body.chatId || !body.filePath || !body.mediaType)
        return send(res, 400, { error: 'chatId, filePath, mediaType required' });
      const id = await sendMedia(
        body.chatId,
        body.filePath,
        body.mediaType,
        body.caption,
        body.fileName,
        body.ptt,
      );
      return send(res, 200, { messageId: id });
    }

    if (req.method === 'POST' && u.pathname === '/pair-code') {
      // Phone-number pairing: instead of scanning the QR, the user types an
      // 8-char code into WhatsApp (Linked devices → Link with phone number).
      // Only meaningful while unpaired; Baileys requires the socket to be in
      // its QR/connecting phase.
      const body = await readBody(req);
      const number = String(body.number || '').replace(/[^0-9]/g, '');
      if (!number) return send(res, 400, { error: 'number required (digits, incl. country code)' });
      if (connectionStatus === 'connected') return send(res, 409, { error: 'already paired' });
      if (!sock || typeof sock.requestPairingCode !== 'function')
        return send(res, 503, { error: 'socket not ready — try again in a few seconds' });
      const code = await sock.requestPairingCode(number);
      return send(res, 200, { code });
    }

    if (req.method === 'POST' && u.pathname === '/typing') {
      const body = await readBody(req);
      if (!body.chatId) return send(res, 400, { error: 'chatId required' });
      try {
        if (sock) {
          await sock.sendPresenceUpdate('composing', body.chatId);
          setTimeout(() => sock?.sendPresenceUpdate('paused', body.chatId).catch(() => {}), 4000);
        }
      } catch (e) {
        log('typing failed:', e.message);
      }
      return send(res, 200, { ok: true });
    }

    if (req.method === 'GET' && u.pathname && u.pathname.startsWith('/chat/')) {
      const chatId = decodeURIComponent(u.pathname.slice('/chat/'.length));
      const info_ = await chatInfo(chatId);
      return send(res, 200, info_);
    }

    if (req.method === 'GET' && u.pathname === '/groups') {
      const groups = await listGroups();
      return send(res, 200, groups);
    }

    if (req.method === 'GET' && u.pathname === '/') {
      return send(res, 200, {
        name: 'olano-whatsapp-bridge',
        version: require('./package.json').version || '0.0.0',
        status: connectionStatus,
        // Feature flags, so the Python adapter can detect a bridge older than
        // the code driving it instead of inferring it from a silent downgrade.
        // `inbound-media` = this bridge decrypts inbound attachments and
        // reports their paths on `mediaUrls`. A bridge without it queues every
        // voice note as a body-less message with no file, so the adapter
        // restarts it rather than reusing it (see `_bridge_lacks_inbound_media`).
        // `quoted-media` = it also decrypts the attachment on the message a
        // reply points at (`quotedMediaUrls`). Same staleness rule: without
        // it, replying to a photo reaches the agent with no photo.
        capabilities: [
          'send',
          'edit',
          'send-media',
          'voice-notes',
          'inbound-media',
          'quoted-media',
          'typing',
          'groups',
        ],
      });
    }

    send(res, 404, { error: 'not found', path: u.pathname });
  } catch (e) {
    log('handler error:', e?.stack || e?.message || e);
    send(res, 500, { error: e?.message || String(e) });
  }
});

server.on('error', (err) => {
  if (err && err.code === 'EADDRINUSE') {
    info(`Port ${ARGS.port} is in use. Set --port or stop the other bridge.`);
  } else {
    info('HTTP server error: ' + (err?.message || err));
  }
  process.exit(3);
});

server.listen(ARGS.port, '127.0.0.1', () => {
  info(`HTTP listening on http://127.0.0.1:${ARGS.port}`);
  info(`Session dir: ${SESSION_DIR}`);
  info(`Media dir: ${MEDIA_DIR}`);
  // Clear anything a previous run left behind before the first message lands.
  lastMediaPrune = 0;
  pruneMediaDir();
  info('Connecting to WhatsApp…');
});

connect().catch((err) => {
  info('Fatal during connect: ' + (err?.stack || err?.message || err));
  process.exit(1);
});

// ── Graceful shutdown ────────────────────────────────────────────────────────
function shutdown(reason) {
  info(`Shutting down (${reason})…`);
  try { sock?.end?.(); } catch {}
  try { server.close(); } catch {}
  setTimeout(() => process.exit(0), 500).unref();
}
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('uncaughtException', (e) => {
  info('uncaughtException: ' + (e?.stack || e?.message || e));
});
