// The groups video: a group chat with three agents. One question to the room, the first member
// answers and hands it on by @name, the others answer in turn; then one agent asked by name, and
// the group's details. Team: Saltmarsh Cycles (teams/groups.py). The group is made in the take, and
// every turn goes through the product's own group routes and hand-off loop.
export const start = '/chat/mara';
export const ready = (app) => app.locator('textarea').last().waitFor();
export const brand = 'Put your agents in one room.';
export const keyboardWords = ['"Can"', 'Can', 'Call'];
export const captions = [
  { eyebrow: 'Group chat', h1: 'Several agents,<br>one room', p: 'Start one from the sidebar.' },
  { eyebrow: 'Members', h1: 'Pick who<br>is in it', p: 'The first one answers by default.' },
  { eyebrow: 'Ask', h1: 'One question,<br>to the room', p: 'The lead answers first.' },
  { eyebrow: 'Hand-off', h1: 'They answer<br>in turn', p: 'Each one names who is next.' },
  { eyebrow: 'By name', h1: 'Or ask<br>one of them', p: 'Type @ and pick an agent.' },
  { eyebrow: 'Details', h1: 'Who is<br>in the room', p: 'Members, in speaking order.' },
];
export const voice = [
  'Some questions need more than one agent. So put them in the same room.',
  'Start a group, give it a name, and pick the agents. Mara, Otto and Fern run a bike shop.',
  'Ask the room one question. Mara is first in the group, so Mara answers.',
  'Mara hands the question to Otto. Otto finds a slot, then asks Fern about the parts.',
  'To ask one agent directly, type an at sign and pick a name. Only Fern replies.',
  "The group's details list its members, and who answers first.",
  'Olano. Put your agents in one room.',
];

export async function run({ app, S, sleep, tap, point, type, send, keyboardOff, scroll, scrollTo, mark, PORTRAIT }) {
  const thread = app.locator('[data-testid="simple-group-thread"]');

  mark('room');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(1500);
  if (PORTRAIT) {       // a phone keeps the sidebar in a drawer
    await tap(app.locator('button[aria-label="Open navigation"]'), { move: 700 });
    await sleep(2200);
  } else {
    const side = app.locator('[data-testid="nav-bot-sections"]');
    await point(side.getByText('fern', { exact: true }), { move: 800 });
    await sleep(600);
    await point(side.getByText('otto', { exact: true }), { move: 700 });
    await sleep(800);
  }
  const fresh = app.getByText('New group chat…').first();
  await point(fresh, { move: 700 });
  await sleep(900);

  mark('members');
  await S('caption', 1);
  await tap(fresh, { move: 200 });
  const d = app.locator('[role=dialog][aria-label="New group chat"]');
  await d.waitFor({ timeout: 30000 });
  await sleep(500);
  await type(d.locator('#simple-newgroup-name'), 'Saturday jobs', { cps: 20 });
  await keyboardOff();
  await sleep(300);
  const pick = (n) => d.locator('.simple-pick', { hasText: n }).first();
  await scrollTo(pick('otto'), PORTRAIT ? 480 : 500, 700);
  for (const n of ['mara', 'otto', 'fern']) { await tap(pick(n), { move: 450 }); await sleep(300); }
  await sleep(1000);
  await tap(d.getByRole('button', { name: 'Create group' }), { move: 700, dispatch: true });
  await thread.getByText('Nobody has spoken').first().waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(1300);

  mark('ask');
  await S('caption', 2);
  await send("Can we service Lena's e-bike and fit new brake pads by Saturday?", { cps: 24 });
  await thread.getByText('is there a workshop slot').first().waitFor({ timeout: 90000 });
  await sleep(1500);

  mark('handoff');
  await S('caption', 3);
  await thread.getByText('Friday at 14:00').first().waitFor({ timeout: 120000 });
  await sleep(2600);
  await thread.getByText('put one aside').first().waitFor({ timeout: 120000 });
  await sleep(3200);

  mark('name');
  await S('caption', 4);
  const box = app.locator('textarea').last();
  await type(box, '@f', { cps: 5 });
  const option = app.locator('.simple-mention-menu [role=option]', { hasText: 'fern' }).first();
  await option.waitFor({ timeout: 30000 });
  await sleep(700);
  await point(option, { move: 500 });
  await S('press');
  await sleep(130);
  await option.dispatchEvent('mousedown');
  await S('release');
  await sleep(300);
  await type(box, 'how many sets are left after that?', { cps: 24 });
  await tap(app.locator('button[aria-label="Send"]').last(), { move: 420 });
  await keyboardOff();
  await S('hide');
  await thread.getByText("Monday's order list").first().waitFor({ timeout: 90000 });
  await sleep(2000);

  mark('details');
  await S('caption', 5);
  await tap(app.getByRole('button', { name: 'About this group' }).first(), { move: 800 });
  const panel = app.locator('[data-testid="simple-group-info"]');
  await panel.getByText('Answers by default').first().waitFor({ timeout: 30000 });
  if (!PORTRAIT) await point(panel.getByText('Answers by default').first(), { move: 800, dx: 62, dy: 16 });
  await sleep(3600);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
