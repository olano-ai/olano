// The install tutorial: the one-line Docker install in a terminal, then the real first-run screens
// (owner account with the setup code, the wizard's vault, AI provider and Agent Father panels).
// Nothing is mocked: it records an Olano that the installer has just started. Before each take the
// install is put back to "no accounts" by DEMO_INSTALL_RESET (a script that re-runs setup.sh on a
// wiped data directory and prints the new setup code); the terminal replays the installer's own
// output, copied from a real run.
import { execFileSync } from 'node:child_process';

export const server = false;
export const app = process.env.DEMO_INSTALL_URL || 'http://localhost:3200';
export const start = null;                       // the terminal comes first
export const brand = 'Running on your own machine.';
export const address = 'localhost:3200';
export const keyboardWords = ['"Sam"', 'Sam', 'Same'];
export const captions = [
  { eyebrow: 'Install', h1: 'One line<br>to start', p: 'Linux, Mac, or Windows with WSL 2.' },
  { eyebrow: 'Setup', h1: 'It sets<br>itself up', p: 'Three containers, started for you.' },
  { eyebrow: 'Address', h1: 'Address<br>and code', p: 'Printed when it finishes.' },
  { eyebrow: 'Account', h1: 'Create<br>your admin', p: 'The code proves it’s you.' },
  { eyebrow: 'Models', h1: 'Choose your<br>models', p: 'Your own keys, or Olano credits.' },
  { eyebrow: 'Agent Father', h1: 'Meet<br>Agent Father', p: 'It builds the others.' },
  { eyebrow: 'Options', h1: 'Your domain,<br>a sandbox', p: '<code>--host</code> and <code>--sandbox</code>' },
];

let CODE = 'ABCD-EFGH-JKMN';
export async function init() {
  const reset = process.env.DEMO_INSTALL_RESET;
  if (!reset) throw new Error('DEMO_INSTALL_RESET is not set: see demo/README.md, "The installation tutorials"');
  CODE = execFileSync('bash', [reset], { encoding: 'utf8' }).trim().split('\n').pop();
  console.log('fresh install, setup code', CODE);
}

const ok = (t) => `<span class="ok">✓</span> ${t}`;
const go = (t) => `<span class="hi">▶</span> ${t}`;

export async function run(c) {
  const { app, S, sleep, tap, point, type, keyboardOff, scroll, scrollTo, openApp, term, mark, PORTRAIT } = c;
  const CURL = 'curl -fsSL https://raw.githubusercontent.com/olano-ai/olano/main/install.sh | bash';

  // 0. the one line
  mark('install');
  await term.on();
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(1300);
  await term.run(CURL, { cps: 26, pause: 900 });
  await sleep(1500);

  // 1. the installer's own output
  mark('setup');
  await S('caption', 1);
  await term.out([
    ok('Docker and the Compose plugin are available'),
    go('Downloading the self-host files (main) into /home/you/olano…'),
    ok('Files are in /home/you/olano'),
    ok('Created .env from .env.example'),
    ok('Set LIONCLAW_PG_PASSWORD in .env'),
    ok('Set OLANO_SETUP_CODE in .env'),
    ok('Set OLANO_SITE_ADDRESS in .env'),
  ], { gap: 170 });
  await term.out(go('Pulling the images…'));
  await sleep(700);
  for (const img of ['postgres:16', 'ghcr.io/olano-ai/olano-backend:latest', 'ghcr.io/olano-ai/olano-dashboard:latest']) { await term.out(`<span class="dim"> Image ${img} Pulled</span>`); await sleep(520); }
  await term.out(go('Starting…'));
  await term.out([
    ' Network olano_olano Created', ' Volume olano_pgdata Created',
    ' Container olano_postgres Started', ' Container olano_postgres Healthy',
    ' Container olano Started', ' Container olano_web Started',
  ].map((l) => `<span class="dim">${l}</span>`), { gap: 260 });
  await term.out(go('Waiting for the runtime…'));
  await sleep(1500);

  // 2. the address and the setup code
  mark('address');
  await S('caption', 2);
  await term.out([
    ok('Olano is up: <mark>http://localhost:3200</mark>'),
    '  Open it in a browser; the setup wizard runs on the first visit.',
    '',
    '  ' + '='.repeat(PORTRAIT ? 44 : 64),
    `    Setup code:  <mark>${CODE}</mark>`,
    '    The dashboard asks for it once, to create the first admin',
    '    account. It is also in .env (OLANO_SETUP_CODE) and in the log',
    '    (docker compose logs olano).',
    '  ' + '='.repeat(PORTRAIT ? 44 : 64),
  ], { gap: 120 });
  await term.prompt();
  await sleep(5200);

  // 3. the first account
  mark('account');
  await S('caption', 3);
  await openApp('/', (a) => a.getByText('Create your account').first().waitFor({ timeout: 60000 }));
  await term.off();
  await sleep(1400);
  await type(app.locator('input[name=name]'), 'Sam Rivera', { cps: 16 });
  await type(app.locator('input[type=email]'), 'sam@example.com', { cps: 22 });
  await type(app.locator('input[type=password]'), 'correct-horse-battery-7', { cps: 30 });
  await keyboardOff();
  const code = app.locator('input[name=setup-code]');
  await scrollTo(code, PORTRAIT ? 260 : 330, 700).catch(() => {});
  await type(code, CODE, { cps: 11 });
  await keyboardOff();
  await sleep(700);
  await tap(app.getByText('Create owner account'), { move: 600, dispatch: true });
  const begin = app.getByText("Let's set it up");
  await begin.waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(2600);
  // the vault passphrase panel, quickly
  await tap(begin, { move: 700, dispatch: true });
  const pw = app.locator('.ob-root input[type=password]');
  await pw.nth(2).waitFor({ timeout: 30000 });
  await pw.nth(0).fill('correct-horse-battery-7');
  await pw.nth(1).fill('a-separate-vault-passphrase');
  await pw.nth(2).fill('a-separate-vault-passphrase');
  await sleep(900);
  await tap(app.locator('.ob-profile-submit'), { move: 600, dispatch: true });
  const cont = app.locator('.ob-root').getByText('Continue', { exact: true });
  await cont.waitFor({ timeout: 60000 });
  await sleep(700);

  // 4. how the models are paid for
  mark('models');
  await S('caption', 4);
  await tap(cont, { move: 500, dispatch: true });
  await app.getByText('Run on Olano credits').first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(2400);
  const byok = app.getByText('Or bring your own keys').first();
  await scrollTo(byok, PORTRAIT ? 120 : 160, 1500);
  await sleep(3600);

  // 5. Agent Father
  mark('father');
  await S('caption', 5);
  await app.locator('.ob-root').getByText('Skip for now', { exact: true }).last().dispatchEvent('click');
  await app.getByText('Creates agents').first().waitFor({ timeout: 60000 });
  await sleep(2200);
  if (!PORTRAIT) await point(app.getByText('Creates agents').first(), { move: 900 });
  await sleep(1200);
  await scroll(PORTRAIT ? 330 : 220, 1700);
  await sleep(2800);
  await S('hide');

  // 6. the two options worth knowing
  mark('options');
  await S('caption', 6);
  await term.clear();
  await term.on();
  await sleep(700);
  await term.out('<span class="dim"># a public https address on your own domain, and a sandbox per agent</span>', { gap: 0 });
  await term.run('curl -fsSL …/install.sh | bash -s -- --host olano.example.com --sandbox', { cps: 24, pause: 400 });
  await sleep(5200);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
