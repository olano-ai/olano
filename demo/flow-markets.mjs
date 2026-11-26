// The markets video: pdata's public MCP server (https://api.pdata.world/mcp), which gathers prediction
// markets including Polymarket and Kalshi, added as a custom server with no account; then where the two
// markets price the same question differently (compare_pairs) and what moved most today (top_movers).
// Each tool step is a real call to that server and the reply is built from what came back
// (teams/markets.py). Market prices only, never a suggestion to trade. Team: Second Look, a newsletter.
export const start = '/agents/ozzie/mcp';
export const ready = (app) => app.getByText('Add MCP server').first().waitFor({ timeout: 60000 });
export const brand = 'Two markets, one question.';
export const keyboardWords = ['"Where', 'Where', 'What'];
export const captions = [
  { eyebrow: 'MCP', h1: 'pdata,<br>by its address', p: 'Many prediction markets, one server.' },
  { eyebrow: 'Tools', h1: 'Search, compare,<br>movers', p: 'Public data. No account, no key.' },
  { eyebrow: 'Ask', h1: 'Where do they<br>disagree?', p: 'Polymarket and Kalshi, side by side.' },
  { eyebrow: 'Answer', h1: 'Same question,<br>two prices', p: 'Biggest gap first.' },
  { eyebrow: 'Ask again', h1: 'What moved<br>most today?', p: 'The biggest swings in a day.' },
];
export const voice = [
  "Ozzie finds stories for a newsletter about forecasts. Add pdata's public MCP server, which gathers many prediction markets.",
  'The server connects with its tools: search, compare, and the biggest movers of the day.',
  'Ask Ozzie where Polymarket and Kalshi disagree. Ozzie compares the questions both markets list.',
  'The same question, priced differently on two markets, side by side, biggest gap first.',
  'Then ask what moved most today. Ozzie lists the biggest swings of the last day.',
  'Olano. Two markets, one question.',
];

export async function run({ app, S, sleep, tap, point, type, send, keyboardOff, scroll, scrollTo, nav, mark, PORTRAIT }) {
  // 0. a custom server: the address and the transport
  mark('mcp');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(1300);
  await tap(app.getByText('Add MCP server').first(), { move: 700 });
  const d = app.locator('[role=dialog]').first();
  await d.getByText('Connect a service').first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(700);
  await tap(d.getByText('Custom', { exact: true }).first(), { move: 600 });
  await sleep(400);
  await type(d.locator('input[placeholder="https://example.com/mcp"]'), 'https://api.pdata.world/mcp', { cps: 18 });
  await keyboardOff();
  const transport = d.locator('select').first();
  await sleep(300);
  await S('move', ...(await transport.boundingBox().then((b) => [b.x + b.width / 2, b.y + b.height / 2])), 600);
  await sleep(600);
  await S('press'); await transport.selectOption('http'); await S('release');
  await S('hide');
  await sleep(1000);

  // 1. connect: the tool list; saved and reloaded
  mark('tools');
  await S('caption', 1);
  await tap(d.getByRole('button', { name: /^Connect/ }).last(), { move: 700 });
  await d.getByText('Connection ready').first().waitFor({ timeout: 90000 });
  await S('hide');
  await d.locator('code', { hasText: /^compare_pairs$/ }).first().scrollIntoViewIfNeeded().catch(() => {});
  await sleep(2600);
  await tap(d.getByRole('button', { name: 'Save server' }), { move: 700 });
  await app.locator('main').getByText('Connected', { exact: true }).first().waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(500);
  await tap(app.getByRole('button', { name: 'Reload agent' }).first(), { move: 700 });
  await app.getByText('Reloaded MCP tools').first().waitFor({ timeout: 90000 });
  await S('hide');
  await sleep(1000);

  // 2. the question
  mark('ask');
  await S('caption', 2);
  await nav('/chat/ozzie', { click: false });
  await app.locator('textarea').last().waitFor({ timeout: 30000 });
  await sleep(600);
  await send('Where do Polymarket and Kalshi disagree right now?', { cps: PORTRAIT ? 30 : 22 });
  await app.getByText('Calling compare_pairs').first().waitFor({ timeout: 90000 });
  await sleep(1200);

  // 3. the answer
  await app.getByText('priced on both markets', { exact: false }).first().waitFor({ timeout: 180000 });
  mark('answer');
  await S('caption', 3);
  await app.getByText('they move all day', { exact: false }).first().waitFor({ timeout: 120000 });
  await sleep(500);
  await scrollTo(app.getByText('priced on both markets', { exact: false }).first(), PORTRAIT ? 90 : 120, 1100).catch(() => {});
  await sleep(PORTRAIT ? 2400 : 3000);
  if (PORTRAIT) { await scroll(320, 1300); await sleep(1800); }
  else { await point(app.getByText('The widest gap', { exact: false }).first(), { move: 800 }); await sleep(1800); }

  // 4. what moved
  mark('movers');
  await S('caption', 4);
  await send('What moved most today?', { cps: 20 });
  await app.getByText('Calling top_movers').first().waitFor({ timeout: 120000 });
  await app.getByText('The biggest swing', { exact: false }).first().waitFor({ timeout: 180000 });
  await sleep(500);
  await scrollTo(app.getByText('biggest moves of the last 24 hours', { exact: false }).first(), PORTRAIT ? 90 : 120, 1100).catch(() => {});
  await sleep(PORTRAIT ? 3600 : 4200);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
