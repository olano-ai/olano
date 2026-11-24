// The cortex video: what an agent does between conversations. A Pulse finding, then the funnel from
// brainstorm to idea to prototype to project, with one promotion approved in the take.
// Team: Fernway Garden Centre (teams/cortex.py seeds every item, rating and Pulse card with the
// product's own functions; the approval runs the real promotion path with a scripted worker).
export const start = '/cortex';
export const ready = (app) => app.getByText('Care card scans doubled on Saturday').first().waitFor();
export const brand = 'Agents with their own ideas.';
export const captions = [
  { eyebrow: 'Pulse', h1: 'It noticed<br>something', p: 'A finding, with a next step.' },
  { eyebrow: 'Brainstorm', h1: 'Loose angles<br>come first', p: 'Each agent keeps its own.' },
  { eyebrow: 'Ideas', h1: 'Each idea<br>gets a score', p: 'With the reason behind it.' },
  { eyebrow: 'Approve', h1: 'You decide<br>what moves on', p: 'Approve, and the build starts.' },
  { eyebrow: 'Prototype', h1: 'A small<br>working version', p: 'With its files and results.' },
  { eyebrow: 'Project', h1: 'A plan,<br>step by step', p: 'Progress counted from the plan.' },
];
export const voice = [
  "Between conversations, Sage keeps working. Here Sage noticed something in the shop's numbers.",
  'New work starts as brainstorms: loose angles on a question, kept by each agent.',
  'The strongest angles become ideas. Each one is rated out of ten, with the reason.',
  'This idea scored nine, so Sage asks to take it further. You approve.',
  'The idea becomes a prototype: a small working version, with its files and results.',
  'And a prototype that proves itself becomes a project, with a plan and its progress.',
  'Olano. Agents with their own ideas.',
];

export async function run({ app, S, sleep, tap, point, scroll, scrollTo, mark, PORTRAIT }) {
  const tab = (name) => app.locator('main button', { hasText: new RegExp('^' + name) }).first();
  const card = (title) => app.locator('main .card', { hasText: title }).first();
  const openTab = async (name) => {
    await tab(name).scrollIntoViewIfNeeded().catch(() => {});
    await sleep(250);
    await tap(tab(name), { move: 700 });
  };
  const open = async (title) => {
    await card(title).waitFor({ timeout: 30000 });
    if (PORTRAIT) await scrollTo(card(title), 300, 700).catch(() => {});
    await sleep(PORTRAIT ? 300 : 900);
    await tap(card(title).getByRole('button', { name: 'Open', exact: true }), { move: 800 });
    await app.getByText('Files', { exact: true }).last().waitFor({ timeout: 30000 });
    await S('hide');
  };
  const close = async () => {
    await tap(app.getByRole('button', { name: 'Close', exact: true }).first(), { move: 600 });
    await sleep(500);
  };

  mark('pulse');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(1800);
  await scrollTo(tab('Pulse'), PORTRAIT ? 70 : 90, 1400);
  if (!PORTRAIT) await point(card('Care card scans doubled'), { move: 900 });
  await sleep(3200);

  mark('brainstorm');
  await S('caption', 1);
  await openTab('Brainstorming');
  await card('Things customers ask').waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(2000);
  await scroll(PORTRAIT ? 260 : 200, 1400);
  await sleep(1900);

  mark('ideas');
  await S('caption', 2);
  await scrollTo(tab('Pulse'), PORTRAIT ? 70 : 90, 700);
  await openTab('Ideas');
  await open('Text me when');
  await sleep(3300);
  await close();

  mark('approve');
  await S('caption', 3);
  await openTab('Self-Improve');
  const approve = app.getByRole('button', { name: 'Approve', exact: true }).first();
  await approve.waitFor({ timeout: 30000 });
  await S('hide');
  if (PORTRAIT) await scrollTo(card('Promote idea to prototype'), 150, 900).catch(() => {});
  await sleep(2100);
  await tap(approve, { move: 800 });
  await app.getByText('Agent is working on this').first().waitFor({ timeout: 60000 });
  await S('hide');
  await app.getByText('into prototype').first().waitFor({ timeout: 120000 });
  await sleep(1900);

  mark('prototype');
  await S('caption', 4);
  await openTab('Prototypes');
  await open('Back-in-stock texts');
  await sleep(3500);
  await close();

  mark('project');
  await S('caption', 5);
  await openTab('Projects');
  await open('Care cards for the houseplant');
  await sleep(4300);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
