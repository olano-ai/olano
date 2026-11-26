// The calculator video: an agent fills in a public calculator that has no API (the CPF Board's CPF
// contribution calculator), reads the result and explains it. Mei's browser steps are the product's
// own tools run for real in the product's headless browser (set to Headless before the take) against
// the live page; the Calculate button is a form submit, so the browser's consequence guard asks
// first, and the card is the one it raises. The reply is built from the result the page showed.
// Team: Lantern Lane Dumplings (teams/calculator.py). Needs network access.
export const start = '/chat/mei';
export const ready = (app) => app.locator('textarea').last().waitFor({ timeout: 60000 });
export const brand = 'Forms filled, answers explained.';
export const keyboardWords = ['"We\'re"', "We're", 'We'];
export const captions = [
  { eyebrow: 'Ask', h1: 'What will<br>CPF cost?', p: 'A new hire’s numbers, in the chat.' },
  { eyebrow: 'Browser', h1: 'Fills in the<br>calculator', p: 'The CPF Board’s public page.' },
  { eyebrow: 'Approval', h1: 'Asks before<br>it submits', p: 'Calculate is a form submit.' },
  { eyebrow: 'Answer', h1: 'In plain<br>words', p: 'Read from the result page.' },
  { eyebrow: 'Screenshot', h1: 'The page<br>it read', p: 'Saved with the answer.' },
];
export const voice = [
  'Mei keeps the books for a dumpling kitchen that is hiring its first cook. Ask Mei what the monthly CPF contributions will be, with his wage and birth date in the message.',
  "Mei opens the CPF Board's contribution calculator in its own browser, and fills in the numbers from the chat.",
  'The Calculate button submits a form, so the browser stops and asks first. Approve it.',
  "Mei reads the result and explains it: what comes out of the cook's pay, and what the kitchen adds.",
  'A screenshot of the result page is saved with the answer, so the figures can be checked.',
  'Olano. Forms filled, answers explained.',
];

export async function run({ app, page, S, sleep, tap, point, send, scrollTo, mark, PORTRAIT }) {
  // The chat brings an approval card into view with scrollIntoView, which also scrolls the frames
  // around the app (see flow-browser.mjs): the stage's own boxes around the app do not scroll.
  await page.evaluate(() => {
    for (let e = document.getElementById('app').parentElement; e && e !== document.body; e = e.parentElement) {
      if (getComputedStyle(e).overflow !== 'visible') e.style.overflow = 'clip';
    }
  });
  // 0. the request
  mark('ask');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(1200);
  await send("We're hiring Wei Jie as a cook at S$3,200 a month from December. Singapore citizen, born May 1991. Use the CPF Board's contribution calculator: what do we each pay?", { cps: 30 });
  await app.getByText('Calling browser_navigate').first().waitFor({ timeout: 90000 });
  await sleep(1400);

  // 1. the form, filled in step by step
  mark('fill');
  await S('caption', 1);
  await app.getByText('Calling browser_fill').first().waitFor({ timeout: 120000 });
  await sleep(1800);

  // 2. the guard stops the submit and asks
  const accept = app.getByRole('button', { name: 'Accept once' });
  await accept.waitFor({ timeout: 120000 });
  mark('approve');
  await S('caption', 2);
  await sleep(600);
  const card = app.locator('.approval-card').first();
  await scrollTo(card, PORTRAIT ? 250 : 300, 800).catch(() => {});
  if (!PORTRAIT) await point(card.getByText(/named 'submit'/).first(), { move: 900 }).catch(() => {});
  await sleep(3200);
  await tap(accept, { move: 800 });
  await S('hide');

  // 3. the answer
  const plain = app.getByText('In plain words').first();
  await plain.waitFor({ timeout: 120000 });
  mark('answer');
  await S('caption', 3);
  await app.getByText('Saved:').first().waitFor({ timeout: 60000 });
  await sleep(500);
  const table = app.locator('main table').last();
  await scrollTo(table, PORTRAIT ? 150 : 140, 1100).catch(() => {});
  if (!PORTRAIT) await point(table.locator('tr').last(), { move: 900 }).catch(() => {});
  await sleep(4600);

  // 4. the screenshot the browser saved
  mark('shot');
  await S('caption', 4);
  const shot = app.locator('main img').last();
  await shot.waitFor({ timeout: 30000 });
  await sleep(300);
  // The file card under the picture: bringing it up shows the whole screenshot, and the chat
  // is then at its end, so its "Jump to latest" button goes away.
  const files = app.getByText('Open in Files').last();
  await scrollTo(files, PORTRAIT ? 640 : 600, 1300).catch(() => {});
  await sleep(4200);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
