// The revisions video: an agent changes a file, its memory and its own instructions; the revisions
// worker commits the turn under the agent's name; the commit's diff; a file held back because it
// holds a password; then the GitHub backup dialog (filled in, not submitted: no repository exists
// for this take, so nothing is pushed).
// Team: Saltwick Paddle Co., a kayak and paddleboard hire (teams/revisions.py).
export const team = 'revisions';
export const start = '/chat/cove';
export const ready = (app) => app.locator('textarea').last().waitFor();
export const brand = 'Every change, on the record.';
export const keyboardWords = ['From', 'Monday', 'kayak', 'school', 'groups'];
export const captions = [
  { eyebrow: 'Real work', h1: 'An agent<br>changes things', p: 'A file, its memory, its instructions.' },
  { eyebrow: 'Revisions', h1: 'Every change,<br>by name', p: 'Each commit shows who made it.' },
  { eyebrow: 'Diff', h1: 'See exactly<br>what changed', p: 'Three files, line by line.' },
  { eyebrow: 'Secrets', h1: 'Credentials<br>stay out', p: 'A file with a password is held back.' },
  { eyebrow: 'Backup', h1: 'Copy it<br>to GitHub', p: 'A private repository, off-site.' },
  { eyebrow: 'Automatic push', h1: 'Push after<br>every commit', p: 'Or once a day, or by hand.' },
];
export const voice = [
  'Cove runs the bookings desk. It updates the price list, saves the school discount to memory, and asks before changing its own instructions.',
  'In Revisions, that turn is already a commit under Cove\'s name, next to earlier work by Tide and Gull.',
  'Open the commit to see its three files, and the new price, line by line.',
  'Tide\'s note holds a password, so history holds it back. Secrets go in the Vault.',
  'For a copy off-site, connect a private, empty GitHub repository with an access token.',
  'Then choose when it pushes: by hand, once a day, or after every commit.',
  'Olano. Every change, on the record.',
];

export async function run({ app, S, sleep, tap, point, type, send, keyboardOff, scrollTo, nav, mark, PORTRAIT }) {
  mark('work');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(1200);
  await send('From Monday a half-day single kayak is $48. School groups get 10% off: remember it and add it to your instructions.', { cps: 22 });
  const accept = app.getByRole('button', { name: 'Accept once' });
  await accept.waitFor({ timeout: 90000 });
  await sleep(1200);
  await tap(accept, { move: 700 });
  await app.getByText('All three changes are in Revisions').first().waitFor({ timeout: 90000 });
  await S('hide');
  // The worker commits after its quiet period (5 s of real time).
  await sleep(2600);

  mark('history');
  await S('caption', 1);
  await nav('/revisions');
  const newest = app.locator('.revisions-entry-main').first();
  await newest.waitFor({ timeout: 30000 });
  if (!(await app.getByText('just now').first().isVisible().catch(() => false))) {
    await sleep(1500);
    await app.getByRole('button', { name: 'Refresh' }).first().click();
  }
  await app.getByText('just now').first().waitFor({ timeout: 30000 });
  await sleep(900);
  await scrollTo(app.getByText('History', { exact: true }).first(), PORTRAIT ? 40 : 60, 1300);
  await sleep(500);
  await point(newest.locator('.badge').first(), { move: 800 });
  await sleep(1300);
  await point(app.locator('.revisions-entry-main .badge').nth(2), { move: 700 });
  await sleep(1600);

  mark('diff');
  await S('caption', 2);
  await tap(newest, { move: 700 });
  await app.locator('[data-testid="revisions-commit-summary"]').first().waitFor({ timeout: 30000 });
  await sleep(900);
  const prices = app.locator('button.revisions-file-row', { hasText: 'prices.md' }).first();
  await tap(prices, { move: 800 });
  await app.locator('.revisions-diff-table').first().waitFor({ timeout: 30000 }).catch(() => {});
  await S('hide');
  await sleep(600);
  await scrollTo(prices, PORTRAIT ? 120 : 160, 1300);
  await sleep(3000);

  mark('held');
  await S('caption', 3);
  const held = app.locator('[data-testid="revisions-held-back"]');
  await scrollTo(held, PORTRAIT ? 125 : 90, 1300);
  await sleep(500);
  await point(held.locator('code').first(), { move: 800 });
  await sleep(4800);

  mark('github');
  await S('caption', 4);
  const connect = app.getByRole('button', { name: 'Connect a GitHub repository' }).first();
  await scrollTo(connect, PORTRAIT ? 300 : 360, 1000);
  await sleep(400);
  await tap(connect, { move: 700 });
  const repo = app.locator('[data-testid="revisions-connect-repo"]');
  await repo.waitFor({ timeout: 30000 });
  await sleep(700);
  await type(repo, 'saltwick-paddle/olano-history', { cps: 18 });
  await keyboardOff();
  await S('hide');
  await sleep(500);
  await point(app.locator('[data-testid="revisions-connect-auth"]'), { move: 700 });
  await sleep(1600);

  mark('push');
  await S('caption', 5);
  const sync = app.locator('.modal-body select').last();
  await scrollTo(sync, PORTRAIT ? 160 : 200, 1200);
  await sleep(400);
  await tap(sync, { move: 700 });
  await sync.selectOption('on_commit');
  await S('hide');
  await sleep(1400);
  await point(app.locator('[data-testid="revisions-connect-submit"]'), { move: 900 });
  await sleep(2600);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}

