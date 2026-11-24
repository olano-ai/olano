// The chat video: one question, the agent's steps as they happen, a chart in the answer, the report as
// a file, then a different model for the follow-up. Team: Kettle Hill Coffee (teams/chat.py); the
// chart is made by the product's own charts skill (chart.mjs → assets/chat-sales.png).
export const start = '/chat/nova';
export const ready = (app) => app.locator('textarea').last().waitFor();
export const brand = 'Agents that show their work.';
export const keyboardWords = ['"How"', 'How', 'Hi'];
export const captions = [
  { eyebrow: 'Ask', h1: 'One<br>question', p: '“How did sales go last month?”' },
  { eyebrow: 'Work', h1: 'See every<br>step', p: 'Reading the file, running the numbers.' },
  { eyebrow: 'Chart', h1: 'An answer<br>you can see', p: 'Drawn right in the chat.' },
  { eyebrow: 'Files', h1: 'The report,<br>saved', p: 'Download it or open it in Files.' },
  { eyebrow: 'Models', h1: 'Switch<br>mid-chat', p: 'The conversation carries on.' },
];

export async function run({ app, frame, S, sleep, tap, point, send, scroll, scrollTo, mark, PORTRAIT }) {
  mark('ask');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(2600);
  await send('How did sales go last month?');

  await app.getByText('Calling read_file').first().waitFor({ timeout: 60000 });
  mark('work');
  await S('caption', 1);
  await app.getByText('Calling write_file').first().waitFor({ timeout: 90000 });
  await sleep(900);

  await app.getByText('Open in Files').first().waitFor({ timeout: 90000 });
  mark('chart');
  await S('caption', 2);
  await sleep(700);
  const chart = app.locator('main img').last();
  await chart.waitFor();
  await scrollTo(chart, PORTRAIT ? 150 : 130, 1300);
  await sleep(4600);

  mark('files');
  await S('caption', 3);
  const card = app.getByText('Open in Files').last();
  await scrollTo(card, PORTRAIT ? 420 : 520, 1000);
  await sleep(500);
  await tap(card, { move: 800 });
  await app.getByText('What drove it').first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(3800);

  mark('models');
  await S('caption', 4);
  await frame.evaluate(() => history.back());
  await app.locator('textarea').last().waitFor();
  await sleep(900);
  await tap(app.getByText('olano:pro').first(), { move: 800 });
  const option = app.getByText('gpt-6.1-sol', { exact: false }).first();
  await option.waitFor({ timeout: 60000 });
  await sleep(1100);
  await tap(option, { move: 600 });
  await sleep(700);
  await send('Why was October so strong?');
  await app.getByText('close to September').first().waitFor({ timeout: 90000 });
  await sleep(2600);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
