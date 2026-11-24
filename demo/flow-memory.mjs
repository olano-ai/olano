// The memory video: MEMORY.md on the Memory page, a fact added by hand, today's daily log, the notes
// wiki, a search of the knowledge base, then a new chat in which the agent uses the fact just saved.
// Team: Juniper Lane Veterinary Clinic (teams/memory.py: a real index of the handbook, searched by
// the product's own kb_search; the chat answer is built from the memory files as they are then).
export const start = '/memory';
export const ready = (app) => app.getByText('Dr. Imani Castellan sees cats and dogs').first().waitFor();
export const brand = 'An agent that remembers.';
export const keyboardWords = ['"Biscuit"', 'Biscuit', 'Book'];
export const captions = [
  { eyebrow: 'Memory', h1: 'What Willa<br>remembers', p: 'A plain file you can read.' },
  { eyebrow: 'Teach', h1: 'Add a fact<br>yourself', p: 'Saved under Clients.' },
  { eyebrow: 'Daily log', h1: 'A page<br>for each day', p: "Today's notes, as a file." },
  { eyebrow: 'Notes', h1: 'A wiki<br>it keeps', p: 'Notes that link to each other.' },
  { eyebrow: 'Knowledge', h1: 'Documents,<br>searchable', p: 'Found by meaning, not keywords.' },
  { eyebrow: 'Recall', h1: 'A new chat,<br>same memory', p: 'Willa uses what was saved.' },
];
export const voice = [
  'Willa runs the front desk at a vet clinic. What Willa remembers is a plain file you can read.',
  'Tell Willa something new. This fact about Biscuit goes straight into memory.',
  'Shorter notes land in a daily log, one page for each day.',
  "Notes are Willa's own wiki: checklists and templates that link to each other.",
  "The clinic's handbook is indexed too, so a question finds the right page.",
  'Start a new chat and ask about Biscuit. Willa answers with the fact saved a minute ago.',
  'Olano. An agent that remembers.',
];

export async function run({ app, S, sleep, tap, point, type, send, keyboardOff, scroll, scrollTo, nav, mark, PORTRAIT }) {
  mark('memory');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(2600);
  await scroll(PORTRAIT ? 330 : 150, 1800);
  await sleep(3400);

  mark('teach');
  await S('caption', 1);
  const section = app.getByPlaceholder('e.g. Preferences');
  await scrollTo(section, PORTRAIT ? 200 : 300, 1000);
  await type(section, 'Clients', { cps: 14 });
  await keyboardOff();
  await type(app.locator('main textarea').last(), 'Biscuit is nervous in the waiting room. Book the first slot.', { cps: 24 });
  await keyboardOff();
  await sleep(500);
  await tap(app.getByRole('button', { name: 'Save fact' }), { move: 600 });
  const saved = app.locator('main').getByText('Biscuit is nervous in the waiting room').first();
  await saved.waitFor({ timeout: 30000 });
  await S('hide');
  await scrollTo(saved, PORTRAIT ? 330 : 380, 1100).catch(() => {});
  await sleep(2600);

  mark('daily');
  await S('caption', 2);
  const today = new Date().toISOString().slice(0, 10);
  await nav(`/files?path=memories/${today}.md&agent=willa`);
  await app.getByText('Dr. Halvorsen is away next Monday').first().waitFor({ timeout: 30000 });
  await sleep(5200);

  mark('notes');
  await S('caption', 3);
  await nav('/notes');
  const note = app.getByText('Front desk checklist').first();
  await note.waitFor({ timeout: 30000 });
  await sleep(1300);
  await tap(note, { move: 700 });
  await app.getByText('What Willa does every morning.').last().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(4400);

  mark('knowledge');
  await S('caption', 4);
  await nav('/knowledge');
  const query = app.locator('input[placeholder^="Search across"]');
  await query.waitFor({ timeout: 30000 });
  await sleep(900);
  await type(query, 'When can a puppy meet other dogs?', { cps: 22 });
  await keyboardOff();
  await tap(app.getByRole('button', { name: 'Search', exact: true }).last(), { move: 500 });
  const hit = app.getByText('one week after the second vaccination').first();
  await hit.waitFor({ timeout: 90000 });
  await S('hide');
  await sleep(500);
  await scrollTo(app.getByText('Results', { exact: true }).first(), PORTRAIT ? 80 : 110, 1200);
  await sleep(3800);

  mark('recall');
  await S('caption', 5);
  await nav('/chat/willa');
  await app.locator('textarea').last().waitFor({ timeout: 30000 });
  await sleep(700);
  await tap(app.locator('button[aria-label="New chat"]').first(), { move: 600 });
  await sleep(600);
  await send('Biscuit is coming in tomorrow. Anything to know?');
  await app.getByText('off the counter').first().waitFor({ timeout: 90000 });
  await sleep(3200);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
