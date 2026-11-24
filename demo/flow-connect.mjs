// The connect video: the Connections page, an MCP server added from the directory (it really
// connects: Parallel's free search server needs no account), and a magic link the agent sends in the
// chat, opened and unlocked with its PIN up to the hand-off to the service.
// Team: Saltmarsh Cycles (teams/connect.py). The link's page opens in a new tab in the product; the
// take shows it in the same window.
export const start = '/connections';
export const ready = (app) => app.getByText('Microsoft 365').first().waitFor();
export const brand = 'Plug in the tools you already use.';
export const keyboardWords = ['"Send"', 'Send', 'Slack'];
export const captions = [
  { eyebrow: 'Connections', h1: 'One card<br>per service', p: 'Each lists the tools it brings.' },
  { eyebrow: 'Sign in', h1: 'More than<br>one way in', p: 'A code, or an app of your own.' },
  { eyebrow: 'MCP', h1: 'Pick from<br>the directory', p: 'No address to type.' },
  { eyebrow: 'Tools', h1: 'Three new<br>tools', p: 'Connected. Switch any of them off.' },
  { eyebrow: 'Magic link', h1: 'A link,<br>with a PIN', p: 'Sent by the agent, in the chat.' },
  { eyebrow: 'Finish', h1: 'Open it on<br>any device', p: 'The PIN, then the service itself.' },
];
// The link's page refuses to be framed (X-Frame-Options, frame-ancestors). The stage shows the app in
// a frame, so the take drops those two headers for that page; nothing else about it changes.
export async function init(ctx) {
  await ctx.route('**/api/connect/**', async (route) => {
    const res = await route.fetch();
    const headers = { ...res.headers() };
    delete headers['x-frame-options'];
    if (headers['content-security-policy']) headers['content-security-policy'] = headers['content-security-policy'].replace(/frame-ancestors[^;]*;?\s*/, '');
    await route.fulfill({ response: res, headers });
  });
}
export const voice = [
  'Your agents work with the tools you already have. Each service gets a card, with the tools it brings.',
  'Open a card to connect it. GitHub takes a one-time code, or an app of your own.',
  'For everything else there are MCP servers. Pick one from the directory.',
  'Milo connects, and three new tools appear. Switch off any that Milo should not use.',
  'Milo can also start a sign-in from the chat: a secure link, with a PIN.',
  'Open it on any device, enter the PIN, and finish with the service itself.',
  'Olano. Plug in the tools you already use.',
];

export async function run({ app, frame, S, sleep, tap, point, type, send, keyboardOff, scroll, scrollTo, nav, mark, PORTRAIT }) {
  mark('services');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(2600);
  await scroll(PORTRAIT ? 420 : 300, 1800);
  await sleep(3200);

  mark('signin');
  await S('caption', 1);
  const github = app.locator('main .card', { hasText: 'github_list_repos' }).last();
  await scrollTo(github, PORTRAIT ? 80 : 90, 1500);
  await sleep(700);
  await tap(github.getByRole('button', { name: 'Connect', exact: true }), { move: 700 });
  await app.getByText('Before you connect').first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(900);
  if (!PORTRAIT) await point(github.getByRole('button', { name: 'Sign in with a code' }), { move: 700 });
  await sleep(1000);
  await scroll(PORTRAIT ? 380 : 260, 1400);
  await sleep(1900);

  mark('mcp');
  await S('caption', 2);
  await nav('/agents/milo/mcp');
  const add = app.getByText('Add MCP server').first();
  await add.waitFor({ timeout: 30000 });
  await sleep(700);
  await tap(add, { move: 700 });
  const d = app.locator('[role=dialog]').first();
  await d.getByText('Notion', { exact: true }).first().waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(1300);
  await type(d.locator('input[placeholder^="Search"]'), 'parallel', { cps: 16 });
  await keyboardOff();
  const preset = d.getByText('Parallel Search', { exact: false }).first();
  await preset.waitFor({ timeout: 30000 });
  await sleep(500);
  await tap(preset, { move: 500 });
  await d.getByText('Ready to connect').first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(900);

  mark('tools');
  await S('caption', 3);
  await tap(d.getByRole('button', { name: /^Connect/ }).last(), { move: 700, dispatch: true });
  await d.getByText('Connection ready').first().waitFor({ timeout: 90000 });
  await S('hide');
  await d.getByText('web_fetch').first().scrollIntoViewIfNeeded().catch(() => {});
  await sleep(2300);
  await tap(d.getByRole('button', { name: 'Save server' }), { move: 700, dispatch: true });
  await app.locator('main').getByText('Connected', { exact: true }).first().waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(600);
  await scrollTo(app.locator('main').getByText('search.parallel.ai', { exact: true }).first(), PORTRAIT ? 120 : 200, 1000).catch(() => {});
  await sleep(1900);

  mark('link');
  await S('caption', 4);
  await nav('/chat/milo');
  await app.locator('textarea').last().waitFor({ timeout: 30000 });
  await sleep(700);
  await send('Send me a link to connect Slack.');
  const card = app.locator('.connect-card').first();
  await card.waitFor({ timeout: 90000 });
  await app.getByText('nothing is pasted into this chat').first().waitFor({ timeout: 90000 });
  if (!PORTRAIT) await point(card.locator('.connect-card__pin'), { move: 800 });
  await sleep(1700);

  mark('finish');
  await S('caption', 5);
  const pin = ((await card.innerText()).match(/PIN\s*(\d+)/) || [])[1];
  // The product opens the link in a new tab; the take has one window, so it opens here.
  await frame.evaluate(() => { window.open = (url) => { window.location.href = url; return null; }; });
  await tap(card.locator('.connect-card__btn'), { move: 700 });
  const field = app.locator('input').first();
  await app.getByText("Confirm it's you").first().waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(900);
  await type(field, pin, { cps: 8 });
  await keyboardOff();
  await tap(app.getByRole('button', { name: 'Continue' }), { move: 500 });
  await app.getByText('Connect Slack').first().waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(PORTRAIT ? 2200 : 3500);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
