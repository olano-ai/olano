// The coingecko video: CoinGecko's keyless public MCP server (https://mcp.api.coingecko.com/mcp) added
// to an agent as a custom server during the take, a market snapshot, then the same brief put on the
// scheduler. The search_docs and execute steps are real calls to that server, the reply is built from
// what came back, and the task is made by the product's own scheduler_create tool (teams/coingecko.py).
// Team: Tidepool Notes, a newsletter about digital payments. Market facts only. Needs network.
export const start = '/agents/remy/mcp';
export const ready = (app) => app.getByText('Add MCP server').first().waitFor();
export const timezone = 'Asia/Singapore';
export const brand = 'Fresh data, every morning.';
export const keyboardWords = ['"Give"', 'Give', 'Put'];
export const captions = [
  { eyebrow: 'MCP', h1: 'CoinGecko,<br>by its address', p: "CoinGecko's public server, no key." },
  { eyebrow: 'Connect', h1: 'Two tools<br>appear', p: 'Search the docs, run a query.' },
  { eyebrow: 'Ask', h1: 'A market<br>snapshot', p: 'The docs first, then the query.' },
  { eyebrow: 'Answer', h1: 'Prices and<br>what is trending', p: 'As CoinGecko reported them.' },
  { eyebrow: 'Schedule', h1: 'Every weekday<br>at eight', p: 'One message puts it on the scheduler.' },
  { eyebrow: 'Scheduler', h1: 'Next to<br>the others', p: 'With its next run.' },
];
export const voice = [
  'CoinGecko runs a public MCP server that needs no key. Add it to Remy by its address.',
  'It connects, with two tools: one searches the docs, the other runs a query.',
  'Ask Remy for a market snapshot. Remy reads the docs first, then writes the query.',
  'The answer shows three coins with their daily change, and what is trending.',
  'Remy writes this for a newsletter, so ask for it every weekday at eight.',
  'The brief is on the scheduler now, next to the other tasks.',
  'Olano. Fresh data, every morning.',
];

export async function run({ app, S, sleep, tap, point, type, send, keyboardOff, scrollTo, nav, mark, PORTRAIT }) {
  mark('add');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(1000);
  await tap(app.getByText('Add MCP server').first(), { move: 800 });
  const d = app.locator('[role=dialog]').first();
  await d.getByText('Notion', { exact: true }).first().waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(600);
  await tap(d.getByRole('button', { name: 'Custom', exact: true }), { move: 700 });
  await sleep(400);
  await type(d.locator('input[placeholder="https://example.com/mcp"]'), 'https://mcp.api.coingecko.com/mcp', { cps: 18 });
  await keyboardOff();
  const transport = d.locator('select').first();
  await point(transport, { move: 500 });
  await S('press'); await sleep(120);
  await transport.selectOption('http'); await S('release');
  await sleep(700);

  mark('connect');
  await S('caption', 1);
  await tap(d.getByRole('button', { name: /^Connect/ }).last(), { move: 700, dispatch: true });
  await d.getByText('Connection ready').first().waitFor({ timeout: 90000 });
  await S('hide');
  await d.getByText('search_docs').first().scrollIntoViewIfNeeded().catch(() => {});
  await sleep(1800);
  await tap(d.getByRole('button', { name: 'Save server' }), { move: 700, dispatch: true });
  await app.locator('main').getByText('Connected', { exact: true }).first().waitFor({ timeout: 60000 });
  await S('hide');
  // A saved server reaches the running agent on its next reload: the tab's own Reload agent.
  const reload = app.getByRole('button', { name: 'Reload agent' }).first();
  await sleep(500);
  await tap(reload, { move: 800 });
  await sleep(700);
  await scrollTo(app.locator('main button[role=switch][aria-label="execute"]').first(), PORTRAIT ? 560 : 500, 1000).catch(() => {});
  await sleep(1300);

  mark('ask');
  await S('caption', 2);
  await nav('/chat/remy', { click: false });
  await app.locator('textarea').last().waitFor({ timeout: 30000 });
  await sleep(600);
  await send('Give me a market snapshot: Bitcoin, Ethereum and Solana with the 24 hour change, and what is trending.', { cps: 28 });
  await app.getByText('Calling search_docs').first().waitFor({ timeout: 90000 });
  await app.getByText('Calling execute').first().waitFor({ timeout: 120000 });
  await sleep(800);

  await app.getByText('snapshot from CoinGecko').first().waitFor({ timeout: 120000 });
  mark('answer');
  await S('caption', 3);
  await app.getByText('move by the minute').first().waitFor({ timeout: 60000 }).catch(() => {});
  await sleep(500);
  await scrollTo(app.getByText('snapshot from CoinGecko').last(), PORTRAIT ? 90 : 120, 1100).catch(() => {});
  await sleep(3800);

  mark('schedule');
  await S('caption', 4);
  await send('Put this on the schedule for 8 am every weekday.', { cps: 24 });
  await app.getByText('Calling scheduler_create').first().waitFor({ timeout: 60000 });
  await app.getByText('is on the scheduler').first().waitFor({ timeout: 60000 });
  await app.getByText('when you get in').first().waitFor({ timeout: 60000 }).catch(() => {});
  await sleep(2600);

  await nav('/scheduler');
  const row = app.locator('.card', { hasText: 'Morning market brief' }).last();
  await row.waitFor({ timeout: 30000 });
  mark('scheduler');
  await S('caption', 5);
  await sleep(600);
  await scrollTo(row, PORTRAIT ? 250 : 260, 900).catch(() => {});
  if (!PORTRAIT) await point(row.getByText('Morning market brief').first(), { move: 800, dx: 40, dy: 26 });
  await sleep(3600);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
