// The monetization video: an agent sold by the month, with Stripe and Telegram Stars. Lumi's Stripe key
// (a Stripe sandbox's test-mode key) is in her vault; the owner sets the plan on her Monetization tab
// (a real test-mode price created in that sandbox for this video) and restarts her. A learner then
// writes on Telegram: Bot API updates handed to the product's own Telegram adapter and runtime handler
// (teams/_telegram.py; only Telegram's network is stood in for). Her first message starts the trial;
// after the trial (played out: its stored start is moved back three days, teams/monetization.py) her
// next message meets the product's paywall, whose Stripe rail creates a real test-mode Checkout
// session. The terminal prints the Bot API calls the bot made (assets/telegram-outbox.py) and that
// session as Stripe reports it (assets/stripe-session.py). No payment is made: checkout.stripe.com is
// not reachable from the recording machine, and Stars need the live bot.
// Team: Puente Spanish, an online Spanish-tutoring service.
import { spawn } from 'node:child_process';
import path from 'node:path';
import { DIR, REPO } from './common.mjs';

export const start = '/agents/lumi/variables';
export const ready = (app) => app.getByText('STRIPE_API_KEY').first().waitFor({ timeout: 60000 });
export const brand = 'Sell your agent by the month.';
export const keyboardWords = ['"Keep"', 'Keep', 'Kept'];
export const captions = [
  { eyebrow: 'Vault', h1: 'The Stripe key,<br>in the vault', p: 'A sandbox key in test mode. Never in the config.' },
  { eyebrow: 'Monetization', h1: 'A plan,<br>two ways to pay', p: 'A free trial, then Stripe or Telegram Stars.' },
  { eyebrow: 'Free trial', h1: 'Hana starts<br>her trial', p: 'Her first message on Telegram starts it.' },
  { eyebrow: 'Three days later', h1: 'The trial<br>is over', p: 'Her next message gets the paywall.' },
  { eyebrow: 'Stripe', h1: 'A real<br>checkout session', p: "Test mode, tied to Hana's chat." },
];
export const voice = [
  "Puente sells Lumi, its Spanish tutor on Telegram, by the month. Lumi's Stripe key is in her vault, never in her config.",
  'On the Monetization tab: a three day free trial, then nine dollars a month with Stripe, or four hundred fifty Telegram Stars.',
  'Hana writes to Lumi on Telegram. Her first message starts the free trial, and Lumi checks it for her.',
  'Three days on, the trial is over. Her next message gets the paywall: a Stripe link, and a Stars invoice that needs the live bot to send.',
  "The Stripe link is a real checkout session in test mode, tied to Hana's chat. Stripe's webhook tells Olano when she pays.",
  'Olano. Sell your agent by the month.',
];

const PORT = process.env.DEMO_MONETIZATION_PORT || '8962';
const BOT = 'http://127.0.0.1:' + PORT;
const HANA = { id: 5823019447, first_name: 'Hana', last_name: 'Kobayashi', username: 'hana_k' };
const PRICE_ID = 'price_1UgEVeCO5K7xqxurO81Hubo5';   // test mode, in the demo's Stripe sandbox
const esc = (s) => s.replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c]);

/** A Telegram message from Hana, delivered to Lumi's Telegram adapter (teams/_telegram.py). */
async function telegram(text) {
  const r = await fetch(BOT + '/message', { method: 'POST', body: JSON.stringify({ agent: 'lumi', user: HANA, text }) });
  if (!r.ok) throw new Error('telegram seam: ' + r.status);
}
async function calls(since = 0) { return (await (await fetch(`${BOT}/calls?agent=lumi&since=${since}`)).json()); }
/** Runs one of the demo's scripts; resolves with the lines it printed. */
function script(name, args, env = {}) {
  return new Promise((resolve, reject) => {
    const p = spawn('uv', ['run', '--quiet', 'python', path.join(DIR, 'assets', name), ...args],
      { cwd: REPO, env: { ...process.env, ...env }, stdio: ['ignore', 'pipe', 'pipe'] });
    let out = '', err = '';
    p.stdout.on('data', (d) => { out += d; });
    p.stderr.on('data', (d) => { err += d; });
    p.on('exit', (code) => (code === 0 ? resolve(out.trimEnd().split('\n')) : reject(new Error(name + ' failed\n' + err.slice(-1500)))));
  });
}

export async function run({ app, S, sleep, tap, point, type, keyboardOff, scroll, scrollTo, nav, term, mark, PORTRAIT }) {
  // 0. the Stripe key, in Lumi's vault
  mark('vault');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(900);
  const key = app.getByText('STRIPE_API_KEY').first();
  await scrollTo(key, PORTRAIT ? 260 : 300, 1000).catch(() => {});
  await point(key, { move: 900 });
  await sleep(1400);
  await point(app.getByText('Stripe sandbox (test mode) secret key').first(), { move: 700 });
  await sleep(2400);

  // 1. the plan: trial, Stripe price, Stars amount; saved, then a restart
  mark('plan');
  await S('caption', 1);
  await nav('/agents/lumi/monetization', { click: false });
  const sw = (label) => app.getByText(label, { exact: true }).first().locator('xpath=ancestor::div[.//button[@role="switch"]][1]').getByRole('switch');
  await app.getByText('Charge end-users', { exact: true }).first().waitFor({ timeout: 30000 });
  await sleep(600);
  await tap(sw('Charge end-users'), { move: 700 });
  await sleep(300);
  if (!PORTRAIT) await point(app.locator('.card input[type=number]').first(), { move: 600 });   // the trial: 3 days
  await sleep(500);
  await scrollTo(app.getByText('Payment rails', { exact: true }).first(), PORTRAIT ? 80 : 90, 900);
  await tap(sw('Stripe'), { move: 600 });
  await tap(sw('Telegram Stars'), { move: 500 });
  const field = (label) => app.locator('.field', { has: app.getByText(label, { exact: true }) }).locator('input, textarea').first();
  await scrollTo(app.getByText('Pricing', { exact: true }).first(), PORTRAIT ? 70 : 90, 900);
  await field('Price (cents)').fill('');
  await type(field('Price (cents)'), '900', { cps: 10 });
  await field('Telegram Stars amount').fill('');
  await type(field('Telegram Stars amount'), '450', { cps: 10 });
  await type(field('Stripe Price ID'), PRICE_ID, { cps: 40 });
  await field('Product name').fill('');
  await type(field('Product name'), 'Lumi Spanish, monthly', { cps: 34 });
  await field('Offer copy').fill('');
  await type(field('Offer copy'), 'Keep practising with Lumi every day.', { cps: 40 });
  await keyboardOff();
  const preview = app.getByText('Users will see:').first();
  await scrollTo(preview, PORTRAIT ? 420 : 470, 800).catch(() => {});
  await point(preview, { move: 700, dx: PORTRAIT ? 0 : 60 });
  await sleep(1200);
  const save = app.getByRole('button', { name: 'Save pricing' });
  await scrollTo(save, PORTRAIT ? 520 : 600, 800).catch(() => {});
  await tap(save, { move: 600 });
  await sleep(900);
  await tap(app.getByRole('button', { name: 'Restart to apply' }), { move: 600 });
  await app.getByRole('button', { name: 'Restart to apply' }).waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(1600);

  // 2. Hana's first message starts her trial
  mark('trial');
  await S('caption', 2);
  await nav('/chat/lumi', { click: false });
  await app.locator('textarea').last().waitFor({ timeout: 30000 });
  await sleep(500);
  await telegram('Hola! Can we practise ordering at a café? Am I on the free trial?');
  const row = app.locator('aside').getByText('Hola! Can we').first();
  if (PORTRAIT) {
    await sleep(900);
    await nav('/chat/lumi?thread=' + HANA.id, { click: false });
  } else {
    await row.waitFor({ timeout: 30000 });
    await sleep(400);
    await tap(row, { move: 800 });
  }
  await app.getByText('how would you order a tea').first().waitFor({ timeout: 60000 });
  await S('hide');
  if (!PORTRAIT) await point(app.getByText('Calling subscription_status').first(), { move: 800 });
  await sleep(PORTRAIT ? 2600 : 3000);

  // 3. three days later (played out): the next message meets the paywall
  mark('paywall');
  await S('caption', 3);
  const before = (await calls()).next;
  await fetch(BOT + '/end-trial', { method: 'POST', body: JSON.stringify({ user_id: String(HANA.id) }) });
  await telegram('Quisiera un té, por favor. Is that right?');
  let recorded = [];
  for (let i = 0; i < 60 && !recorded.some((c) => /Send \/subscribe/.test(c.text || '')); i++) {
    recorded = (await calls(before)).calls;
    await sleep(300);
  }
  const W = PORTRAIT ? 64 : 88;
  const outbox = await script('telegram-outbox.py', ['lumi', String(HANA.id), String(before)], { OUTBOX_WIDTH: String(W) });
  await term.clear();
  await term.on();
  await sleep(600);
  await term.out('<span class="dim"># what Lumi\'s bot sent Hana just now</span>', { gap: 0 });
  await term.run(`python telegram-outbox.py lumi ${HANA.id}`, { cps: 24, pause: 400 });
  for (const line of outbox) {
    let html = esc(line);
    if (/^(sendMessage|createInvoiceLink)/.test(line)) html = `<span class="ok">${html}</span>`;
    else if (line.startsWith('Bot API calls')) html = `<span class="dim">${html}</span>`;
    else if (/\[.*Subscribe\]|XTR/.test(line)) html = `<span class="b">${html}</span>`;
    await term.out(html, { gap: 0 });
    await sleep(line ? 160 : 60);
  }
  await term.prompt();
  await sleep(PORTRAIT ? 4200 : 4600);

  // 4. the Stripe session behind the Subscribe link, as Stripe reports it
  mark('checkout');
  await S('caption', 4);
  const link = recorded.flatMap((c) => (c.reply_markup || []).flat()).find((b) => /checkout\.stripe\.com/.test(b.url || ''));
  const session = (link?.url.match(/cs_test_[A-Za-z0-9]+/) || [])[0];
  if (!session) throw new Error('no test-mode checkout session in the paywall');
  const looked = await script('stripe-session.py', [session]);
  await term.out(`<span class="dim"># SESSION=${session.slice(0, 22)}… (from the Subscribe link)</span>`, { gap: 0 });
  await term.run('python stripe-session.py "$SESSION"', { cps: 24, pause: 400 });
  for (const line of looked) {
    let html = esc(line);
    if (/^(livemode|client_reference_id|amount)/.test(line)) html = `<span class="b">${html}</span>`;
    await term.out(html, { gap: 0 });
    await sleep(140);
  }
  await term.prompt();
  await sleep(PORTRAIT ? 5200 : 5600);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
