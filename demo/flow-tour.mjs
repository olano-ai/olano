// The tour: Olano in under a minute. Overview → the agents → a chat with tool steps → the org chart →
// the scheduler and calendar → an approval before an email goes out → end card.
// Team: Harbor & Pine, an online home-goods shop (teams/tour.py).
export const start = '/overview';
export const ready = (app, { layout }) => app.getByText('Scheduled tasks').first().waitFor();
export const brand = 'Open source. Yours to run.';
export const keyboardWords = ['"Which"', 'Which', 'What'];
export const captions = [
  { eyebrow: 'Meet Olano', h1: 'Your AI team,<br>in one place', p: 'Agents with their own memory, tools and jobs.' },
  { eyebrow: 'Agents', h1: 'Each one<br>has a job', p: 'Research, support, writing, operations.' },
  { eyebrow: 'Chat', h1: 'Ask, and watch<br>it work', p: 'Every step shown as it happens.' },
  { eyebrow: 'Team', h1: 'They work<br>together', p: 'Agents hand work to each other.' },
  { eyebrow: 'Schedule', h1: 'On the clock', p: 'Reports and checks, on a timetable.' },
  { eyebrow: 'Control', h1: 'You approve<br>what matters', p: 'Agents ask before they send.' },
];

export async function run({ app, S, sleep, tap, point, send, scroll, nav, mark, PORTRAIT }) {
  const composer = app.locator('textarea').last();

  // Intro: the Overview
  mark('intro');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(6200);

  // 1. Agents: the roster, then one agent
  mark('agents');
  await S('caption', 1);
  if (PORTRAIT) await nav('/agents'); else await tap(app.getByText('Manage agents'));
  await app.getByText('Research Analyst').first().waitFor();
  await sleep(2600);
  await tap(app.locator('.card', { hasText: 'Research Analyst' }), { dispatch: true });
  await app.getByText('Identity').first().waitFor();
  await S('hide');
  await sleep(3200);

  // 2. Chat: a question, the steps, the answer
  mark('chat');
  await S('caption', 2);
  await tap(app.locator('main').getByText('Chat', { exact: true }).first());
  await composer.waitFor();
  await sleep(500);
  await send('Which products sold best last week?');
  await app.getByText('Open in Files').first().waitFor({ timeout: 90000 });
  await sleep(2400);

  // 3. Team: the org chart
  mark('team');
  await S('caption', 3);
  await nav('/orgchart');
  await app.getByText('Org Chart').first().waitFor();
  await sleep(1500);
  if (!PORTRAIT) { await point(app.getByText('Head of Operations').first(), { move: 800 }); await sleep(900); await point(app.getByText('Research Analyst').first(), { move: 900 }); }
  await sleep(3600);
  await S('hide');

  // 4. Schedule: the tasks, then the calendar
  mark('schedule');
  await S('caption', 4);
  await nav('/scheduler');
  await app.getByText('Daily sales report').first().waitFor();
  await sleep(1500);
  await scroll(PORTRAIT ? 380 : 300, 1400);
  await sleep(1500);
  await nav('/calendar');
  await app.getByText('Today').first().waitFor();
  await sleep(3200);

  // 5. Control: the agent asks before it sends
  mark('control');
  await S('caption', 5);
  if (PORTRAIT) await nav('/chat/aria'); else await tap(app.locator('aside.sidebar a[href="/chat/aria"]'));
  await composer.waitFor();
  await sleep(500);
  await send('Reply to Dana about her late order.');
  const accept = app.getByRole('button', { name: 'Accept once' });
  await accept.waitFor({ timeout: 90000 });
  await sleep(2600);
  await tap(accept, { move: 800 });
  await app.getByText('Sent. Dana has').first().waitFor({ timeout: 90000 });
  await S('hide');
  await sleep(2400);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
