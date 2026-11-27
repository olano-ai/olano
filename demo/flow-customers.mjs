// The customers video: one support agent serving many customers, each kept apart. Nell's Multi-user
// tab turns customer service mode on and adds one allowed action (my_order, a script that reads the
// asking customer's id from its environment). Two customers then write to the shop's Telegram bot:
// their messages are Bot API updates handed to the product's own Telegram adapter and runtime handler
// (teams/_telegram.py: only Telegram's network is stood in for), and Nell's tool steps run the
// product's own per-customer tools for real (teams/customers.py). The chat shows each conversation as
// the dashboard shows any Telegram chat; Files shows the per-customer folders before and after
// forget_me. Team: Inkwell & Ivy, an online stationery shop.
export const start = '/agents/nell/multiuser';
export const ready = (app) => app.getByText('Multi-user mode', { exact: true }).first().waitFor({ timeout: 60000 });
export const brand = 'One agent, every customer kept apart.';
export const captions = [
  { eyebrow: 'Multi-user', h1: 'One agent,<br>many customers', p: 'Customer service mode, switched on.' },
  { eyebrow: 'Allowed action', h1: 'A script that<br>knows who asks', p: 'Olano passes the customer in its environment.' },
  { eyebrow: 'Telegram', h1: 'Maya asks<br>about her order', p: 'A note saved, the script run for her.' },
  { eyebrow: 'Isolation', h1: 'Tomas sees<br>only his own', p: 'Nothing about Maya in his memory.' },
  { eyebrow: 'Files', h1: 'A folder<br>per customer', p: 'What Nell saved, kept apart.' },
  { eyebrow: 'Forget me', h1: 'Erased<br>on request', p: "<code>forget_me</code> deletes Maya's folder." },
];
export const voice = [
  'Nell answers the customers of Inkwell and Ivy, a stationery shop, on Telegram. Customer service mode gives every customer a private memory.',
  "Nell may run one script, my order. Olano hands it the customer's Telegram id in its environment, so Nell never passes it.",
  "Maya writes to the shop's bot. Nell saves a note in Maya's own memory, and the script finds her order.",
  "Tomas asks about his order, and about Maya's. The script finds only his, and his memory has nothing on Maya.",
  "In Nell's files, each customer has a folder of their own.",
  "When Maya asks to be forgotten, forget me deletes her folder. Tomas's stays as it was.",
  'Olano. One agent, every customer kept apart.',
];

const BOT = 'http://127.0.0.1:' + (process.env.DEMO_CUSTOMERS_PORT || '8961');
const MAYA = { id: 6123400871, first_name: 'Maya', last_name: 'Ortiz', username: 'maya_ortiz' };
const TOMAS = { id: 7020455512, first_name: 'Tomas', last_name: 'Berg', username: 'tomasberg' };
/** A Telegram message from a customer, delivered to the agent's Telegram adapter (teams/_telegram.py). */
async function telegram(user, text) {
  const r = await fetch(BOT + '/message', { method: 'POST', body: JSON.stringify({ agent: 'nell', user, text }) });
  if (!r.ok) throw new Error('telegram seam: ' + r.status);
}

export async function run({ app, S, sleep, tap, point, type, keyboardOff, scroll, scrollTo, nav, mark, PORTRAIT }) {
  // 0. customer service mode
  mark('mode');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(900);
  const mode = app.getByText('Multi-user mode', { exact: true }).first();
  await scrollTo(mode, PORTRAIT ? 90 : 110, 1400);
  await sleep(700);
  const sw = app.locator('.card', { has: mode }).getByRole('switch').first();
  await tap(sw, { move: 800 });
  await app.getByText('Allowed actions', { exact: true }).first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(2400);

  // 1. the allowed action, and its script
  mark('action');
  await S('caption', 1);
  const card = app.locator('.card', { has: app.getByText('Allowed actions', { exact: true }) }).first();
  await scrollTo(card, PORTRAIT ? 70 : 90, 1200);
  await sleep(500);
  const add = card.getByRole('button', { name: 'Add action' });
  await scrollTo(add, PORTRAIT ? 420 : 380, 800).catch(() => {});
  await tap(add, { move: 700 });
  await type(card.getByPlaceholder('action name (e.g. add_bill)'), 'my_order', { cps: 14 });
  await type(card.getByPlaceholder(/script path/), 'actions/my_order.py', { cps: 18 });
  await keyboardOff();
  await tap(card.getByRole('button', { name: 'Save actions' }), { move: 600 });
  await app.getByText('Actions saved').first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(900);
  await nav('/files?path=actions/my_order.py&agent=nell', { click: false });
  const env = app.getByText('LIONCLAW_CUSTOMER_ID').first();
  await env.waitFor({ timeout: 30000 });
  await sleep(800);
  if (PORTRAIT) await scrollTo(env, 330, 900).catch(() => {});
  await point(env, { move: 900 });
  await sleep(2600);

  // 2. Maya writes on Telegram
  mark('maya');
  await S('caption', 2);
  await nav('/chat/nell', { click: false });
  const verbosity = app.getByText('Normal', { exact: true }).first();
  await verbosity.waitFor({ timeout: 30000 });
  await sleep(700);
  await tap(verbosity, { move: 700 });          // verbose: each tool's result, as the agent got it
  await app.getByText('Verbose', { exact: true }).first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(500);
  await telegram(MAYA, 'Hi! Where is my order?');
  const mayaRow = app.locator('aside').getByText('Hi! Where is').first();
  if (PORTRAIT) {
    await sleep(900);
    await nav('/chat/nell?thread=' + MAYA.id, { click: false });
  } else {
    await mayaRow.waitFor({ timeout: 30000 });
    await sleep(500);
    await tap(mayaRow, { move: 800 });
  }
  await app.getByText('It should arrive').first().waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(600);
  await scrollTo(app.getByText('"customer": "' + MAYA.id + '"').first(), PORTRAIT ? 300 : 260, 1000).catch(() => {});
  if (!PORTRAIT) await point(app.getByText('"customer": "' + MAYA.id + '"').first(), { move: 800 });
  await sleep(PORTRAIT ? 1800 : 2400);
  if (PORTRAIT) { await scroll(400, 1200); await sleep(1600); }

  // 3. Tomas writes: his own order, and nothing about Maya
  mark('tomas');
  await S('caption', 3);
  await telegram(TOMAS, 'Hey, where is my order? And what did Maya order?');
  const tomasRow = app.locator('aside').getByText('Hey, where is').first();
  if (PORTRAIT) {
    await sleep(900);
    await nav('/chat/nell?thread=' + TOMAS.id, { click: false });
  } else {
    await tomasRow.waitFor({ timeout: 30000 });
    await sleep(400);
    await tap(tomasRow, { move: 800 });
  }
  await app.getByText('I can only see your own account').first().waitFor({ timeout: 60000 });
  await S('hide');
  const nothing = app.getByText('Nothing matching in this customer').first();
  await scrollTo(nothing, PORTRAIT ? 470 : 420, 1000).catch(() => {});
  if (!PORTRAIT) await point(nothing, { move: 800 });
  await sleep(PORTRAIT ? 3400 : 3600);

  // 4. one folder per customer
  mark('folders');
  await S('caption', 4);
  await nav(`/files?path=customers/telegram-${MAYA.id}/MEMORY.md&agent=nell`, { click: false });
  const fact = app.getByText('Name: Maya Ortiz').first();
  await fact.waitFor({ timeout: 30000 });
  await sleep(700);
  if (!PORTRAIT) {
    await point(app.getByText('telegram-' + TOMAS.id).first(), { move: 800 });
    await sleep(1000);
    await point(fact, { move: 800 });
    await sleep(2600);
  } else {
    await point(fact, { move: 800 });
    await sleep(1600);
    await tap(app.getByText('All files', { exact: true }).first(), { move: 700 });   // the phone shows one pane
    await app.getByText('telegram-' + TOMAS.id).first().waitFor({ timeout: 30000 });
    await S('hide');
    await sleep(500);
    await point(app.getByText('telegram-' + TOMAS.id).first(), { move: 700 });
    await sleep(1600);
  }

  // 5. Maya asks to be forgotten
  mark('forget');
  await S('caption', 5);
  await nav('/chat/nell?thread=' + MAYA.id, { click: false });
  await app.getByText('It should arrive').first().waitFor({ timeout: 30000 });
  await sleep(700);
  await telegram(MAYA, 'Thanks! Please forget me now.');
  await app.getByText('I will start fresh').first().waitFor({ timeout: 60000 });
  await S('hide');
  await scrollTo(app.getByText('Deleted all stored memory').first(), PORTRAIT ? 420 : 400, 900).catch(() => {});
  await sleep(2000);
  await nav('/files?path=customers&agent=nell', { click: false });
  await sleep(600);
  const back = app.getByText('All files', { exact: true }).first();
  if (await back.isVisible().catch(() => false)) await tap(back, { move: 700 });
  const tomasDir = app.getByText('telegram-' + TOMAS.id).first();
  await tomasDir.waitFor({ timeout: 30000 });
  const refresh = app.locator('button[title*="efresh" i], button[aria-label*="efresh" i]').first();
  if (await refresh.isVisible().catch(() => false)) await tap(refresh, { move: 700 });
  await app.getByText('telegram-' + MAYA.id).first().waitFor({ state: 'detached', timeout: 30000 });
  await S('hide');
  await sleep(500);
  await point(tomasDir, { move: 800 });
  await sleep(2800);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
