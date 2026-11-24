// The pages video: an agent builds a small web page and publishes it; the page is listed on the
// agent's Pages tab, the owner sets the viewer password there, and the page is served by the runtime.
// Team: Fernhill Yoga (teams/pages.py). The publishing step runs the product's own pages_start tool.
//
// Two stand-ins, both for things outside the dashboard itself:
// - The demo dashboard is the Vite dev server, which forwards only /api to the runtime. A deployment
//   serves /pages/ from the same address, so init() forwards /pages/ to the runtime as well.
// - A browser asks for the page's username and password in its own sign-in box, which a recording
//   of the page cannot show. init() sends the credentials the take sets on the Pages tab, as a
//   browser does once someone has signed in. Until that password is set, the runtime answers 503.
const SLUG = 'fernhill-classes';
const USER = 'owner';                       // the form's default username
const PASSWORD = 'demo-only-not-real';      // typed into the Pages tab during the take

export const start = '/chat/sage';
export const ready = (app) => app.locator('textarea').last().waitFor();
export const brand = 'Agents that publish.';
export const keyboardWords = ['"Publish"', 'Publish', 'Public'];
export const captions = [
  { eyebrow: 'Ask', h1: 'Ask for<br>a page', p: 'In plain words, in the chat.' },
  { eyebrow: 'Build', h1: 'It writes it,<br>then publishes', p: 'From the files in its workspace.' },
  { eyebrow: 'Pages tab', h1: 'Every page,<br>listed', p: 'With the address it is served at.' },
  { eyebrow: 'Password', h1: 'You set<br>the password', p: 'The agent never sees it.' },
  { eyebrow: 'Live', h1: 'Served by<br>your deployment', p: 'The timetable, at its own address.' },
];
export const voice = [
  "Sage looks after a yoga studio's timetable. Ask for it as a web page.",
  'Sage reads the studio files, writes the page, and publishes it. Each step shows in the chat.',
  "The new page is listed on Sage's Pages tab, with its address.",
  'Every page sits behind a password that only you set. Sage cannot read it or change it.',
  "Open the address with that password, and there it is: this week's classes and prices, served by your own deployment.",
  'Olano. Agents that publish.',
];

export async function init(ctx, { server }) {
  const auth = 'Basic ' + Buffer.from(`${USER}:${PASSWORD}`).toString('base64');
  await ctx.route((url) => url.origin === new URL(server.base_url).origin && url.pathname.startsWith('/pages/'), async (route) => {
    const url = route.request().url().replace(server.base_url, server.runtime_url);
    const response = await route.fetch({ url, headers: { ...route.request().headers(), authorization: auth } });
    await route.fulfill({ response });
  });
}

export async function run({ app, S, sleep, tap, point, type, send, keyboardOff, scroll, scrollTo, nav, openApp, mark, PORTRAIT }) {
  mark('ask');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(1800);
  await send("Publish this week's classes and prices as a page.");

  await app.getByText('Calling read_file').first().waitFor({ timeout: 60000 });
  mark('build');
  await S('caption', 1);
  await app.getByText('Calling pages_start').first().waitFor({ timeout: 90000 });
  await app.getByText('cannot see or change').first().waitFor({ timeout: 90000 });
  await sleep(2600);

  mark('tab');
  await S('caption', 2);
  if (PORTRAIT) await nav(`/agents/sage/pages`);
  else {
    await nav('/agents/sage', { click: false });
    const tab = app.locator('main').getByText('Pages', { exact: true }).first();
    await tab.waitFor({ timeout: 30000 });
    await sleep(900);
    await tap(tab, { move: 800 });
  }
  const card = app.locator(`[data-testid="page-card-${SLUG}"]`);
  await card.waitFor({ timeout: 30000 });
  await sleep(500);
  await scrollTo(card, PORTRAIT ? 150 : 250, 900);
  if (!PORTRAIT) await point(card.locator('a').first(), { move: 800 });
  await sleep(PORTRAIT ? 3600 : 3000);
  await S('hide');

  mark('password');
  await S('caption', 3);
  const field = app.locator('input[type=password]').first();
  await scrollTo(field, PORTRAIT ? 250 : 420, 1000);
  await sleep(700);
  await type(field, PASSWORD, { cps: 15 });
  await keyboardOff();
  await sleep(500);
  await tap(app.getByRole('button', { name: 'Set password' }), { move: 600 });
  await app.getByText('Password set').first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(2200);

  mark('live');
  await S('caption', 4);
  await scrollTo(card, PORTRAIT ? 150 : 250, 900);
  const link = card.locator('a').first();
  // The link opens a new browser tab; the recording follows it in the same window.
  await point(link, { move: 700 });
  await sleep(120);
  await S('press');
  await sleep(140);
  await S('release');
  await openApp(`/pages/${SLUG}/`, (a) => a.getByText("This week's classes").first().waitFor({ timeout: 30000 }));
  if (!PORTRAIT) await S('address', `localhost:3200/pages/${SLUG}/`);
  await S('hide');
  await sleep(3400);
  await scroll(PORTRAIT ? 520 : 330, 1800);
  await sleep(3800);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
