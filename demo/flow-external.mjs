// The external access video: an agent published as an MCP server. The home-wide switch (Config,
// External access), one agent's External access tab, an access token, then a real MCP client (the
// official `mcp` Python SDK, assets/mcp-ask.py) calls the agent's `ask_juno` tool with that token
// during the take; the terminal prints what that client printed, and the call is then on record in the
// Agent to Agent view. Team: Pinecrest Veterinary Clinic (teams/external.py); juno answers from a script.
// The endpoint, the token check, the tool call and its record are the product's own. serve.py stands in
// for one thing the launcher's gateway lacks: publishing the agent when its External access is saved
// (a real box does that when the agent restarts, as the toast says).
import { spawn } from 'node:child_process';
import path from 'node:path';
import { DIR, REPO } from './common.mjs';

export const start = '/config';
export const ready = (app) => app.getByText('MCP endpoint', { exact: true }).first().waitFor({ timeout: 60000 });
export const brand = 'Your agents, reachable from your tools.';
export const keyboardWords = ['"Booking"', 'Booking', 'Book'];
export const captions = [
  { eyebrow: 'External access', h1: 'Your agent,<br>an MCP server', p: 'Off until you switch it on.' },
  { eyebrow: 'Publish', h1: 'Choose<br>the agent', p: 'With a public description.' },
  { eyebrow: 'Token', h1: 'A token<br>per caller', p: 'Shown once. Only a hash is kept.' },
  { eyebrow: 'Call', h1: 'A real<br>MCP client', p: 'It finds <code>ask_juno</code> and calls it.' },
  { eyebrow: 'On record', h1: 'Every call,<br>kept', p: 'Who asked, and the answer.' },
];
export const voice = [
  'An Olano agent can answer other tools, over the Model Context Protocol. The endpoint is off until you switch it on.',
  'Then publish one agent, with a public description. That is all an outside caller gets to read about it.',
  'Each caller gets its own token, limited to the agents you name. It is shown once.',
  "Here, a real MCP client connects with that token, finds the tool ask juno, and gets the agent's answer.",
  'Every call is on record in the dashboard: who asked, what they asked, and the reply.',
  'Olano. Your agents, reachable from your tools.',
];

const QUESTION = 'Any free slots on Friday?';
const DESCRIPTION = 'Answers questions about opening hours and free appointment slots.';
const esc = (s) => s.replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c]);
function wrap(text, width) {
  const lines = [''];
  for (const word of text.split(' ')) {
    if (lines.at(-1) && (lines.at(-1) + ' ' + word).length > width) lines.push(word);
    else lines[lines.length - 1] += (lines.at(-1) ? ' ' : '') + word;
  }
  return lines;
}
/** One real call with the official MCP SDK; resolves with the lines the client printed. */
function ask(url, token) {
  return new Promise((resolve, reject) => {
    const p = spawn('uv', ['run', '--quiet', 'python', path.join(DIR, 'assets/mcp-ask.py'), 'ask_juno', QUESTION],
      { cwd: REPO, env: { ...process.env, OLANO_MCP_URL: url, OLANO_MCP_TOKEN: token }, stdio: ['ignore', 'pipe', 'pipe'] });
    let out = '', err = '';
    p.stdout.on('data', (d) => { out += d; });
    p.stderr.on('data', (d) => { err += d; });
    p.on('exit', (code) => (code === 0 ? resolve(out.trimEnd().split('\n')) : reject(new Error('mcp-ask.py failed\n' + err.slice(-1500)))));
  });
}

export async function run(c) {
  const { app, S, sleep, tap, point, type, keyboardOff, scroll, scrollTo, nav, term, mark, server, PORTRAIT } = c;

  // 0. the home-wide switch
  mark('door');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  const label = app.getByText('MCP endpoint', { exact: true }).first();
  await sleep(1200);
  await scrollTo(app.getByText('External access', { exact: true }).last(), PORTRAIT ? 110 : 120, 1600);
  await sleep(2400);
  await tap(label.locator('xpath=ancestor::*[.//button][1]').getByRole('switch'), { move: 800 });
  await sleep(900);
  await tap(app.getByRole('button', { name: 'Save changes' }), { move: 800 });
  await S('hide');
  await sleep(2600);

  // 1. publish one agent
  mark('publish');
  await S('caption', 1);
  await nav('/agents/juno/external', { click: false });
  const tab = app.getByTestId('external-access-tab');
  await tab.waitFor({ timeout: 30000 });
  await sleep(1500);
  const reach = tab.getByLabel('Reachable over MCP');
  await scrollTo(reach, PORTRAIT ? 300 : 380, 900).catch(() => {});
  await tap(reach, { move: 700 });
  await sleep(600);
  const desc = tab.getByTestId('ext-description');
  await scrollTo(desc, PORTRAIT ? 120 : 300, 800).catch(() => {});
  await type(desc, DESCRIPTION, { cps: 34 });
  await keyboardOff();
  await sleep(500);
  const save = tab.getByRole('button', { name: 'Save', exact: true });
  await scrollTo(save, PORTRAIT ? 380 : 420, 600).catch(() => {});
  await tap(save, { move: 600 });
  const endpoints = app.getByTestId('ext-endpoints');
  await endpoints.getByText('ask_juno').first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(700);
  await scrollTo(endpoints, PORTRAIT ? 90 : 110, 1200);
  if (!PORTRAIT) await point(endpoints.getByText('ask_juno').first(), { move: 800, dx: -70 });
  await sleep(3200);

  // 2. a token for this caller
  mark('token');
  await S('caption', 2);
  const mint = app.getByRole('button', { name: 'Mint a token' });
  await scrollTo(mint, PORTRAIT ? 200 : 240, 800).catch(() => {});
  await tap(mint, { move: 700 });
  const dlg = app.locator('.modal, [role=dialog]').last();
  await dlg.locator('input.input').first().waitFor({ timeout: 30000 });
  await sleep(500);
  await type(dlg.locator('input.input').first(), 'Booking site assistant', { cps: 22 });
  await type(dlg.locator('textarea'), 'juno', { cps: 14 });
  await keyboardOff();
  await sleep(500);
  await tap(dlg.getByRole('button', { name: 'Create token' }), { move: 600 });
  const minted = app.getByTestId('ext-minted-token');
  await minted.waitFor({ timeout: 30000 });
  const token = (await minted.textContent()).trim();
  if (!PORTRAIT) await point(minted, { move: 700 });
  await sleep(3800);
  await tap(app.getByRole('button', { name: 'Done' }), { move: 600 });
  await S('hide');
  await sleep(700);

  // 3. a real MCP client calls ask_juno with that token (the call runs now; its output is printed below)
  mark('call');
  await S('caption', 3);
  const status = await (await fetch(server.runtime_url + '/api/mcp-server/status')).json();
  const call = ask(status.endpoint, token).catch(() => ask(status.endpoint, token));
  await term.clear();
  await term.on();
  await sleep(700);
  await term.out('<span class="dim"># the endpoint and the new token are in OLANO_MCP_URL and OLANO_MCP_TOKEN</span>', { gap: 0 });
  await term.run(`python mcp-ask.py ask_juno "${QUESTION}"`, { cps: 22, pause: 400 });
  const lines = await call;
  const W = PORTRAIT ? 70 : 90;
  for (const line of lines) {
    if (/^(connected|tools|calling)/.test(line)) {
      await term.out(esc(line).replace(/^(connected:|tools:|calling)/, '<span class="ok">$1</span>').replace('ask_juno', '<mark>ask_juno</mark>'));
      await sleep(line.startsWith('calling') ? 2200 : 800);
      continue;
    }
    for (const row of wrap(line, W)) {              // the agent's answer
      await term.out('', { gap: 0 });
      let shown = '';
      for (const word of row.split(' ')) { shown += (shown ? ' ' : '') + word; await term.replace(`<span class="b">${esc(shown)}</span>`); await sleep(70); }
    }
  }
  await term.prompt();
  await sleep(4200);

  // 4. the call, on record
  mark('record');
  await S('caption', 4);
  await nav('/activity', { click: false });
  await term.off();
  const rail = app.getByTestId('a2a-rail');
  await rail.waitFor({ timeout: 30000 });
  await sleep(1200);
  await tap(app.locator('.a2a-seg', { hasText: 'External' }), { move: 800 });
  await sleep(900);
  await tap(rail.getByText('Booking site assistant').first(), { move: 700 });
  await app.getByText('Friday has three free slots').last().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(5200);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
