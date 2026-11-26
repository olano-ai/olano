// The Playwright video: Microsoft's Playwright MCP server, a local (stdio) server started with npx,
// added from the MCP directory with --headless, its tools listed, then a small web task in chat. Cass's
// tool steps are real calls to that server, which drives its own browser (not Olano's built-in browser
// tools), and the reply is built from the page snapshot it returned (teams/playwright.py, which also
// says how the server reaches the web from this machine). Team: Halyard Software.
export const start = '/agents/cass/mcp';
export const ready = (app) => app.getByText('Add MCP server').first().waitFor({ timeout: 60000 });
export const brand = 'Bring your own MCP servers.';
export const keyboardWords = ['"Open"', 'Open', 'Op'];
export const captions = [
  { eyebrow: 'Local MCP', h1: 'Playwright<br>MCP, via npx', p: 'Microsoft\'s server, run on this machine.' },
  { eyebrow: 'Tools', h1: 'Its own<br>browser tools', p: 'Not Olano\'s browser: the server\'s.' },
  { eyebrow: 'Ask', h1: 'Check a<br>release', p: 'On PyPI, in the server\'s browser.' },
  { eyebrow: 'Answer', h1: 'Read off<br>the page', p: 'From the snapshot the server returned.' },
  { eyebrow: 'Verbose', h1: 'Every step,<br>in full', p: 'What the server sent back.' },
];
export const voice = [
  "Microsoft's Playwright MCP server runs locally, started with npx. Pick it from the directory and add one argument: headless.",
  'Cass connects, and the server lists its own browser tools. This is the Playwright server, not the browser built into Olano.',
  'Ask Cass to check a package release on PyPI. Cass opens the page, clicks Release history, and reads it.',
  'The answer comes from the page itself: the latest stable release, and any newer pre-releases.',
  'In verbose mode, each step shows what the Playwright server sent back.',
  'Olano. Bring your own MCP servers.',
];

export async function run({ page, app, S, sleep, tap, type, send, keyboardOff, scroll, scrollTo, nav, mark, PORTRAIT, PHONE }) {
  // 0. the directory entry: npx @playwright/mcp@latest, plus --headless
  mark('mcp');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(1300);
  await tap(app.getByText('Add MCP server').first(), { move: 700 });
  const d = app.locator('[role=dialog]').first();
  await d.getByText('Connect a service').first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(800);
  await type(d.locator('input[placeholder^="Search"]'), 'playwright', { cps: 14 });
  await keyboardOff();
  const preset = d.getByRole('button', { name: /^Playwright/ }).first();
  await preset.waitFor({ timeout: 60000 });
  await sleep(400);
  await tap(preset, { move: 500 });
  const boxes = d.locator('textarea');
  let args = null;
  for (let i = 0; i < await boxes.count(); i++) {
    if ((await boxes.nth(i).inputValue()).includes('@playwright/mcp')) { args = boxes.nth(i); break; }
  }
  if (!args) throw new Error('no Arguments field with @playwright/mcp');
  await args.scrollIntoViewIfNeeded().catch(() => {});
  await sleep(900);
  await tap(args, { move: 600 });
  await args.focus();
  await page.keyboard.press('Control+End');
  if (PHONE) { await S('keyboard', true); await S('hide'); await sleep(450); }
  for (const ch of '\n--headless') {
    if (PHONE && ch !== '\n') await S('key', ch);
    if (ch === '\n') await page.keyboard.press('Enter'); else await page.keyboard.type(ch);
    await sleep(70);
  }
  await keyboardOff();
  await S('hide');
  await sleep(1300);

  // 1. connect: the server starts and lists its tools; saved, then reloaded into Cass
  mark('tools');
  await S('caption', 1);
  await tap(d.getByRole('button', { name: /^Connect/ }).last(), { move: 700 });
  await d.getByText('Connection ready').first().waitFor({ timeout: 120000 });
  await S('hide');
  await d.getByText('browser_navigate', { exact: true }).first().scrollIntoViewIfNeeded().catch(() => {});
  await sleep(2400);
  await tap(d.getByRole('button', { name: 'Save server' }), { move: 700 });
  await app.locator('main').getByText('Connected', { exact: true }).first().waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(600);
  await tap(app.getByRole('button', { name: 'Reload agent' }), { move: 700 });
  await app.getByText('Reloaded MCP tools').first().waitFor({ timeout: 120000 });
  await S('hide');
  await sleep(1300);

  // 2. the task
  mark('ask');
  await S('caption', 2);
  await nav('/chat/cass', { click: false });
  await app.locator('textarea').last().waitFor({ timeout: 30000 });
  await sleep(700);
  await send('Open httpx on PyPI and check its latest release.', { cps: 22 });
  await app.getByText('Calling browser_navigate').first().waitFor({ timeout: 90000 });
  await app.getByText('Calling browser_click').first().waitFor({ timeout: 120000 });
  await sleep(1000);

  // 3. the answer, built from the snapshot
  await app.getByText('on PyPI is', { exact: false }).first().waitFor({ timeout: 180000 });
  mark('answer');
  await S('caption', 3);
  await app.getByText('pypi.org/project/httpx/#history', { exact: false }).first().waitFor({ timeout: 120000 });
  await sleep(700);
  await scrollTo(app.locator('.tool-activity').first(), PORTRAIT ? 110 : 150, 1200).catch(() => {});
  await sleep(3800);

  // 4. verbose: what each call returned
  mark('verbose');
  await S('caption', 4);
  await tap(app.getByText('Normal', { exact: true }).first(), { move: 700 });
  await app.getByText('Verbose', { exact: true }).first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(600);
  await scrollTo(app.getByText('Ran Playwright code').first(), PORTRAIT ? 120 : 140, 1200).catch(() => {});
  await sleep(2200);
  await scroll(PORTRAIT ? 380 : 340, 1500);
  await sleep(1600);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
