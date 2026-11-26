// The runweather video: Weather MCP (npm @dangahagan/weather-mcp, Open-Meteo data, no key), a local
// stdio server started with npx, added as a custom server during the take, its tools listed, then one
// question in chat. Kip's two tool steps are real calls to that server over stdio and the go or no-go
// is built from what it returned (teams/runweather.py). Team: Lamplight Runners, a running club.
export const start = '/agents/kip/mcp';
export const ready = (app) => app.getByText('Add MCP server').first().waitFor({ timeout: 60000 });
export const timezone = 'Asia/Bangkok';
export const brand = 'Know before you go.';
export const keyboardWords = ['"Is"', 'Is', 'np', 'we'];
export const captions = [
  { eyebrow: 'Local MCP', h1: 'A weather<br>server, via npx', p: 'Weather MCP: Open-Meteo data, no key.' },
  { eyebrow: 'Tools', h1: 'Its weather<br>tools appear', p: 'Saved, then loaded into Kip.' },
  { eyebrow: 'Ask', h1: 'Good air<br>for a run?', p: 'Tonight, in Bangkok.' },
  { eyebrow: 'Answer', h1: 'Go or no-go,<br>and the hour', p: 'From the air reading and the forecast.' },
  { eyebrow: 'Verbose', h1: 'The raw<br>numbers', p: 'What the server sent back.' },
];
export const voice = [
  "Ploy coaches a running club in Bangkok. She adds a weather MCP server to Kip, her run planner. It runs locally with npx and needs no key.",
  'It connects and lists its weather tools. Save it, and reload Kip.',
  'Ploy asks Kip if the air is good enough for a run tonight, and when to go.',
  'Kip checks the air quality and the hourly forecast, then answers: go or no-go, and the best hour.',
  'In verbose mode, every step shows what the server sent back.',
  'Olano. Know before you go.',
];

export async function run({ page, app, S, sleep, tap, point, type, send, keyboardOff, scroll, scrollTo, nav, mark, PORTRAIT, PHONE }) {
  mark('add');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(1200);
  await tap(app.getByText('Add MCP server').first(), { move: 700 });
  const d = app.locator('[role=dialog]').first();
  await d.getByText('Notion', { exact: true }).first().waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(500);
  await tap(d.getByRole('button', { name: 'Custom', exact: true }), { move: 700 });
  await sleep(400);
  const transport = d.locator('select').first();
  await point(transport, { move: 500 });
  await S('press'); await sleep(120);
  await transport.selectOption('stdio'); await S('release');
  await sleep(500);
  await type(d.locator('input[placeholder="time"]'), 'weather', { cps: 14 });
  await type(d.locator('input[placeholder="uvx"]'), 'npx', { cps: 12 });
  const args = d.locator('textarea').first();
  await tap(args, { move: 600 });
  await args.focus();
  if (PHONE) { await S('keyboard', true); await S('hide'); await sleep(450); }
  for (const ch of '-y\n@dangahagan/weather-mcp@1.35.30') {
    if (PHONE && ch !== '\n') await S('key', ch);
    if (ch === '\n') await page.keyboard.press('Enter'); else await page.keyboard.type(ch);
    await sleep(55);
  }
  await keyboardOff();
  await S('hide');
  await sleep(900);

  mark('tools');
  await S('caption', 1);
  await tap(d.getByRole('button', { name: /^Connect/ }).last(), { move: 700 });
  await d.getByText('Connection ready').first().waitFor({ timeout: 150000 });
  await S('hide');
  await d.getByText('get_weather_summary', { exact: true }).first().scrollIntoViewIfNeeded().catch(() => {});
  await sleep(2200);
  await tap(d.getByRole('button', { name: 'Save server' }), { move: 700 });
  await app.locator('main').getByText('Connected', { exact: true }).first().waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(500);
  await tap(app.getByRole('button', { name: 'Reload agent' }), { move: 700 });
  await app.getByText('Reloaded MCP tools').first().waitFor({ timeout: 150000 });
  await S('hide');
  await sleep(1200);

  mark('ask');
  await S('caption', 2);
  await nav('/chat/kip', { click: false });
  await app.locator('textarea').last().waitFor({ timeout: 30000 });
  await sleep(600);
  await send("Is the air good enough for a run tonight in Bangkok? When's the best hour?", { cps: PORTRAIT ? 36 : 26 });
  await app.getByText('Calling get_weather_summary').first().waitFor({ timeout: 90000 });
  await app.getByText('Calling get_forecast').first().waitFor({ timeout: 150000 });
  await sleep(600);

  await app.getByText('through Weather MCP').first().waitFor({ timeout: 180000 });
  mark('answer');
  await S('caption', 3);
  await sleep(500);
  await scrollTo(app.locator('.tool-activity').first(), PORTRAIT ? 100 : 130, 1200).catch(() => {});
  await sleep(PORTRAIT ? 5200 : 5600);

  mark('verbose');
  await S('caption', 4);
  await tap(app.getByText('Normal', { exact: true }).first(), { move: 700 });
  await app.getByText('Verbose', { exact: true }).first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(600);
  await sleep(1400);
  await scroll(PORTRAIT ? 420 : 380, 1600);
  await sleep(1300);
  await scroll(PORTRAIT ? 420 : 380, 1600);
  await sleep(1200);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
