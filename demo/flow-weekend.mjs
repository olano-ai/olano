// The weekend video: an agent reads two public events calendars that have no API (Gardens by the
// Bay and NParks), picks events on the coming Saturday and Sunday, and puts reminders on Olano's
// calendar. Remi's browser steps are the product's own tools run for real in the product's headless
// browser (set to Headless before the take) against the live sites; the reminders are the product's
// own scheduler_create, which is what the Calendar shows. Team: Brightwater Swim School
// (teams/weekend.py). Needs network access.
export const start = '/chat/remi';
export const ready = (app) => app.locator('textarea').last().waitFor({ timeout: 60000 });
export const brand = 'The weekend, planned.';
export const keyboardWords = ['"Plan"', 'Plan', 'Pl'];
export const captions = [
  { eyebrow: 'Ask', h1: 'Plan the<br>weekend', p: 'Two public events calendars.' },
  { eyebrow: 'Browser', h1: 'Reads what<br>is on', p: 'Closes the banners, as you would.' },
  { eyebrow: 'Reminders', h1: 'One for<br>each event', p: 'Added to Olano’s calendar.' },
  { eyebrow: 'Plan', h1: 'Saturday<br>and Sunday', p: 'Dates and times as the sites list them.' },
  { eyebrow: 'Calendar', h1: 'On the<br>calendar', p: 'Next to everything else scheduled.' },
  { eyebrow: 'Details', h1: 'What it<br>will say', p: 'The event, the place, the link.' },
];
export const voice = [
  "Remi assists the owner of a swim school. Ask Remi to plan a family weekend from what's on at Gardens by the Bay and NParks, and to put the best events on the calendar.",
  "Remi opens each site's events page in its own browser, closes the notices, and reads the listings.",
  'For each event it picks, Remi adds a reminder to the calendar, an hour before the start.',
  'The plan gives each event its day, time and place, with a link to the page it came from.',
  'In the Calendar, each reminder sits on its day and time, in the same place as every other scheduled task.',
  'Open one to see what Remi will send, and when.',
  'Olano. The weekend, planned.',
];

export async function run({ app, S, sleep, tap, point, send, scrollTo, nav, mark, PORTRAIT }) {
  // 0. the request
  mark('ask');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(1200);
  await send('Plan a family weekend: what is on at Gardens by the Bay and NParks this Saturday and Sunday? Put the best two or three on the calendar.', { cps: 30 });
  await app.getByText('Calling browser_navigate').first().waitFor({ timeout: 90000 });
  await sleep(1600);

  // 1. the browser steps
  mark('browse');
  await S('caption', 1);
  await app.getByText('Calling browser_snapshot').first().waitFor({ timeout: 180000 });
  await sleep(2400);

  // 2. the reminders, one call each
  await app.getByText('Calling scheduler_create').first().waitFor({ timeout: 120000 });
  mark('remind');
  await S('caption', 2);
  await sleep(5600);

  // 3. the plan
  const plan = app.getByText('Here is a plan').last();
  await plan.waitFor({ timeout: 90000 });
  mark('plan');
  await S('caption', 3);
  await app.getByText('as the sites list them').first().waitFor({ timeout: 90000 });
  await sleep(500);
  await scrollTo(plan, PORTRAIT ? 140 : 150, 1100).catch(() => {});
  if (!PORTRAIT) await point(app.locator('main li strong').last(), { move: 900 }).catch(() => {});
  await sleep(4200);

  // 4. the Calendar: both weekend days (the month on a desktop; on a phone, the month's cells are
  // too narrow for a name, so the day view of Saturday, then Sunday)
  let chip;
  if (PORTRAIT) {
    const d = new Date();
    d.setDate(d.getDate() + ((6 - d.getDay() + 7) % 7));
    const pad = (n) => String(n).padStart(2, '0');
    await nav(`/calendar/day?date=${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`);
    chip = app.getByText(/^Weekend: /).first();
    await chip.waitFor({ timeout: 30000 });
    mark('calendar');
    await S('caption', 4);
    await sleep(800);
    await scrollTo(chip, 330, 1000).catch(() => {});
    await sleep(2600);
    await tap(app.getByRole('button', { name: 'Next' }).first(), { move: 700 });
    await sleep(1200);
    chip = app.getByText(/^Weekend: /).first();
    await chip.waitFor({ timeout: 30000 });
    await scrollTo(chip, 330, 900).catch(() => {});
    await sleep(1600);
  } else {
    await nav('/calendar/month');
    chip = app.getByText(/^Weekend: /).first();
    await chip.waitFor({ timeout: 30000 });
    mark('calendar');
    await S('caption', 4);
    await sleep(800);
    await scrollTo(chip, 340, 1000).catch(() => {});
    await point(chip, { move: 900 }).catch(() => {});
    await sleep(3800);
  }

  // 5. one reminder, opened
  mark('detail');
  await S('caption', 5);
  await tap(chip, { move: 700 });
  const dialog = app.locator('[role=dialog]').last();
  await dialog.getByText('Remind the family').first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(4200);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
