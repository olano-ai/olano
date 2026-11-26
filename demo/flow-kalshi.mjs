// The kalshi video: Kalshi's official MCP server (https://api.elections.kalshi.com/mcp) added to an agent
// as a custom server with no account, then two plain questions in the chat. Only the public market tools
// are called (search_markets, get_event_odds, get_market_links); each is a real call to that server and
// the reply is built from what came back (teams/kalshi.py). Market prices only, never a suggestion to
// trade. Team: Loose Change, a podcast about money and the news. Needs network.
export const start = '/agents/tobi/mcp';
export const ready = (app) => app.getByText('Add MCP server').first().waitFor({ timeout: 60000 });
export const brand = 'Ask the market, in plain words.';
export const keyboardWords = ['"What', 'What', 'And'];
export const captions = [
  { eyebrow: 'MCP', h1: "Kalshi's<br>MCP server", p: 'Added by its address. No account.' },
  { eyebrow: 'Tools', h1: 'Market tools<br>appear', p: 'Only the public ones get used.' },
  { eyebrow: 'Ask', h1: 'Will the Fed<br>cut rates?', p: 'A search, then the live odds.' },
  { eyebrow: 'Answer', h1: 'What the market<br>thinks', p: 'Each outcome, with a link to it.' },
  { eyebrow: 'Ask again', h1: 'The halftime<br>show', p: 'Same tools, a different market.' },
];
export const voice = [
  "Tobi does research for a money podcast. Add Kalshi's official MCP server by its address. No account needed.",
  'The server connects and lists its tools. The account tools stay unused: Tobi only reads public markets.',
  'Ask Tobi what the odds are of a Fed rate cut in December. Tobi finds the market and reads its prices.',
  "The answer turns the prices into plain chances for each outcome, with a link to the market on Kalshi.",
  'Then something lighter: who will play the Super Bowl halftime show. Same tools, a different market.',
  'Olano. Ask the market, in plain words.',
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
  await type(d.locator('input[placeholder="https://example.com/mcp"]'), 'https://api.elections.kalshi.com/mcp', { cps: 20 });
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
  await d.locator('code', { hasText: /^search_markets$/ }).first().scrollIntoViewIfNeeded().catch(() => {});
  await sleep(2600);
  await tap(d.getByRole('button', { name: 'Save server' }), { move: 700 });
  await app.locator('main').getByText('Connected', { exact: true }).first().waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(500);
  await tap(app.getByRole('button', { name: 'Reload agent' }).first(), { move: 700 });
  await app.getByText('Reloaded MCP tools').first().waitFor({ timeout: 90000 });
  await S('hide');
  await sleep(1000);

  // 2. the Fed question
  mark('ask');
  await S('caption', 2);
  await nav('/chat/tobi', { click: false });
  await app.locator('textarea').last().waitFor({ timeout: 30000 });
  await sleep(600);
  await send('What are the odds the Fed cuts rates in December?', { cps: PORTRAIT ? 32 : 24 });
  await app.getByText('Calling search_markets').first().waitFor({ timeout: 90000 });
  await app.getByText('Calling get_event_odds').first().waitFor({ timeout: 120000 });
  await sleep(600);

  // 3. the answer, built from the three results
  await app.getByText("Kalshi's", { exact: false }).first().waitFor({ timeout: 180000 });
  mark('answer');
  await S('caption', 3);
  await app.getByText('they move all day', { exact: false }).first().waitFor({ timeout: 120000 });
  await sleep(500);
  await scrollTo(app.getByText('The market puts', { exact: false }).first(), PORTRAIT ? 90 : 120, 1100).catch(() => {});
  await sleep(PORTRAIT ? 2200 : 2800);
  if (PORTRAIT) { await scroll(320, 1300); await sleep(1800); }
  else { await point(app.getByText('Open this market on Kalshi').first(), { move: 800 }); await sleep(1600); }

  // 4. a lighter question: the halftime show
  mark('halftime');
  await S('caption', 4);
  await send('And who does the market think will play the Super Bowl halftime show?', { cps: PORTRAIT ? 38 : 28 });
  await app.getByText('Calling get_event_odds').nth(1).waitFor({ timeout: 120000 });
  await app.getByText('do not add up to 100', { exact: false }).first().waitFor({ timeout: 180000 });
  await sleep(500);
  await scrollTo(app.getByText('who performs at the halftime show', { exact: false }).first(), PORTRAIT ? 90 : 120, 1100).catch(() => {});
  await sleep(PORTRAIT ? 3600 : 4200);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
