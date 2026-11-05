// Read-only WhatsApp helper. Never sends messages.
// Only messages from the ONE watched group are ever emitted; everything else is dropped here.
import * as B from '@whiskeysockets/baileys';
// works with both ESM-style and CJS-style export shapes
const ON_DEMAND = B.proto?.HistorySync?.HistorySyncType?.ON_DEMAND ?? B.default?.proto?.HistorySync?.HistorySyncType?.ON_DEMAND ?? 6;
const pick = (k) => B[k] ?? B.default?.[k];
const makeWASocket = typeof B.default === 'function' ? B.default : (B.default?.default ?? B.makeWASocket);
const useMultiFileAuthState = pick('useMultiFileAuthState'), DisconnectReason = pick('DisconnectReason');
const fetchLatestWaWebVersion = pick('fetchLatestWaWebVersion'), Browsers = pick('Browsers');
import pino from 'pino';
import readline from 'readline';
import fs from 'fs';

const AUTH_DIR = process.env.AUTH_DIR || './auth';
let watch = process.env.WATCH_GROUP || '';
const names = new Map();           // id -> saved contact name / push name
let sock;
let startGeneration = 0, reconnectTimer;
let reconnectAttempts = 0;
let authStatePromise, clientVersionPromise;
const MAX_RECONNECT_ATTEMPTS = 3;
const out = (o) => process.stdout.write(JSON.stringify(o) + '\n');
const clean = (j) => String(j || '').split('@')[0].split(':')[0];
const NAMES_FILE = AUTH_DIR + '_group_names.json';
const groupNames = new Map();      // saved ONLY for members/posters of the watched group
try { for (const [k, v] of Object.entries(JSON.parse(fs.readFileSync(NAMES_FILE, 'utf8')))) { groupNames.set(k, v); names.set(k, v); } } catch {}
const ANCHOR_FILE = AUTH_DIR + '_anchor.json';      // newest message of the watched group we have seen (starting point for 'read earlier')
let anchor = null, cursor = null, olderTimer;
try { anchor = JSON.parse(fs.readFileSync(ANCHOR_FILE, 'utf8')); } catch {}
let anchorT;
const saveAnchor = () => { clearTimeout(anchorT); anchorT = setTimeout(() => { try { fs.writeFileSync(ANCHOR_FILE, JSON.stringify(anchor)); } catch {} }, 2000); };
const older = { active: false, floor: 0, got: 0 };
let olderWait = false;                                      // 'read earlier' asked before any group message was seen
let saveT;
const persistGroup = () => { clearTimeout(saveT); saveT = setTimeout(() => { try { fs.writeFileSync(NAMES_FILE, JSON.stringify(Object.fromEntries(groupNames))); } catch {} }, 2000); };

function remember(list) {
  for (const x of list || []) {
    const saved = x.name, push = x.notify || x.verifiedName;
    for (const k of [x.id, x.lid, x.phoneNumber]) {
      if (!k) continue;
      const key = clean(k);
      if (saved) names.set(key, saved);
      else if (push && !names.has(key)) names.set(key, push);
    }
  }
}
const num = (t) => (t && typeof t === 'object' && t.toNumber ? t.toNumber() : Number(t));

async function start() {
  const generation = ++startGeneration;
  clearTimeout(reconnectTimer);
  out({ type: 'status', msg: 'Checking WhatsApp connection settings...' });
  if (!authStatePromise) {
    authStatePromise = useMultiFileAuthState(AUTH_DIR).catch((error) => {
      authStatePromise = null;
      throw error;
    });
  }
  const { state, saveCreds } = await authStatePromise;
  if (!clientVersionPromise) {
    clientVersionPromise = fetchLatestWaWebVersion({ signal: AbortSignal.timeout(5000) });
  }
  const versionResult = await clientVersionPromise;
  const { version } = versionResult;
  if (!versionResult.isLatest) {
    const reason = versionResult.error?.message || 'WhatsApp Web version could not be retrieved';
    out({ type: 'status', msg: `WhatsApp Web version check failed (${reason}); trying bundled version ${version.join('.')}.` });
  } else {
    out({ type: 'status', msg: `Using current WhatsApp Web version ${version.join('.')}; opening connection...` });
  }
  out({ type: 'status', msg: state.creds.me
    ? 'Restoring saved WhatsApp session; opening connection...'
    : 'No saved WhatsApp session; opening connection to request a QR...' });
  sock = makeWASocket({
    version, auth: state, logger: pino({ level: 'warn' }),
    browser: Browsers.windows('Chrome'),
    // true = when the phone is linked it sends the FULL chat history (this is only read at link time). The old value
    // `false` made WhatsApp send almost nothing, so earlier messages could never be fetched afterwards.
    syncFullHistory: true, markOnlineOnConnect: false,
    // Without this, Baileys' default is `() => syncFullHistory` (= false) and it THROWS AWAY every history
    // reply - including the answer to "read earlier messages" - so the request always timed out.
    // Real notifications carry a download path / inline payload; the internal start-up probe does not, so
    // returning false for it keeps start-up fast (no 20 s wait) while real history is still processed.
    shouldSyncHistoryMessage: (h) => !!(h && (h.directPath || h.fileSha256 || h.initialHistBootstrapInlinePayload || h.syncType === ON_DEMAND)),
  });
  sock.ws.on('upgrade', (response) => {
    out({ type: 'transport', event: 'websocket_upgrade', status: response.statusCode });
  });
  sock.ws.on('unexpected-response', (_request, response) => {
    out({ type: 'transport', event: 'websocket_rejected', status: response.statusCode,
      message: String(response.statusMessage || '').slice(0, 200) });
  });
  sock.ws.on('error', (error) => {
    out({ type: 'transport', event: 'websocket_error', message: String(error?.message || error).slice(0, 240) });
  });
  sock.ws.on('close', (code, reason) => {
    out({ type: 'transport', event: 'websocket_close', code,
      reason: Buffer.isBuffer(reason) ? reason.toString('utf8').slice(0, 240) : String(reason || '').slice(0, 240) });
  });
  sock.ev.on('creds.update', saveCreds);
  sock.ev.on('contacts.upsert', remember);
  sock.ev.on('contacts.update', remember);
  sock.ev.on('messaging-history.set', ({ contacts, messages, syncType }) => {
    remember(contacts);
    let oldest = null, groupCount = 0;
    const batch = [];
    for (const m of messages || []) {
      if (m.key?.remoteJid === watch) {
        groupCount++;
        const t = num(m.messageTimestamp) * 1000;
        if (!oldest || t < oldest.ts) oldest = { key: { remoteJid: m.key.remoteJid, fromMe: !!m.key.fromMe, id: m.key.id, participant: m.key.participant }, ts: t };
      }
      const event = emitMsg(m, true, false);                    // recent / earlier history, group only
      if (event) batch.push(event);
    }
    if (batch.length) out({ type: 'messages', messages: batch });
    if (older.active && syncType === ON_DEMAND) {               // answer to "read earlier messages"
      clearTimeout(olderTimer);
      const fin = (status) => { older.active = false; out({ type: 'older', status, got: older.got }); };
      older.got += groupCount;
      if (!oldest || (cursor && oldest.ts >= cursor.ts)) return fin('done');
      cursor = oldest;
      out({ type: 'older', status: 'progress', got: older.got, oldest: Math.floor(oldest.ts / 1000) });
      if (oldest.ts <= older.floor) fin('done');
      else requestOlder();
    }
  });
  sock.ev.on('connection.update', ({ connection, lastDisconnect, qr }) => {
    if (generation !== startGeneration) return;
    if (qr) out({ type: 'qr', qr });
    if (connection === 'open') {
      reconnectAttempts = 0;
      out({ type: 'ready' });
    }
    if (connection === 'connecting') out({ type: 'status', msg: 'Connecting to WhatsApp; waiting for QR or saved-session login...' });
    if (connection === 'close') {
      const code = lastDisconnect?.error?.output?.statusCode;
      const reason = lastDisconnect?.error?.message || 'Connection closed without a reason from WhatsApp';
      out({ type: 'disconnect', code: code ?? null, reason });
      if (code === DisconnectReason.loggedOut) out({ type: 'logged_out' });
      else if (code === 515) {
        out({ type: 'status', msg: 'WhatsApp requested a connection restart after linking; reconnecting...' });
        reconnectTimer = setTimeout(() => {
          if (generation === startGeneration) {
            start().catch((e) => out({ type: 'error', msg: `cannot restart WhatsApp helper: ${e}` }));
          }
        }, 1000);
      }
      else {
        reconnectAttempts++;
        if (reconnectAttempts > MAX_RECONNECT_ATTEMPTS) {
          out({ type: 'status', msg: `Stopped after ${MAX_RECONNECT_ATTEMPTS} failed connection attempts. Check the Log, then restart or re-link.` });
          out({ type: 'stopped', code: code ?? null, reason, attempts: reconnectAttempts - 1 });
          return;
        }
        const delay = Math.min(2000 * (2 ** (reconnectAttempts - 1)), 12000);
        out({ type: 'status', msg: `WhatsApp disconnected (${code ?? 'no code'}: ${reason}); retrying in ${Math.ceil(delay / 1000)} seconds...` });
        reconnectTimer = setTimeout(() => {
          if (generation === startGeneration) {
            start().catch((e) => out({ type: 'error', msg: `cannot restart WhatsApp helper: ${e}` }));
          }
        }, delay);
      }
    }
  });
  sock.ev.on('messages.upsert', ({ messages }) => { for (const m of messages) emitMsg(m, false); });
}

function startOlder() {
  older.active = true; older.got = 0; cursor = anchor; requestOlder();
}

function requestOlder() {
  if (!sock?.user) { older.active = false; out({ type: 'older', status: 'not_connected' }); return; }
  if (!cursor) { older.active = false; out({ type: 'older', status: 'no_anchor' }); return; }
  sock.fetchMessageHistory(50, cursor.key, cursor.ts).catch((e) => { older.active = false; out({ type: 'error', msg: String(e) }); });
  clearTimeout(olderTimer);
  olderTimer = setTimeout(() => { if (older.active) { older.active = false; out({ type: 'older', status: older.got ? 'done' : 'timeout', got: older.got }); } }, 60000);
}

let seen = 0, dropped = 0;                                    // counters only - no content is kept
function emitMsg(m, hist, write = true) {
  if (!watch || m.key?.remoteJid !== watch) { dropped++; return null; }   // group-only filter: everything else is dropped
  seen++;
  const tsMs = num(m.messageTimestamp) * 1000;
  if (!anchor || tsMs > anchor.ts) {
    anchor = { key: { remoteJid: m.key.remoteJid, fromMe: !!m.key.fromMe, id: m.key.id, participant: m.key.participant }, ts: tsMs };
    saveAnchor();
    if (olderWait && !hist) { olderWait = false; setTimeout(startOlder, 1500); }   // first live group message = starting point
  }
  const g = m.message; if (!g) return;
  const text = g.conversation || g.extendedTextMessage?.text || g.imageMessage?.caption ||
    g.ephemeralMessage?.message?.extendedTextMessage?.text || g.ephemeralMessage?.message?.conversation || '';
  if (!text) return null;
  if (m.pushName) {
    for (const k of [m.key.participant, m.key.participantAlt, m.key.participantPn].map(clean).filter(Boolean)) {
      if (!names.has(k) || groupNames.get(k) !== names.get(k)) { if (!names.has(k)) names.set(k, m.pushName); groupNames.set(k, names.get(k)); }
    }
    persistGroup();
  }
  const event = {
    type: 'msg', id: m.key.id, ts: num(m.messageTimestamp), text, hist,
    sender: m.key.fromMe ? sock.user?.id : m.key.participant,
    senderAlt: m.key.participantAlt || m.key.participantPn || '',
    pushName: m.pushName || '',
  };
  if (write) out(event);
  return event;
}

const rl = readline.createInterface({ input: process.stdin });
rl.on('line', async (line) => {
  let c; try { c = JSON.parse(line); } catch { return; }
  try {
    if (c.cmd === 'watch') watch = c.group || '';
    else if (c.cmd === 'older') {
      if (older.active) return;                                  // already running - ignore repeated clicks
      older.floor = Number(c.floor) || 0;
      if (!anchor) { olderWait = true; out({ type: 'older', status: 'waiting' }); }
      else startOlder();
    }
    else if (c.cmd === 'groups') {
      const g = await sock.groupFetchAllParticipating();
      out({ type: 'groups', groups: Object.values(g).map((x) => ({ id: x.id, subject: x.subject })) });
    } else if (c.cmd === 'members') {
      const meta = await sock.groupMetadata(c.group);
      const members = meta.participants.map((p) => {
        const ids = [p.id, p.jid, p.lid, p.phoneNumber].filter(Boolean);     // p.jid = real phone JID when WhatsApp gives it
        const name = ids.map((i) => names.get(clean(i))).find(Boolean) || '';
        if (name) for (const i of ids) groupNames.set(clean(i), name);
        return { ids, name };
      });
      persistGroup();
      out({ type: 'members', members });
    } else if (c.cmd === 'logout') { if (sock?.user) await sock.logout(); else out({ type: 'logged_out' }); }
  } catch (e) { out({ type: 'error', msg: String(e) }); }
});
setInterval(() => out({ type: 'stats', seen, dropped, watching: !!watch }), 10000);
process.on('uncaughtException', (e) => out({ type: 'error', msg: String(e) }));
process.on('unhandledRejection', (e) => out({ type: 'error', msg: String(e) }));
start().catch((e) => out({ type: 'error', msg: `cannot start WhatsApp helper: ${e}` }));
