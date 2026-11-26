// The f1 video: Sports Hub (npx -y mcp-sports-hub@1.4.1), an open-source keyless MCP server, added as a
// custom local (stdio) server with its Formula 1 preset (Jolpica and OpenF1 data); the standings and the
// last race, then two drivers compared lap by lap as a chart. The tool steps are real calls to that server
// over stdio; the chart is drawn from the lap times it returned, with the product's own charts skill and
// render_html_image tool (teams/f1.py). Team: Slipstream, a sim-racing café.
export const start = '/agents/lotte/mcp';
export const ready = (app) => app.getByText('Add MCP server').first().waitFor({ timeout: 60000 });
export const brand = 'Race data, in a chart.';
export const keyboardWords = ['"Who\'s', 'Who\'s', 'Now'];
export const captions = [
  { eyebrow: 'Local MCP', h1: 'Formula 1<br>data, via npx', p: 'Sports Hub, set to its F1 preset.' },
  { eyebrow: 'Tools', h1: 'Results and<br>lap times', p: 'Jolpica and OpenF1. No key.' },
  { eyebrow: 'Ask', h1: "Who's leading<br>the title?", p: 'Standings and the last race.' },
  { eyebrow: 'Chart', h1: 'Two drivers,<br>lap by lap', p: 'Lap times from OpenF1.' },
  { eyebrow: 'Answer', h1: 'The chart,<br>in the reply', p: 'Drawn from the real lap times.' },
];
export const voice = [
  'Lotte plans race-night watch parties at a sim-racing café. Add Sports Hub, a free MCP server, set to Formula 1.',
  'The server starts locally and lists its tools: results and standings, and timing data down to every lap.',
  "Ask Lotte who's leading the championship. Lotte reads the standings and the last race.",
  'Then ask for a chart: two drivers in that race, lap by lap. Lotte fetches every lap time.',
  'The chart arrives in the reply, drawn from the real lap times, with the takeaway on top.',
  'Olano. Race data, in a chart.',
];

export async function run({ page, app, S, sleep, tap, type, send, keyboardOff, scroll, scrollTo, nav, mark, PORTRAIT, PHONE }) {
  // 0. a custom stdio server: npx, the package, one environment variable
  mark('mcp');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(900);
  await tap(app.getByText('Add MCP server').first(), { move: 700 });
  const d = app.locator('[role=dialog]').first();
  await d.getByText('Connect a service').first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(600);
  await tap(d.getByText('Custom', { exact: true }).first(), { move: 600 });
  await sleep(300);
  const transport = d.locator('select').first();
  await S('move', ...(await transport.boundingBox().then((b) => [b.x + b.width / 2, b.y + b.height / 2])), 600);
  await sleep(500);
  await S('press'); await transport.selectOption('stdio'); await S('release');
  await sleep(400);
  await type(d.locator('input[placeholder="time"]'), 'formula1', { cps: 22 });
  await type(d.locator('input[placeholder="uvx"]'), 'npx', { cps: 12 });
  await typeLines(d.locator('textarea').nth(0), ['-y', 'mcp-sports-hub@1.4.1'], 30);
  await typeLines(d.locator('textarea').nth(1), ['SPORTS_HUB_PROVIDERS=f1'], 32);
  await keyboardOff();
  await S('hide');
  await sleep(700);

  // 1. connect: the server starts and lists its tools; saved, then reloaded into Lotte
  mark('tools');
  await S('caption', 1);
  await tap(d.getByRole('button', { name: /^Connect/ }).last(), { move: 700 });
  await d.getByText('Connection ready').first().waitFor({ timeout: 150000 });
  await S('hide');
  await d.getByText('f1_get_driver_standings').first().scrollIntoViewIfNeeded().catch(() => {});
  await sleep(2400);
  await tap(d.getByRole('button', { name: 'Save server' }), { move: 700 });
  await app.locator('main').getByText('Connected', { exact: true }).first().waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(500);
  await tap(app.getByRole('button', { name: 'Reload agent' }).first(), { move: 700 });
  await app.getByText('Reloaded MCP tools').first().waitFor({ timeout: 150000 });
  await S('hide');
  await sleep(900);

  // 2. standings and the last race
  mark('ask');
  await S('caption', 2);
  await nav('/chat/lotte', { click: false });
  await app.locator('textarea').last().waitFor({ timeout: 30000 });
  await sleep(600);
  await send("Who's leading the F1 championship, and how did the last race finish?", { cps: PORTRAIT ? 38 : 28 });
  await app.getByText('Calling f1_get_driver_standings').first().waitFor({ timeout: 120000 });
  await app.getByText('through the Sports Hub server', { exact: false }).first().waitFor({ timeout: 180000 });
  await sleep(400);
  await scrollTo(app.getByText("The drivers' championship", { exact: false }).first(), PORTRAIT ? 90 : 110, 1100).catch(() => {});
  await sleep(PORTRAIT ? 1600 : 2200);
  if (PORTRAIT) { await scroll(380, 1200); await sleep(1300); }

  // 3. the chart request
  mark('chart');
  await S('caption', 3);
  await send('Now chart Verstappen against Norris, lap by lap, in that race.', { cps: PORTRAIT ? 36 : 26 });
  await app.getByText('Calling openf1_get_laps').first().waitFor({ timeout: 120000 });
  await app.getByText('Calling render_html_image').first().waitFor({ timeout: 180000 });
  await sleep(500);

  // 4. the reply with the chart
  await app.locator('img[src*="race-laps"]').first().waitFor({ timeout: 240000 });
  mark('answer');
  await S('caption', 4);
  await sleep(600);
  await scrollTo(app.locator('img[src*="race-laps"]').first(), PORTRAIT ? 160 : 110, 1200).catch(() => {});
  await sleep(PORTRAIT ? 4800 : 5000);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);

  /** Type lines into a textarea, Enter between them (the phone keyboard shows the letters). */
  async function typeLines(box, lines, cps) {
    await tap(box, { move: 550 });
    await box.focus();
    if (PHONE) { await S('keyboard', true); await S('hide'); await sleep(400); }
    for (let i = 0; i < lines.length; i++) {
      if (i) { await page.keyboard.press('Enter'); await sleep(120); }
      for (const ch of lines[i]) {
        if (PHONE) await S('key', ch);
        await page.keyboard.type(ch);
        await sleep((1000 / cps) * (0.7 + Math.random() * 0.6));
      }
    }
    await sleep(250);
  }
}
