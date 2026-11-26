// The price-check video: an agent compares products on a real website with no API, then books a
// daily check. Config, Browser is set to Headless during the take; Juno's browser steps are the
// product's own browser_navigate, run for real in the product's headless
// browser against Amazon.sg, and the reply is built from what that page showed. The daily check is
// the product's own scheduler_create. Read and search only: nothing goes in a cart, no sign-in.
// Team: Pebble & Thread, a tailoring shop (teams/pricecheck.py). Needs network access.
export const start = '/config';
export const ready = (app) => app.getByTestId('browser-mode').waitFor({ state: 'attached', timeout: 60000 });
export const brand = 'Prices checked while you work.';
export const keyboardWords = ['"Find"', 'Find', 'Fi'];
export const captions = [
  { eyebrow: 'Config', h1: 'A browser<br>for agents', p: 'Headless: one per agent, no window.' },
  { eyebrow: 'Ask', h1: 'Compare<br>on Amazon.sg', p: 'Search and read. Never buy.' },
  { eyebrow: 'Browser', h1: 'The real<br>results page', p: 'Each browser step shows in the chat.' },
  { eyebrow: 'Answer', h1: 'Three options,<br>side by side', p: 'Price, rating and a link for each.' },
  { eyebrow: 'Every day', h1: 'A daily<br>price check', p: 'One message sets it up.' },
  { eyebrow: 'Scheduler', h1: 'There when<br>you look', p: 'Change it or pause it here.' },
];
export const voice = [
  'Browser control is off until an administrator turns it on, in Config. Here it is set to Headless: a browser that runs inside Olano, one per agent, with no window.',
  'Juno buys supplies for a tailoring shop. Ask Juno to compare noise-cancelling earbuds on Amazon.sg, under eighty dollars.',
  'Juno opens the search results in its own browser and reads the page, step by step.',
  'The answer is a small table: the price, the rating and a link for each, as the page showed them.',
  'Then ask Juno to check every morning. It saves a daily task on the scheduler.',
  'The task is in the Scheduler, with its next run. Change it or pause it there.',
  'Olano. Prices checked while you work.',
];

export async function run({ app, S, sleep, tap, point, send, scrollTo, nav, mark, PORTRAIT }) {
  // 0. Config, Browser: Off by default; Headless chosen and saved
  mark('config');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  const mode = app.getByTestId('browser-mode');
  await sleep(700);
  await scrollTo(app.getByText('Browser', { exact: true }).last(), PORTRAIT ? 100 : 110, 1300);
  await sleep(900);
  await scrollTo(mode, PORTRAIT ? 330 : 400, 900);
  await sleep(500);
  // A native dropdown opens outside the page, where a recording cannot see it: the take picks
  // the option directly, as the arrow keys do.
  await point(mode, { move: 700 });
  await sleep(700);
  await S('press'); await mode.selectOption('headless'); await S('release');
  await sleep(1300);
  await tap(app.getByRole('button', { name: 'Save changes' }), { move: 800 });
  const effective = app.getByTestId('browser-effective');
  await effective.getByText('Headless').first().waitFor({ timeout: 30000 });
  await S('hide');
  await scrollTo(effective, PORTRAIT ? 480 : 560, 900).catch(() => {});
  await sleep(1600);

  // 1. the request
  mark('ask');
  await S('caption', 1);
  await nav('/chat/juno', { click: false });
  await app.locator('textarea').last().waitFor({ timeout: 30000 });
  await sleep(900);
  await send('Find noise-cancelling earbuds under S$80 on Amazon.sg and compare three on price and rating.', { cps: 26 });
  await app.getByText('Calling browser_navigate').first().waitFor({ timeout: 90000 });
  await sleep(1400);

  // 2. the browser steps, live
  mark('browse');
  await S('caption', 2);
  await sleep(5200);
  const table = app.locator('main table').last();
  await table.waitFor({ timeout: 120000 });

  // 3. the answer
  mark('answer');
  await S('caption', 3);
  await app.getByText('Prices are as the results page').first().waitFor({ timeout: 90000 });
  await sleep(600);
  await scrollTo(table, PORTRAIT ? 170 : 150, 1100);
  await sleep(1200);
  if (!PORTRAIT) await point(table.locator('td').nth(1), { move: 800 }).catch(() => {});
  await sleep(4200);

  // 4. a daily check, from the chat
  mark('daily');
  await S('caption', 4);
  await send('Check these every morning and tell me if a price drops.', { cps: 26 });
  await app.getByText('Calling scheduler_create').first().waitFor({ timeout: 60000 });
  await app.getByText('where you can change or pause it').first().waitFor({ timeout: 60000 });
  await sleep(3200);

  // 5. the task in the Scheduler
  mark('scheduler');
  await S('caption', 5);
  await nav('/scheduler');
  const row = app.locator('.card', { hasText: 'Earbuds price check' }).last();
  await row.waitFor({ timeout: 30000 });
  await sleep(900);
  await scrollTo(row, PORTRAIT ? 250 : 300, 900).catch(() => {});
  if (!PORTRAIT) await point(row.getByText('Earbuds price check').first(), { move: 800 }).catch(() => {});
  await sleep(3600);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
