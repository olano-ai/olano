// The snack video: the Open Food Facts MCP server (@cyanheads/openfoodfacts-mcp-server, a published npm
// package run locally with npx; Open Food Facts needs no account or key) added as a custom stdio server
// during the take, then two breakfast cereals compared by the barcodes on their boxes. The off_* steps
// are real calls to that server and the reply is built from what came back (teams/snack.py). Label
// facts only, no dietary advice. Team: the Okafor household. Needs network (and Node 24, see the team).
export const start = '/agents/basil/mcp';
export const ready = (app) => app.getByText('Add MCP server').first().waitFor({ timeout: 60000 });
export const brand = 'Bring your own MCP servers.';
export const keyboardWords = ['"Is"', 'Is', 'My'];
export const captions = [
  { eyebrow: 'MCP', h1: 'Open Food<br>Facts', p: 'A local MCP server, run with npx.' },
  { eyebrow: 'Connect', h1: 'Its tools<br>appear', p: 'Look up, search, compare.' },
  { eyebrow: 'Ask', h1: 'Is this cereal<br>healthy?', p: 'Two boxes, by their barcodes.' },
  { eyebrow: 'Answer', h1: 'The label,<br>decoded', p: 'Scores, sugar and additives.' },
  { eyebrow: 'Verbose', h1: 'Straight from<br>the database', p: 'What the server sent back.' },
];
export const voice = [
  'Add the Open Food Facts MCP server to Basil. It runs on this machine with npx, no key needed.',
  'It connects, and its tools appear: look up, search and compare.',
  'Ask Basil whether the cereal your kid wants is actually healthy, with the barcodes from both boxes.',
  'Basil compares them: Nutri-Score, processing level, sugar per bowl and additives.',
  'In verbose mode, you can see what the server sent back for each call.',
  'Olano. Bring your own MCP servers.',
];

export async function run({ page, app, S, sleep, tap, point, type, send, keyboardOff, scrollTo, scroll, mark, nav, PORTRAIT, PHONE }) {
  // 0. a custom stdio server: npx @cyanheads/openfoodfacts-mcp-server
  mark('add');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(900);
  await tap(app.getByText('Add MCP server').first(), { move: 800 });
  const d = app.locator('[role=dialog]').first();
  await d.getByText('Notion', { exact: true }).first().waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(400);
  await tap(d.getByRole('button', { name: 'Custom', exact: true }), { move: 700 });
  await sleep(300);
  const transport = d.locator('select').first();
  await point(transport, { move: 500 });
  await S('press'); await sleep(120);
  await transport.selectOption('stdio'); await S('release');
  await sleep(400);
  await type(d.locator('input[placeholder="time"]'), 'openfoodfacts', { cps: 20 });
  await type(d.locator('input[placeholder="uvx"]'), 'npx', { cps: 14 });
  const args = d.locator('textarea[placeholder^="-y"]').first();
  await tap(args, { move: 500 });
  await args.focus();
  if (PHONE) { await S('keyboard', true); await S('hide'); await sleep(450); }
  for (const ch of '-y\n@cyanheads/openfoodfacts-mcp-server') {
    if (PHONE && ch !== '\n') await S('key', ch);
    if (ch === '\n') await page.keyboard.press('Enter'); else await page.keyboard.type(ch);
    await sleep(45);
  }
  await keyboardOff();
  await S('hide');
  await sleep(500);

  // 1. connect: the server starts and lists its tools; saved, then reloaded into Basil
  mark('connect');
  await S('caption', 1);
  await tap(d.getByRole('button', { name: /^Connect/ }).last(), { move: 700, dispatch: true });
  await d.getByText('Connection ready').first().waitFor({ timeout: 150000 });
  await S('hide');
  await d.getByText('off_compare_products').first().scrollIntoViewIfNeeded().catch(() => {});
  await sleep(2000);
  await tap(d.getByRole('button', { name: 'Save server' }), { move: 700, dispatch: true });
  await app.locator('main').getByText('Connected', { exact: true }).first().waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(400);
  await tap(app.getByRole('button', { name: 'Reload agent' }).first(), { move: 800 });
  await app.getByText('Reloaded MCP tools').first().waitFor({ timeout: 150000 }).catch(() => {});
  await S('hide');
  await scrollTo(app.locator('main').getByText('off_compare_products', { exact: true }).first(), PORTRAIT ? 420 : 380, 1000).catch(() => {});
  await sleep(1300);

  // 2. the question
  mark('ask');
  await S('caption', 2);
  await nav('/chat/basil', { click: false });
  await app.locator('textarea').last().waitFor({ timeout: 30000 });
  await sleep(500);
  await send('Is this cereal actually healthy? My kid wants Froot Loops (0064100595729) instead of our Cheerios (0016000275263).', { cps: PORTRAIT ? 44 : 34 });
  await app.getByText('Calling off_compare_products').first().waitFor({ timeout: 120000 }).catch(() => {});
  await sleep(600);

  // 3. the answer
  await app.getByText('what the two labels say', { exact: false }).first().waitFor({ timeout: 180000 });
  mark('answer');
  await S('caption', 3);
  await app.getByText('not dietary advice', { exact: false }).first().waitFor({ timeout: 60000 });
  await sleep(400);
  await scrollTo(app.getByText('what the two labels say', { exact: false }).last(), PORTRAIT ? 70 : 90, 1100).catch(() => {});
  await sleep(PORTRAIT ? 3800 : 4400);
  if (PORTRAIT) { await scroll(330, 1200); await sleep(2400); }

  // 4. verbose: what the server returned for the comparison
  mark('source');
  await S('caption', 4);
  await tap(app.getByText('Normal', { exact: true }).first(), { move: 700 });
  await app.getByText('Verbose', { exact: true }).first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(500);
  await scrollTo(app.getByText('Product Comparison', { exact: false }).first(), PORTRAIT ? 120 : 140, 1200).catch(() => {});
  await sleep(PORTRAIT ? 3400 : 3800);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
