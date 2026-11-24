// Records a demo: the real dashboard (Vite dev server over a throwaway runtime, see serve.py) inside
// a browser window or an iPhone on stage.html, captured with the CDP screencast. The steps, captions
// and start page are in flow-<DEMO>.mjs. The page runs K times slower than real time (fake clock +
// CSS animation rate) so a small machine still gets enough frames; build.sh retimes them.
//   DEMO=tour LAYOUT=landscape|portrait node record.mjs
import fs from 'node:fs';
import path from 'node:path';
import { chromium, CHROME, prepare, startServer, APP_CSS, WORK, DIR, REPO } from './common.mjs';

const LAYOUT = process.env.LAYOUT || 'landscape';
const PORTRAIT = LAYOUT === 'portrait';
const DEMO = process.env.DEMO || 'tour';
const flow = await import(`./flow-${DEMO}.mjs`);
const OUT = path.join(WORK, `frames-${DEMO}-${LAYOUT}`);
fs.rmSync(OUT, { recursive: true, force: true }); fs.mkdirSync(OUT, { recursive: true });
const DSF = Number(process.env.DSF || 1.25);
const K = Number(process.env.SLOW || 3);

// The phone layout gets room for the status bar and the home indicator
const DEMO_CSS = APP_CSS + (PORTRAIT ? (flow.phoneCss ?? '') : (flow.desktopCss ?? '')) + (flow.css ?? '');

const server = flow.server === false ? null : await startServer(flow.team ?? DEMO, K);
const APP = server?.base_url ?? flow.app ?? 'http://127.0.0.1:1';     // flow.app: an Olano that is already running
const quit = async (code) => { server?.stop(); process.exit(code); };
process.on('SIGINT', () => quit(1)); process.on('SIGTERM', () => quit(1));
// A stuck setup ends the run; make.sh tries again.
const watchdog = setTimeout(() => { console.log('stuck before recording'); quit(3); }, 120000);
const b = await chromium.launch({ executablePath: CHROME, timeout: 30000, args: ['--disable-features=LocalNetworkAccessChecks,BlockInsecurePrivateNetworkRequests', ...(flow.chromeArgs ?? [])] });
const ctx = await b.newContext({ viewport: PORTRAIT ? { width: 1080, height: 1920 } : { width: 1920, height: 1080 },
  deviceScaleFactor: DSF, locale: 'en-US', timezoneId: flow.timezone ?? 'America/New_York', colorScheme: 'dark', hasTouch: PORTRAIT });
await prepare(ctx, { app: APP, appCss: DEMO_CSS });
if (flow.init) await flow.init(ctx, { server, layout: LAYOUT, K });
const STAGE = APP + '/__demo/stage.html';
await ctx.route('**/__demo/stage.html*', (r) => r.fulfill({ body: fs.readFileSync(path.join(DIR, 'stage.html')), contentType: 'text/html' }));
await ctx.route('**/__demo/icon.svg', (r) => r.fulfill({ path: path.join(REPO, 'olano/dashboard_2.0/public/favicon.svg'), contentType: 'image/svg+xml' }));

// Page time at 1/K speed (Date, timers, requestAnimationFrame, performance.now)
const T0 = flow.now ? new Date(flow.now) : new Date();
await ctx.clock.install({ time: T0 });
await ctx.clock.pauseAt(new Date(T0.getTime() + 1000));
const r0 = Date.now();
let fake = 0, driving = true;
const driver = (async () => {
  while (driving) {
    const d = Math.floor((Date.now() - r0) / K - fake);
    if (d > 0) { fake += d; await ctx.clock.runFor(d).catch(() => {}); }
    await new Promise((r) => setTimeout(r, 8));
  }
})();

const page = await ctx.newPage();
page.setDefaultTimeout(30000);
page.on('pageerror', (e) => console.log('pageerror', e.message));
await page.goto(STAGE + '?layout=' + LAYOUT);
await page.evaluate(([c, w]) => window.stage.setup(c, w), [flow.captions, flow.keyboardWords ?? []]);
if (flow.brand) await page.evaluate((l) => window.stage.brand(l), flow.brand);
if (flow.address) await page.evaluate((a) => window.stage.address(a), flow.address);
const app = page.frameLocator('#app');
let frame = null;
if (flow.start !== null && (server || flow.app)) {
  await page.evaluate((u) => { document.getElementById('app').src = u; }, APP + (flow.start ?? '/'));
  await flow.ready(app, { layout: LAYOUT }).catch(async (e) => { await page.screenshot({ path: path.join(WORK, 'not-ready.png') }); console.log('not ready: see', path.join(WORK, 'not-ready.png')); await quit(2); });
  frame = page.frames().find((f) => f !== page.mainFrame() && f.url().startsWith(APP));
}
await page.waitForTimeout(1500 * K);
await page.mouse.move(2, 2);
clearTimeout(watchdog);

const sleep = (ms) => page.waitForTimeout(ms * K);
const S = (fn, ...a) => page.evaluate(([fn, a]) => window.stage[fn](...a), [fn, a]);
// The app's box on the stage (once it has settled), and a point in it as fractions
const scr = await page.evaluate((portrait) => {
  const w = document.getElementById(portrait ? 'phone' : 'win'), sc = Number(getComputedStyle(w).getPropertyValue('--s'));
  return portrait ? { x: w.offsetLeft + 20 * sc, y: w.offsetTop + 72 * sc, width: 393 * sc, height: 780 * sc }
    : { x: w.offsetLeft, y: w.offsetTop + 40 * sc, width: 1200 * sc, height: 790 * sc };
}, PORTRAIT);
const P = (fx, fy) => [scr.x + scr.width * fx, scr.y + scr.height * fy];
async function center(loc) { const bb = await loc.boundingBox(); if (!bb) throw new Error('not visible: ' + loc); return [bb.x + bb.width / 2, bb.y + bb.height / 2]; }
/** The pointer glides to the element, presses and clicks it. */
async function tap(loc, { move = 650, hold = 130, after = 0, dispatch = PORTRAIT, at } = {}) {
  loc = loc.first();
  await loc.waitFor({ state: 'visible' });
  const [x, y] = at ?? await center(loc);
  await S('move', x, y, move);
  if (!PORTRAIT) await page.mouse.move(x, y);       // hover states, as a real mouse gives
  await sleep(90);
  await S('press');
  await sleep(hold);
  if (dispatch) await loc.dispatchEvent('click'); else await page.mouse.click(x, y);
  await S('release');
  if (after) await sleep(after);
}
/** Glide to an element without clicking (to point at it). */
async function point(loc, { move = 700, dx = 0, dy = 0 } = {}) {
  const [x, y] = await center(loc.first());
  await S('move', x + dx, y + dy, move);
  if (!PORTRAIT) await page.mouse.move(x + dx, y + dy);
}
/** Type into the focused field, key by key (the iOS keyboard shows on the phone). */
async function type(loc, text, { cps = 16, keyboard = PORTRAIT } = {}) {
  await tap(loc, { move: 600 });
  await loc.first().focus();
  if (keyboard) { await S('keyboard', true); await S('hide'); await sleep(450); }
  for (const ch of text) {
    if (keyboard) await S('key', ch);
    await page.keyboard.type(ch);
    await sleep((1000 / cps) * (0.7 + Math.random() * 0.6));
  }
  await sleep(250);
}
/** Type a chat message and press Send. */
async function send(text, opts = {}) {
  const box = app.locator('textarea').last();
  await type(box, text, opts);
  await tap(app.locator('button[aria-label="Send"]').last(), { move: 420 });
  await keyboardOff();
  await S('hide');
}
async function keyboardOff() { if (PORTRAIT) { await S('keyboard', false); await frame.evaluate(() => document.activeElement?.blur()); } }
const easeJs = 't < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2';
/** Scroll smoothly by dy: the nearest scrollable ancestor of `sel` (or the page). */
async function scroll(dy, ms = 1100, sel = null) {
  if (PORTRAIT) { await S('show', ...P(.6, .7)); await sleep(80); }
  await Promise.all([
    PORTRAIT ? S('swipe', -Math.sign(dy) * Math.min(Math.abs(dy) * 0.5, scr.height * 0.4), ms) : Promise.resolve(),
    frame.evaluate(([dy, ms, ez, sel]) => new Promise((res) => {
      const e = new Function('t', 'return ' + ez);
      let el = sel ? document.querySelector(sel) : null;
      const scrollable = (x) => x && x.scrollHeight > x.clientHeight + 4 && /auto|scroll/.test(getComputedStyle(x).overflowY);
      if (el) while (el && !scrollable(el)) el = el.parentElement;
      if (!el) el = [...document.querySelectorAll('main, main *, .app-shell *')].filter(scrollable).sort((a, b) => b.clientHeight * b.clientWidth - a.clientHeight * a.clientWidth)[0] || document.scrollingElement;
      const s = el.scrollTop, t0 = performance.now();
      const f = (t) => { const k = Math.min(1, (t - t0) / ms); el.scrollTop = s + dy * e(k); k < 1 ? requestAnimationFrame(f) : res(); };
      requestAnimationFrame(f);
    }), [dy, ms, easeJs, sel]),
  ]);
  if (PORTRAIT) { await S('release'); await S('hide'); }
}
/** Bring an element to `top` px below the top of the app (smoothly). */
async function scrollTo(loc, top = 140, ms = 1100) {
  const bb = await loc.first().boundingBox();
  const sc = scr.width / (PORTRAIT ? 393 : 1200);
  const dy = (bb.y - scr.y) / sc - top;
  const h = await loc.first().elementHandle();
  await frame.evaluate((el) => { el.setAttribute('data-demo-target', '1'); }, h);
  await scroll(dy, ms, '[data-demo-target]');
  await frame.evaluate((el) => el.removeAttribute('data-demo-target'), h);
}
/** Go to a dashboard page: by its sidebar link when one is showing, otherwise by the router. */
async function nav(path, { click = true } = {}) {
  const link = app.locator(`aside.sidebar a[href="${path}"]`).first();
  if (click && !PORTRAIT && await link.isVisible().catch(() => false)) { await tap(link); return; }
  await frame.evaluate((p) => { history.pushState({}, '', p); dispatchEvent(new PopStateEvent('popstate', { state: {} })); }, path);
}
/** Load a page of the app into the frame mid-flow (the tutorials start in the terminal). */
async function openApp(path, readyFn) {
  await page.evaluate((u) => { document.getElementById('app').src = u; }, APP + path);
  if (readyFn) await readyFn(app);
  frame = page.frames().find((f) => f !== page.mainFrame() && f.url().startsWith(APP));
}
// --- the terminal (installation tutorials) ---
const term = {
  on: () => S('term', true), off: () => S('term', false), clear: () => S('termClear'),
  /** Types a command at a fresh prompt. */
  async run(cmd, { cps = 22, pause = 500 } = {}) {
    await S('termPrompt');
    await sleep(350);
    for (const ch of cmd) { await S('termKey', ch); await sleep((1000 / cps) * (0.6 + Math.random() * 0.8)); }
    await sleep(pause);
  },
  /** Prints output lines, `gap` ms apart. */
  async out(lines, { gap = 90 } = {}) { for (const l of [].concat(lines)) { await S('termPrint', l); if (gap) await sleep(gap); } },
  replace: (l) => S('termReplace', l), prompt: () => S('termPrompt'),
};

// --- record ---
const cdp = await ctx.newCDPSession(page);
await cdp.send('Animation.enable');
await cdp.send('Animation.setPlaybackRate', { playbackRate: 1 / K });
const index = [];
let n = 0;
cdp.on('Page.screencastFrame', (f) => {
  const file = `f${String(n++).padStart(5, '0')}.jpg`;
  fs.writeFileSync(path.join(OUT, file), Buffer.from(f.data, 'base64'));
  index.push({ file, ts: f.metadata.timestamp });
  cdp.send('Page.screencastFrameAck', { sessionId: f.sessionId }).catch(() => {});
});
await cdp.send('Page.startScreencast', { format: 'jpeg', quality: 94, maxWidth: 1920 * DSF, maxHeight: 1920 * DSF, everyNthFrame: 1 });
const t0 = Date.now();
const marks = {};   // when each step starts (video seconds); mix.mjs times the voiceover to these
const mark = (l) => { marks[l] = (Date.now() - t0) / 1000 / K; console.log(marks[l].toFixed(1).padStart(5), l); };
await sleep(400);

let failed = null;
try {
  await flow.run({ page, app, get frame() { return frame; }, S, sleep, P, tap, point, type, send, keyboardOff, scroll, scrollTo, nav, openApp, term, mark, APP, K, server, PORTRAIT, LAYOUT });
} catch (e) { failed = e; await page.screenshot({ path: path.join(WORK, 'failed.png') }); console.log('flow failed: see', path.join(WORK, 'failed.png'), '\n', e.message); }
mark('stop');
await cdp.send('Page.stopScreencast');
await sleep(300);
fs.writeFileSync(path.join(OUT, 'index.json'), JSON.stringify(index.map((f) => ({ ...f, ts: f.ts / K }))));
fs.writeFileSync(path.join(OUT, 'marks.json'), JSON.stringify(marks));
driving = false; await driver;
console.log('frames', index.length, 'video fps ≈', (index.length / ((index.at(-1).ts - index[0].ts) / K)).toFixed(0));
await b.close();
await quit(failed ? 4 : 0);
