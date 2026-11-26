// The chess video: lichess-mcp (a published npm package, run locally with npx; no account or key for
// public data) added to an agent as a custom stdio server during the take, then today's Lichess puzzle
// drawn as a board and Magnus Carlsen's Lichess rating history as a chart. The lichess_* steps are
// real calls to that server, and the board and chart are rendered by the product's own
// render_html_image tool from what came back (teams/chess.py). Team: Endgame Club. Needs network.
export const start = '/agents/nell/mcp';
export const ready = (app) => app.getByText('Add MCP server').first().waitFor({ timeout: 60000 });
export const brand = 'Bring your own MCP servers.';
export const keyboardWords = ['"Give"', 'Give', 'How'];
export const captions = [
  { eyebrow: 'MCP', h1: 'A Lichess<br>MCP server', p: 'Run locally with npx. No key.' },
  { eyebrow: 'Connect', h1: 'Its tools<br>appear', p: 'Puzzles, players, ratings.' },
  { eyebrow: 'Ask', h1: "Today's<br>puzzle", p: 'Fetched, then drawn as a board.' },
  { eyebrow: 'Answer', h1: 'The board,<br>ready to solve', p: 'With the solution underneath.' },
  { eyebrow: 'Ask', h1: "A champion's<br>rating", p: 'Magnus Carlsen on Lichess.' },
  { eyebrow: 'Answer', h1: 'Years of<br>ratings, charted', p: 'From the rating history.' },
];
export const voice = [
  'Add a Lichess MCP server to Nell. It runs on this machine with npx, and needs no key.',
  'It connects, and its tools appear: puzzles, players and ratings.',
  "Ask Nell for today's Lichess puzzle.",
  'Nell fetches it and draws the board, with the solution underneath for later.',
  "Now ask how Magnus Carlsen's rating has moved.",
  'Nell reads the rating history from Lichess and charts it.',
  'Olano. Bring your own MCP servers.',
];

export async function run({ page, app, S, sleep, tap, point, type, send, keyboardOff, scrollTo, nav, mark, PORTRAIT, PHONE }) {
  // 0. a custom stdio server: npx lichess-mcp
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
  await type(d.locator('input[placeholder="time"]'), 'lichess', { cps: 16 });
  await type(d.locator('input[placeholder="uvx"]'), 'npx', { cps: 14 });
  const args = d.locator('textarea[placeholder^="-y"]').first();
  await tap(args, { move: 500 });
  await args.focus();
  if (PHONE) { await S('keyboard', true); await S('hide'); await sleep(450); }
  for (const ch of '-y\nlichess-mcp') {
    if (PHONE && ch !== '\n') await S('key', ch);
    if (ch === '\n') await page.keyboard.press('Enter'); else await page.keyboard.type(ch);
    await sleep(65);
  }
  await keyboardOff();
  await S('hide');
  await sleep(500);

  // 1. connect: the server starts and lists its tools; saved, then reloaded into Nell
  mark('connect');
  await S('caption', 1);
  await tap(d.getByRole('button', { name: /^Connect/ }).last(), { move: 700, dispatch: true });
  await d.getByText('Connection ready').first().waitFor({ timeout: 120000 });
  await S('hide');
  await d.getByText('lichess_get_daily_puzzle').first().scrollIntoViewIfNeeded().catch(() => {});
  await sleep(1200);
  await tap(d.getByRole('button', { name: 'Save server' }), { move: 700, dispatch: true });
  await app.locator('main').getByText('Connected', { exact: true }).first().waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(400);
  await tap(app.getByRole('button', { name: 'Reload agent' }).first(), { move: 800 });
  await S('hide');
  await sleep(900);
  await scrollTo(app.locator('main').getByText('lichess_get_user', { exact: true }).first(), PORTRAIT ? 420 : 380, 1000).catch(() => {});
  await sleep(1300);

  // 2. the puzzle
  mark('ask');
  await S('caption', 2);
  await nav('/chat/nell', { click: false });
  await app.locator('textarea').last().waitFor({ timeout: 30000 });
  await sleep(500);
  await send("Give me today's Lichess puzzle.", { cps: 22 });
  await app.getByText('Calling lichess_get_daily_puzzle').first().waitFor({ timeout: 90000 }).catch(() => {});
  await app.getByText('Calling render_html_image').first().waitFor({ timeout: 120000 }).catch(() => {});

  // 3. the board
  await app.getByText("Here is today's Lichess puzzle", { exact: false }).first().waitFor({ timeout: 180000 });
  mark('board');
  await S('caption', 3);
  await app.getByText('From the game', { exact: false }).first().waitFor({ timeout: 60000 });
  const img = app.locator('img[src*="daily-"]').last();
  await img.waitFor({ timeout: 60000 }).catch(() => {});
  await sleep(500);
  await scrollTo(img, PORTRAIT ? 70 : 90, 1200).catch(() => {});
  await sleep(PORTRAIT ? 2600 : 2800);
  if (!PORTRAIT) {
    await scrollTo(app.getByText('Solution, when you want it', { exact: false }).last(), 420, 1000).catch(() => {});
    await sleep(1500);
  } else await sleep(1200);

  // 4. Magnus Carlsen
  mark('magnus');
  await S('caption', 4);
  await send("How has Magnus Carlsen's rating moved on Lichess?", { cps: PORTRAIT ? 30 : 24 });
  await app.getByText('Calling lichess_get_rating_history').first().waitFor({ timeout: 90000 }).catch(() => {});

  // 5. the chart
  await app.getByText('plays on Lichess as', { exact: false }).first().waitFor({ timeout: 180000 });
  mark('chart');
  await S('caption', 5);
  await app.getByText('not his FIDE rating', { exact: false }).first().waitFor({ timeout: 60000 });
  await sleep(400);
  await scrollTo(app.getByText('plays on Lichess as', { exact: false }).last(), PORTRAIT ? 70 : 90, 1100).catch(() => {});
  await sleep(1800);
  await scrollTo(app.locator('img[src*="drnykterstein"]').last(), PORTRAIT ? 120 : 110, 1100).catch(() => {});
  await sleep(PORTRAIT ? 3000 : 3200);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
