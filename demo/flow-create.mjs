// The create video: a new agent from the New agent dialog (name, title, what it is for, its model),
// then the first conversation. Team: Casa Lumen, a small hotel (teams/create.py); serve.py gives the
// agent made during the take a scripted runner, so Maya answers.
export const start = '/agents';
export const ready = (app) => app.getByText('Housekeeping Planner').first().waitFor();
export const brand = 'Build your first agent today.';
export const keyboardWords = ['"Maya"', 'Maya', 'May'];
export const captions = [
  { eyebrow: 'New agent', h1: 'Like a<br>new hire', p: 'No code. One short form.' },
  { eyebrow: 'Name', h1: 'Give it<br>a name', p: 'A real one, like a colleague.' },
  { eyebrow: 'Job', h1: 'Describe<br>the job', p: 'In your own words.' },
  { eyebrow: 'Model', h1: 'Pick how<br>it thinks', p: 'Your own key, or Olano credits.' },
  { eyebrow: 'Ready', h1: 'Built in<br>a minute', p: 'A workspace and tools, set up for you.' },
  { eyebrow: 'First chat', h1: 'Say hello', p: 'It already knows the job.' },
];

export async function run({ app, S, sleep, tap, point, type, send, keyboardOff, scroll, scrollTo, nav, mark, PORTRAIT }) {
  mark('new');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(3400);
  await tap(app.locator('main').getByText('New agent').first(), { move: 900 });
  const d = app.locator('[role=dialog]');
  await d.waitFor();
  await sleep(1500);

  mark('name');
  await S('caption', 1);
  await type(d.getByPlaceholder('Merli the Analyst'), 'Maya', { cps: 7 });
  await keyboardOff();
  await sleep(900);
  const title = d.getByPlaceholder('Recruitment Specialist');
  await scrollTo(title, PORTRAIT ? 200 : 260, 700);
  await type(title, 'Guest Relations', { cps: 15 });
  await keyboardOff();
  await sleep(900);

  mark('job');
  await S('caption', 2);
  const about = d.getByPlaceholder('Short summary of what this agent does.');
  await scrollTo(about, PORTRAIT ? 200 : 260, 700);
  await type(about, 'Answers guests about rooms, breakfast and check-in.', { cps: 19 });
  await keyboardOff();
  await sleep(700);

  mark('model');
  await S('caption', 3);
  const model = d.locator('select').first();
  await scrollTo(model, PORTRAIT ? 220 : 280, 900);
  await d.locator('select option[value="olano:pro"]').first().waitFor({ state: 'attached', timeout: 90000 });
  await sleep(900);
  await point(model, { move: 600 }); await S('press'); await sleep(140);
  await model.selectOption('olano:pro'); await S('release');
  await sleep(4200);

  mark('ready');
  await S('caption', 4);
  await tap(d.getByText('Create agent'), { move: 800, dispatch: true });
  const card = app.locator('main .card', { hasText: 'Guest Relations' });
  await card.waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(2400);
  await tap(card, { move: 800, dispatch: true });
  await app.getByText('Identity').first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(2800);

  mark('chat');
  await S('caption', 5);
  await tap(app.locator('main').getByText('Chat', { exact: true }).first());
  await app.locator('textarea').last().waitFor();
  await sleep(500);
  await send('Good morning Maya! What time is breakfast?');
  await app.getByText('brought to your room').first().waitFor({ timeout: 90000 });
  await sleep(2600);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
