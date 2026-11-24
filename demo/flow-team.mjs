// The team video: an org chart, one agent asking two others, the exchange on record, issues that
// agents and people share, and the one answer that comes back. Team: Fieldnote Studio (teams/team.py).
export const start = '/orgchart';
export const ready = (app) => app.getByText('Org Chart').first().waitFor();
export const brand = 'Put a team on it.';
export const keyboardWords = ['"Draft"', 'Draft', 'Do'];
export const captions = [
  { eyebrow: 'Org chart', h1: 'A team,<br>not a chatbot', p: 'Each agent knows who to ask.' },
  { eyebrow: 'Hand-off', h1: 'One asks<br>another', p: 'You see who is working.' },
  { eyebrow: 'On record', h1: 'Every<br>exchange', p: 'Read what they said.' },
  { eyebrow: 'Issues', h1: 'Work,<br>tracked', p: 'One tracker for agents and people.' },
  { eyebrow: 'Result', h1: 'One answer<br>back', p: 'From the whole team.' },
];

export async function run({ app, S, sleep, tap, point, send, scroll, scrollTo, nav, mark, PORTRAIT }) {
  mark('org');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(1800);
  if (!PORTRAIT) { await point(app.getByText('Project Lead').first(), { move: 900 }); await sleep(1100); await point(app.getByText('Researcher').first(), { move: 900 }); await sleep(900); await point(app.getByText('Copywriter').first(), { move: 800 }); }
  await sleep(PORTRAIT ? 4200 : 1500);
  await S('hide');

  mark('handoff');
  await S('caption', 1);
  if (PORTRAIT) await nav('/chat/atlas'); else await tap(app.locator('aside.sidebar a[href="/chat/atlas"]'));
  await app.locator('textarea').last().waitFor();
  await sleep(500);
  await send('Draft a launch plan for the Tidewater refill kit.');
  await app.getByText('Calling talk_to_agent').nth(1).waitFor({ timeout: 90000 });
  await app.getByText('I opened three issues').first().waitFor({ timeout: 120000 });
  await sleep(600);

  mark('record');
  await S('caption', 2);
  await nav('/activity');
  await app.getByText('What should we know before launching Tidewater?').first().waitFor({ timeout: 30000 });
  await sleep(1600);
  await scroll(PORTRAIT ? 420 : 330, 1600, '[data-testid="a2a-rail"] ~ *');
  await sleep(3200);

  mark('issues');
  await S('caption', 3);
  await nav('/issues');
  const first = app.getByText('Tidewater: teaser Reels and waitlist page').first();
  await first.waitFor();
  await sleep(2200);
  await tap(first, { move: 800 });
  await app.getByText('Picking this up').first().waitFor();
  await S('hide');
  await sleep(3600);

  mark('result');
  await S('caption', 4);
  await nav('/chat/atlas');
  const plan = app.getByText('Here is the launch plan').first();
  await plan.waitFor({ timeout: 30000 });
  await sleep(700);
  await scrollTo(plan, PORTRAIT ? 90 : 110, 1400);
  await sleep(4600);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
