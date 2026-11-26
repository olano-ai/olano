// The kiwi video: Kiwi.com's public flight search MCP server (https://mcp.kiwi.com, no account) added
// to an agent as a custom server during the take, then a real trip. Every search-flight step in the
// chat is a real call to that server, and the replies are built from what it returned (teams/kiwi.py).
// Team: Paper Kite Games, a board-game studio in Singapore planning a team offsite. Needs network.
export const start = '/agents/sora/mcp';
export const ready = (app) => app.getByText('Add MCP server').first().waitFor();
export const brand = 'Any MCP server, by its address.';
export const keyboardWords = ['"Find"', 'Find', 'Fly'];
export const captions = [
  { eyebrow: 'MCP', h1: 'Add a server<br>by its address', p: "Kiwi.com's public flight search." },
  { eyebrow: 'Connect', h1: 'No account,<br>two tools', p: 'Sora keeps just the search.' },
  { eyebrow: 'Ask', h1: 'A real<br>trip', p: 'Four people, Singapore to Tokyo.' },
  { eyebrow: 'Answer', h1: 'Options to<br>compare', p: 'Times, prices and booking links.' },
  { eyebrow: 'Follow-up', h1: 'A day<br>either way', p: 'The same search, with flexible dates.' },
];
export const voice = [
  'Kiwi.com runs a public MCP server for flight search. Add it to Sora as a custom server, by its address.',
  'It connects with no account, and two tools appear. Sora only needs the search.',
  'Now a real trip: four people from Singapore to Tokyo in January. Sora calls the search.',
  'The answer compares a few options, with times, prices and the booking links from Kiwi.com.',
  'Would a day earlier or later cost less? Sora searches again, with flexible dates.',
  'Olano. Any MCP server, by its address.',
];

export async function run({ app, S, sleep, tap, point, type, send, keyboardOff, scroll, scrollTo, nav, mark, PORTRAIT }) {
  mark('add');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(1200);
  await tap(app.getByText('Add MCP server').first(), { move: 800 });
  const d = app.locator('[role=dialog]').first();
  await d.getByText('Notion', { exact: true }).first().waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(700);
  await tap(d.getByRole('button', { name: 'Custom', exact: true }), { move: 700 });
  await sleep(500);
  const url = d.locator('input[placeholder="https://example.com/mcp"]');
  await type(url, 'https://mcp.kiwi.com', { cps: 14 });
  await keyboardOff();
  const transport = d.locator('select').first();
  await point(transport, { move: 500 });
  await S('press'); await sleep(120);
  await transport.selectOption('http'); await S('release');
  await sleep(900);

  mark('connect');
  await S('caption', 1);
  await tap(d.getByRole('button', { name: /^Connect/ }).last(), { move: 700, dispatch: true });
  await d.getByText('Connection ready').first().waitFor({ timeout: 90000 });
  await S('hide');
  await sleep(400);
  await d.getByText('feedback-to-devs').first().scrollIntoViewIfNeeded().catch(() => {});
  await sleep(1600);
  await tap(d.getByRole('button', { name: 'Save server' }), { move: 700, dispatch: true });
  await app.locator('main').getByText('Connected', { exact: true }).first().waitFor({ timeout: 60000 });
  await S('hide');
  const toggle = app.locator('main button[role=switch][aria-label="feedback-to-devs"]').first();
  await scrollTo(toggle, PORTRAIT ? 560 : 500, 1000).catch(() => {});
  await sleep(900);
  await tap(toggle, { move: 700 });
  await app.locator('main').getByText('1/2 tools enabled').first().waitFor({ timeout: 30000 }).catch(() => {});
  await S('hide');
  await sleep(700);
  // A saved server reaches the running agent on its next reload: the tab's own Reload agent.
  const reload = app.getByRole('button', { name: 'Reload agent' }).first();
  await scrollTo(reload, PORTRAIT ? 220 : 300, 900).catch(() => {});
  await tap(reload, { move: 800 });
  await sleep(1400);
  await S('hide');

  mark('ask');
  await S('caption', 2);
  await nav('/chat/sora', { click: false });
  await app.locator('textarea').last().waitFor({ timeout: 30000 });
  await sleep(700);
  await send('Find return flights from Singapore to Tokyo for 4 of us, out 13 January, back 17 January.', { cps: 26 });
  await app.getByText('Calling search-flight').first().waitFor({ timeout: 90000 });
  await sleep(1500);

  await app.getByText('worth a look').first().waitFor({ timeout: 120000 });
  mark('answer');
  await S('caption', 3);
  await app.getByText('Prices are what Kiwi.com showed').first().waitFor({ timeout: 60000 });
  await sleep(600);
  await scrollTo(app.getByText('worth a look').last(), PORTRAIT ? 90 : 110, 1200).catch(() => {});
  await sleep(3200);
  if (PORTRAIT) { await scroll(260, 1000); await sleep(1600); }

  mark('flex');
  await S('caption', 4);
  await send('Is it cheaper if we fly a day earlier or later?', { cps: 24 });
  await app.getByText('date pairs').first().waitFor({ timeout: 120000 });
  await app.getByText('in total.').last().waitFor({ timeout: 60000 });
  await sleep(600);
  await scrollTo(app.getByText('date pairs').last(), PORTRAIT ? 90 : 120, 1100).catch(() => {});
  await sleep(4200);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
