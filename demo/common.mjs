// Shared setup for the demo videos: the work folder, the browser, the demo dashboard and the fonts.
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';

export const DIR = path.dirname(new URL(import.meta.url).pathname);
export const REPO = path.dirname(DIR);
export const WORK = process.env.DEMO_WORK || path.join(DIR, '.work');
const req = createRequire(path.join(WORK, 'noop.js'));
let pw;
try { pw = req('playwright-core'); } catch { pw = createRequire(import.meta.url)('playwright'); }
export const { chromium } = pw;
export const CHROME = process.env.CHROME || undefined;       // default: Playwright's own Chromium
const font = (f) => fs.readFileSync(path.join(WORK, 'node_modules/@fontsource-variable', f));
const INTER = font('inter/files/inter-latin-opsz-normal.woff2');
const MONO = font('jetbrains-mono/files/jetbrains-mono-latin-wght-normal.woff2');

// Inter and JetBrains Mono are the dashboard's own first choices; this machine doesn't have them.
export const APP_CSS = `
@font-face { font-family: 'Inter'; src: url(/__demo/inter.woff2) format('woff2'); font-weight: 100 900; font-display: block; }
@font-face { font-family: 'JetBrains Mono'; src: url(/__demo/mono.woff2) format('woff2'); font-weight: 100 800; font-display: block; }
::-webkit-scrollbar { display: none; }
*:focus-visible { outline: none !important; }
`;

/** Starts the throwaway dashboard for a demo (serve.py) and resolves once it is ready. */
export function startServer(demo, slow) {
  return new Promise((resolve, reject) => {
    const p = spawn('uv', ['run', 'python', path.join(DIR, 'serve.py'), demo], {
      cwd: REPO, env: { ...process.env, DEMO_SLOW: String(slow) }, stdio: ['ignore', 'pipe', 'pipe'], detached: true });
    let out = '', err = '';
    const stop = () => { try { process.kill(-p.pid, 'SIGTERM'); } catch {} };
    p.stderr.on('data', (d) => { err += d; if (process.env.DEMO_DEBUG) process.stderr.write(d); });
    p.stdout.on('data', (d) => {
      out += d;
      const line = out.split('\n').find((l) => l.startsWith('{'));
      if (line) resolve({ ...JSON.parse(line), stop });
    });
    p.on('exit', (c) => reject(new Error(`serve.py exited (${c})\n${err.slice(-2000)}`)));
    setTimeout(() => reject(new Error('serve.py did not start in 120 s\n' + err.slice(-2000))), 120000);
  });
}

export async function prepare(ctx, { app, appCss = APP_CSS } = {}) {
  await ctx.route('**/__demo/inter.woff2', (r) => r.fulfill({ body: INTER, contentType: 'font/woff2' }));
  await ctx.route('**/__demo/mono.woff2', (r) => r.fulfill({ body: MONO, contentType: 'font/woff2' }));
  await ctx.addInitScript(({ css, port }) => {
    if (location.port !== port || location.pathname.startsWith('/__demo/')) return;
    const add = () => { const s = document.createElement('style'); s.textContent = css; document.head.appendChild(s); };
    if (document.head) add(); else document.addEventListener('DOMContentLoaded', add);
  }, { css: appCss, port: app ? new URL(app).port : '-' });
}
