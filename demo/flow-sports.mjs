// The sports video: Sports Hub (npx -y mcp-sports-hub@1.4.1), an open-source keyless MCP server, added
// as a custom local (stdio) server with only its ESPN provider loaded, its ten tools listed, then a
// game-day briefing in the chat. Dex's tool steps are real calls to that server over stdio, and the
// reply is built from what ESPN's data returned (teams/sports.py). Team: The Corner Booth, a sports bar.
export const start = '/agents/dex/mcp';
export const ready = (app) => app.getByText('Add MCP server').first().waitFor({ timeout: 60000 });
export const brand = 'Game day, sorted.';
export const keyboardWords = ['"Who\'s', 'Who\'s', 'Wh'];
export const captions = [
  { eyebrow: 'Local MCP', h1: 'Sports Hub,<br>via npx', p: 'Open source, no key. ESPN data only.' },
  { eyebrow: 'Tools', h1: 'Ten ESPN<br>tools', p: 'Scores, standings, game summaries.' },
  { eyebrow: 'Ask', h1: "Who's playing<br>today?", p: 'The scoreboard, then each game.' },
  { eyebrow: 'Answer', h1: 'A game-day<br>briefing', p: 'Kickoff, the favourite, who is out.' },
  { eyebrow: 'Verbose', h1: 'Straight<br>from ESPN', p: 'What the server sent back.' },
];
export const voice = [
  'Dex plans the screens at a sports bar. Add Sports Hub, a free MCP server, run locally with npx, set to ESPN data.',
  'The server starts on this machine and lists ten tools: scores, standings and game summaries.',
  "Ask Dex who's playing today. Dex reads the scoreboard, then each game.",
  'The briefing has every game: kickoff, channel, who ESPN favours, and who is out injured.',
  'In verbose mode, you can see what ESPN sent back.',
  'Olano. Game day, sorted.',
];

export async function run({ page, app, S, sleep, tap, type, send, keyboardOff, scroll, scrollTo, nav, mark, PORTRAIT, PHONE }) {
  // 0. a custom stdio server: npx, the package, one environment variable
  mark('mcp');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(1200);
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
  await type(d.locator('input[placeholder="time"]'), 'sports-hub', { cps: 16 });
  await type(d.locator('input[placeholder="uvx"]'), 'npx', { cps: 12 });
  await typeLines(d.locator('textarea').nth(0), ['-y', 'mcp-sports-hub@1.4.1'], 20);
  await typeLines(d.locator('textarea').nth(1), ['SPORTS_HUB_PROVIDERS=espn'], 22);
  await keyboardOff();
  await S('hide');
  await sleep(700);

  // 1. connect: the server starts and lists its tools; saved, then reloaded into Dex
  mark('tools');
  await S('caption', 1);
  await tap(d.getByRole('button', { name: /^Connect/ }).last(), { move: 700 });
  await d.getByText('Connection ready').first().waitFor({ timeout: 150000 });
  await S('hide');
  await d.getByText('espn_get_scoreboard').first().scrollIntoViewIfNeeded().catch(() => {});
  await sleep(2600);
  await tap(d.getByRole('button', { name: 'Save server' }), { move: 700 });
  await app.locator('main').getByText('Connected', { exact: true }).first().waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(500);
  await tap(app.getByRole('button', { name: 'Reload agent' }).first(), { move: 700 });
  await app.getByText('Reloaded MCP tools').first().waitFor({ timeout: 150000 });
  await S('hide');
  await sleep(1000);

  // 2. the question
  mark('ask');
  await S('caption', 2);
  await nav('/chat/dex', { click: false });
  await app.locator('textarea').last().waitFor({ timeout: 30000 });
  await sleep(600);
  await send("Who's playing in the NFL today, who's favoured, and any injuries?", { cps: PORTRAIT ? 36 : 26 });
  await app.getByText('Calling espn_get_scoreboard').first().waitFor({ timeout: 120000 });
  await app.getByText('Calling espn_get_event_summary').first().waitFor({ timeout: 120000 });
  await sleep(600);

  // 3. the briefing, built from the four results
  await app.getByText('NFL game', { exact: false }).first().waitFor({ timeout: 240000 });
  mark('answer');
  await S('caption', 3);
  await app.getByText('injury report can change', { exact: false }).first().waitFor({ timeout: 120000 });
  await sleep(500);
  await scrollTo(app.getByText('NFL game', { exact: false }).first(), PORTRAIT ? 90 : 120, 1100).catch(() => {});
  await sleep(PORTRAIT ? 2400 : 3200);
  if (PORTRAIT) { await scroll(420, 1400); await sleep(2400); }
  else { await sleep(1000); }

  // 4. verbose: what each call returned
  mark('verbose');
  await S('caption', 4);
  await tap(app.getByText('Normal', { exact: true }).first(), { move: 700 });
  await app.getByText('Verbose', { exact: true }).first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(600);
  await scrollTo(app.getByText('espn_get_scoreboard', { exact: false }).first(), PORTRAIT ? 120 : 140, 1200).catch(() => {});
  await sleep(1600);
  await scroll(PORTRAIT ? 420 : 380, 1500);
  await sleep(1600);

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
