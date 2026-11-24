// The mobile app video: the real screens of the iPhone and Android app (mobile/, Ionic React on
// Capacitor), in the stage's iPhone in both layouts (device: 'phone'). The app is built from source
// and served the way its end-to-end test serves it (tests/e2e/test_mobile_app.py): the production
// bundle behind `vite preview`, with /api proxied to the throwaway runtime that serve.py starts. In a
// browser the screens talk to that API with fetch(); on a phone a native plugin carries the same calls.
// Team: Tidewater Cycles, a bike shop (teams/mobile.py). The Cortex items and the approval in the Inbox
// are made with the product's own code; the agents answer from a script.
import { spawn, execFileSync } from 'node:child_process';
import path from 'node:path';
import { REPO, WORK } from './common.mjs';

const PORT = Number(process.env.DEMO_MOBILE_PORT || 5391);
export const team = 'mobile';
export const device = 'phone';                      // the iPhone in landscape too
export const app = `http://127.0.0.1:${PORT}`;      // the app's own server; serve.py is its deployment
export const start = '/';
// Ionic picks its iOS look from the user agent, as on a real iPhone
export const userAgent = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1';
export const brand = 'Your agents, in your pocket.';
export const keyboardWords = ['"Is"', 'Is', 'It'];
export const captions = [
  { eyebrow: 'Mobile app', h1: 'Olano in<br>your pocket', p: 'On iPhone and Android.' },
  { eyebrow: 'Chat', h1: 'Ask from<br>anywhere', p: 'Each step, then the answer.' },
  { eyebrow: 'Cortex', h1: 'Their ideas,<br>rated', p: 'Open one to see why.' },
  { eyebrow: 'Inbox', h1: 'Approve<br>on the go', p: 'It waits until you decide.' },
  { eyebrow: 'Settings', h1: 'Your own<br>Olano', p: 'Account, tasks, files, appearance.' },
];
export const voice = [
  'Olano has an app, for iPhone and Android. Your whole team of agents is in it.',
  'Ask a question the way you would at your desk. You see each step the agent takes, and then the answer.',
  'Cortex is where agents keep their own ideas, each one rated. Open one to read the reasoning.',
  'When an agent needs your go-ahead, the request waits in the Inbox. Approve it from wherever you are.',
  'Settings shows the Olano you are connected to, with scheduled tasks and files one tap away.',
  'Olano. Your agents, in your pocket.',
];

// A phone's status bar and home indicator. A desktop browser reports no safe-area insets; Ionic reads
// these variables first (the app's own test sets the same two).
const INSETS = `
if (!location.pathname.startsWith('/__demo/')) document.addEventListener('DOMContentLoaded', () => {
  const style = document.createElement('style');
  style.id = 'demo-insets';
  style.textContent = 'html { --safe-area-inset-top: 59px; --safe-area-inset-bottom: 34px; }';
  document.head.appendChild(style);
});`;

export async function init(ctx, { server }) {
  const cwd = path.join(REPO, 'mobile'), out = path.join(WORK, 'mobile-www');
  const env = { ...process.env, OLANO_DEV_RUNTIME: server.runtime_url, OLANO_DEV_PORT: String(PORT), BROWSER: 'none' };
  execFileSync('npx', ['vite', 'build', '--base', '/', '--outDir', out, '--emptyOutDir'], { cwd, env, stdio: 'ignore' });
  const p = spawn('npx', ['vite', 'preview', '--host', '127.0.0.1', '--outDir', out], { cwd, env, stdio: 'ignore', detached: true });
  process.on('exit', () => { try { process.kill(-p.pid, 'SIGTERM'); } catch {} });
  let up = false;
  for (let i = 0; i < 200 && !up; i++) { try { up = (await fetch(app)).ok; } catch {} if (!up) await new Promise((r) => setTimeout(r, 150)); }
  if (!up) throw new Error('the mobile app did not start on ' + app);
  await ctx.addInitScript(INSETS);
  // The app's public remote config comes from Olano Cloud; a take reaches nothing outside this machine
  // and the app draws from its bundled copy.
  await ctx.route('https://cloud.olano.ai/**', (r) => r.abort());
}

const active = (a) => a.locator('.ion-page:not(.ion-page-hidden):visible').last();

// Before the take: connect the app to its deployment, as a person does once
export const ready = async (a) => {
  await a.getByText('Connect to your Olano').waitFor({ timeout: 60000 });
  await a.locator('ion-input input').fill(app);
  await a.getByRole('button', { name: 'Continue' }).click();
  await active(a).getByText('milo', { exact: true }).waitFor({ timeout: 30000 });
};

export async function run(c) {
  const { app: a, S, sleep, P, tap, type, keyboardOff, mark } = c;
  const page = () => active(a);
  const real = { dispatch: false };                  // real taps: Ionic listens to the pointer
  /** Scroll the screen's content by dy with a fingertip swipe. */
  async function swipe(dy, ms = 1200) {
    await S('show', ...P(.62, .66));
    await sleep(80);
    await Promise.all([
      S('swipe', -Math.sign(dy) * Math.min(Math.abs(dy) * 0.6, 320), ms),
      c.frame.evaluate(async ([dy, ms]) => {
        const pg = [...document.querySelectorAll('.ion-page:not(.ion-page-hidden)')].filter((e) => e.offsetParent && e.querySelector(':scope > ion-content')).at(-1);
        const el = await pg.querySelector(':scope > ion-content').getScrollElement();
        const s = el.scrollTop, t0 = performance.now(), e = (t) => (t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
        await new Promise((res) => { const f = (t) => { const k = Math.min(1, (t - t0) / ms); el.scrollTop = s + dy * e(k); k < 1 ? requestAnimationFrame(f) : res(); }; requestAnimationFrame(f); });
      }, [dy, ms]),
    ]);
    await S('release');
    await S('hide');
  }
  const inset = (px) => c.frame.evaluate((px) => { document.getElementById('demo-insets').textContent = `html { --safe-area-inset-top: 59px; --safe-area-inset-bottom: ${px}px; }`; }, px);

  await S('fullScreen');
  await S('time', new Date().toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone: 'America/New_York' }).replace(/\s?[AP]M/, ''));

  // 0. the app: every agent, with its latest conversation
  mark('app');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(6200);

  // 1. a question, the steps, the answer
  mark('ask');
  await S('caption', 1);
  await tap(page().getByText('milo', { exact: true }), { ...real, move: 800 });
  const box = a.locator('ion-textarea textarea').last();
  await box.waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(1300);
  const typed = type(box, 'Is the Halden e-bike ready?', { cps: 15 });
  await sleep(800);
  await inset(0);                                   // the keyboard covers the home indicator
  await typed;
  await tap(page().getByRole('button', { name: 'Send' }), { ...real, move: 420 });
  await keyboardOff();
  await inset(34);
  await S('hide');
  await page().getByText('waiting for your approval').waitFor({ timeout: 120000 });
  await sleep(2800);
  await tap(page().locator('ion-back-button'), { ...real, move: 700 });
  const cortexTab = a.locator("ion-tab-button[tab='cortex']");
  await cortexTab.waitFor();
  await sleep(500);

  // 2. Cortex: what the agents came up with, rated
  mark('cortex');
  await S('caption', 2);
  await tap(cortexTab, { ...real, move: 600 });
  const idea = page().getByText('Text customers when a rep').first();
  await idea.waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(2600);
  await tap(idea, { ...real, move: 700 });
  await page().getByText('Why now').waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(1500);
  await swipe(170, 1300);
  await sleep(2400);
  await tap(page().locator('ion-back-button'), { ...real, move: 700 });
  const inboxTab = a.locator("ion-tab-button[tab='inbox']");
  await inboxTab.waitFor();
  await sleep(500);

  // 3. the Inbox: an approval that waited for a person
  mark('inbox');
  await S('caption', 3);
  await tap(inboxTab, { ...real, move: 600 });
  const approve = page().getByRole('button', { name: 'Approve' });
  await approve.waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(3200);
  await tap(approve, { ...real, move: 800 });
  await page().getByText('Nothing to approve').waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(2800);

  // 4. Settings: the deployment the app is connected to
  mark('settings');
  await S('caption', 4);
  await tap(a.locator("ion-tab-button[tab='settings']"), { ...real, move: 600 });
  await page().getByText('Scheduled Tasks').waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(6200);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
