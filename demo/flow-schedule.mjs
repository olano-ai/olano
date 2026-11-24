// The schedule video: the Scheduler tab, a new weekday task, the calendar, Run now, and the run as its
// own conversation. Team: Brightside Lettings (teams/schedule.py). The launcher's scheduler is a
// stand-in (serve.py): Run now plays the agent's scripted turn on a fresh scheduled thread.
export const start = '/scheduler';
export const ready = (app) => app.getByText('Viewing reminders').first().waitFor();
export const brand = 'The morning report, ready.';
export const keyboardWords = ['"Rent"', 'Rent', 'Run'];
export const captions = [
  { eyebrow: 'Schedule', h1: 'Work that<br>runs itself', p: 'No one has to remember.' },
  { eyebrow: 'Create', h1: 'Weekdays<br>at eight', p: 'Pick the days and the time.' },
  { eyebrow: 'Calendar', h1: 'The week<br>at a glance', p: "Every agent's jobs together." },
  { eyebrow: 'Try it', h1: 'Run it<br>now', p: 'No need to wait for morning.' },
  { eyebrow: 'Result', h1: 'Done before<br>you’re in', p: 'Each run is its own thread.' },
];

export async function run({ app, frame, S, sleep, tap, point, type, keyboardOff, scroll, scrollTo, nav, mark, PORTRAIT }) {
  mark('schedule');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(4600);

  mark('create');
  await S('caption', 1);
  await tap(app.getByText('New task').first(), { move: 800 });
  const d = app.locator('[role=dialog]');
  await d.waitFor();
  await sleep(500);
  await type(d.getByPlaceholder('Daily email digest'), 'Rent arrears check', { cps: 20 });
  await keyboardOff();
  const agent = d.locator('select').first();
  await point(agent, { move: 450 }); await S('press'); await sleep(120);
  await agent.selectOption('piper'); await S('release');
  await sleep(350);
  await type(d.locator('textarea'), 'Who is behind on rent, and by how much?', { cps: 24 });
  await keyboardOff();
  await tap(d.getByText('Weekly', { exact: true }), { move: 500 });
  await sleep(300);
  for (const day of ['Tue', 'Wed', 'Thu', 'Fri']) await tap(d.getByText(day, { exact: true }), { move: 240, hold: 70 });
  const time = d.locator('input[type=time]');
  await scrollTo(time, PORTRAIT ? 300 : 380, 600).catch(() => {});
  await point(time, { move: 450 }); await S('press'); await sleep(120);
  await time.fill('08:00'); await S('release');
  await sleep(900);
  await d.getByText('Runs every week').first().scrollIntoViewIfNeeded().catch(() => {});
  await sleep(1100);
  await tap(d.getByText('Create task'), { move: 700, dispatch: true });
  await app.getByText('Rent arrears check').first().waitFor();
  await S('hide');
  await sleep(1700);

  mark('calendar');
  await S('caption', 2);
  await nav('/calendar/week');
  await app.getByText('Today').first().waitFor();
  await sleep(4800);

  mark('run');
  await S('caption', 3);
  await nav('/scheduler');
  const row = app.locator('.card', { hasText: 'Rent arrears check' }).last();
  await row.waitFor();
  await scrollTo(row, PORTRAIT ? 250 : 300, 900).catch(() => {});
  await sleep(900);
  await tap(row.locator('button[title="Run now"], button[aria-label="Run now"]').first(), { move: 900 });
  await sleep(4200);          // the run finishes before its conversation is opened
  await S('hide');

  mark('result');
  await S('caption', 4);
  await nav('/chat/piper');
  await app.getByText('Three tenants are behind').first().waitFor({ timeout: 60000 });
  await sleep(6200);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
