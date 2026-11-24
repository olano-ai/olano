// The simple video: the chat app an account can be given instead of the full dashboard. The agent
// list, one question, the three detail levels, a photo attached, and a new agent made from there.
// Team: Willow Lane Vets (teams/simple.py, which switches the demo's one account to the simple
// interface the way the product stores it).
//
// The photo: a browser picks a file in the operating system's own dialog, which a recording of the
// page cannot show. The take presses the attach button and hands the file to the same file input.
import { fileURLToPath } from 'node:url';

const PHOTO = fileURLToPath(new URL('./assets/simple-vaccination-card.jpg', import.meta.url));

export const start = '/';
export const ready = (app) => app.locator('[data-simple-shell] textarea').last().waitFor();
export const brand = 'A chat app for the rest of the team.';
export const keyboardWords = ['"When"', 'When', 'What'];
export const captions = [
  { eyebrow: 'Simple chat', h1: 'Just agents<br>and chats', p: 'An interface you choose per account.' },
  { eyebrow: 'Ask', h1: 'Ask,<br>get an answer', p: 'Replies only, by default.' },
  { eyebrow: 'Detail', h1: 'Quiet, Normal,<br>Verbose', p: 'Show as much of the work as you like.' },
  { eyebrow: 'Attach', h1: 'Send<br>a photo', p: 'From the same message box.' },
  { eyebrow: 'New agent', h1: 'Add one<br>yourself', p: 'A name, and what it should do.' },
  { eyebrow: 'Ready', h1: 'It joins<br>the list', p: 'And answers its first message.' },
];
export const voice = [
  'Not everyone needs the full dashboard. Some accounts get this instead.',
  'At the front desk, ask Poppy when Biscuit can come in. Only the answer comes back.',
  'Want to see the work? One button switches from Quiet, to Normal, to Verbose.',
  'Attach a photo from the same box. Here, a vaccination card for Poppy to read.',
  'Staff can add an agent of their own: a name, and a sentence about the job.',
  'Milo joins the list, and answers straight away.',
  'Olano. A chat app for the rest of the team.',
];

export async function run({ app, frame, S, sleep, tap, point, type, send, keyboardOff, scroll, scrollTo, mark, PORTRAIT }) {
  const rail = app.locator('[data-simple-bot-rail]');
  const chat = app.locator('[data-testid="simple-chat"]');
  const openRail = async () => { await tap(app.locator('button[aria-label="Show my agents"]'), { move: 700 }); await sleep(900); };

  mark('app');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(1400);
  if (PORTRAIT) {       // a phone shows the conversation; the agents are in a drawer
    await openRail();
    await sleep(2200);
    await tap(rail.locator('.simple-row', { hasText: 'poppy' }), { move: 700 });
    await sleep(1000);
  } else {
    await point(rail.getByText('bram', { exact: true }), { move: 800 });
    await sleep(700);
    await point(rail.getByText('poppy', { exact: true }), { move: 600 });
    await sleep(700);
    await point(rail.getByText('tess', { exact: true }), { move: 700 });
    await sleep(1300);
  }
  await S('hide');

  mark('ask');
  await S('caption', 1);
  await send('When can Biscuit come in for a booster?', { cps: 22 });
  await chat.getByText('Shall I book one').first().waitFor({ timeout: 90000 });
  await sleep(1700);

  mark('detail');
  await S('caption', 2);
  const detail = app.locator('[data-testid="simple-detail-btn"]');
  await tap(detail, { move: 800 });
  await chat.getByText('Calling read_file').first().waitFor({ timeout: 30000 });
  await sleep(2100);
  await tap(detail, { move: 200 });
  await chat.locator('.simple-tool-card').first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(1000);
  await scroll(600, 900, '[data-testid="simple-chat"] .simple-tool-card');
  await sleep(1800);

  mark('attach');
  await S('caption', 3);
  const attach = app.locator('button[aria-label="Attach a file"]').last();
  await point(attach, { move: 800 });
  await sleep(120);
  await S('press');
  await sleep(140);
  await S('release');
  await app.locator('[data-simple-shell] input[type=file]').first().setInputFiles(PHOTO);
  await sleep(1100);
  await send("Here is Biscuit's vaccination card.", { cps: 22 });
  await chat.getByText('due this week').first().waitFor({ timeout: 90000 });
  await sleep(1600);

  mark('new');
  await S('caption', 4);
  if (PORTRAIT) await openRail();
  await tap(app.locator('.simple-newbot').first(), { move: 800 });
  const d = app.locator('[role=dialog]').last();
  const name = d.locator('#simple-newbot-name');
  await name.waitFor({ timeout: 30000 });
  await sleep(600);
  await type(name, 'Milo', { cps: 10 });
  await keyboardOff();
  await sleep(400);
  const about = d.locator('textarea').first();
  await scrollTo(about, PORTRAIT ? 220 : 300, 900);
  await type(about, 'Writes aftercare notes for pets going home.', { cps: 26 });
  await keyboardOff();
  await sleep(600);
  await tap(d.getByRole('button', { name: 'Create agent' }), { move: 700, dispatch: true });

  await app.getByText('Say hello to milo').first().waitFor({ timeout: 90000 });
  mark('ready');
  await S('caption', 5);
  await S('hide');
  if (!PORTRAIT) { await sleep(500); await point(rail.getByText('milo', { exact: true }).first(), { move: 800 }); }
  await sleep(700);
  await send('Hi Milo!');
  await chat.getByText('draft their aftercare notes').first().waitFor({ timeout: 90000 });
  await sleep(1800);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
