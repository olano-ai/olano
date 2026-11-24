// The control video: an approval before an email goes out, the vault, Insights and Revisions.
// Team: Marlow & Finch, an accounting firm (teams/control.py seeds the vault, a month of usage and a
// few commits with the product's own code).
export const team = 'control';
export const start = '/chat/aria';
export const ready = (app) => app.locator('textarea').last().waitFor();
export const brand = 'Your agents, your rules.';
export const keyboardWords = ['"Send"', 'Send', 'See'];
export const captions = [
  { eyebrow: 'Approvals', h1: 'It asks<br>first', p: 'Before anything leaves.' },
  { eyebrow: 'Decide', h1: 'Read it,<br>then choose', p: 'Approve or decline.' },
  { eyebrow: 'Vault', h1: 'Keys,<br>locked away', p: 'Encrypted, in one place.' },
  { eyebrow: 'Insights', h1: 'Know what<br>it costs', p: 'By model and by day.' },
  { eyebrow: 'History', h1: 'Every change,<br>kept', p: 'See what changed, and when.' },
];

export async function run({ app, S, sleep, tap, point, send, scroll, scrollTo, nav, mark, PORTRAIT }) {
  mark('approvals');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(1500);
  await send('Send Priya the Q3 VAT return.');
  const accept = app.getByRole('button', { name: 'Accept once' });
  await accept.waitFor({ timeout: 90000 });
  await sleep(1400);

  mark('decide');
  await S('caption', 1);
  await scrollTo(app.locator('.approval-card').first(), PORTRAIT ? 110 : 150, 900).catch(() => {});
  if (!PORTRAIT) await point(app.locator('.approval-card pre, .approval-card code').first(), { move: 900 }).catch(() => {});
  await sleep(3200);
  await tap(accept, { move: 800 });
  await app.getByText('Sent. I will let you know').first().waitFor({ timeout: 90000 });
  await S('hide');
  await sleep(1800);

  mark('vault');
  await S('caption', 2);
  await nav('/vault');
  await app.getByText('GMAIL_APP_PASSWORD').first().waitFor({ timeout: 30000 });
  await sleep(2200);
  await scroll(PORTRAIT ? 260 : 200, 1500);
  await sleep(2600);

  mark('insights');
  await S('caption', 3);
  await nav('/insights');
  const chart = app.getByText('API cost over time').first();
  await chart.waitFor({ timeout: 30000 });
  await sleep(1500);
  await scrollTo(chart, PORTRAIT ? 60 : 90, 1400);
  await sleep(4200);

  mark('history');
  await S('caption', 4);
  await nav('/revisions');
  const commit = app.getByText('aria: turn in chat').first();
  await commit.waitFor({ timeout: 30000 });
  await sleep(600);
  await scrollTo(app.getByText('History', { exact: true }).first(), PORTRAIT ? 40 : 60, 1200);
  await sleep(700);
  await tap(commit, { move: 700 });
  await app.getByText('file changed').first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(800);
  await scroll(PORTRAIT ? 300 : 260, 1500);
  await sleep(3600);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
