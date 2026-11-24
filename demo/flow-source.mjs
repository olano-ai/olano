// The from-source tutorial, in a terminal: clone, ./install.sh, olano setup, olano doctor, olano
// dashboard, then the dashboard itself. The terminal shows what the commands really print (the step
// headings of install.sh, the prompts of `olano setup`, a real `olano doctor` table, the start-up lines
// of `olano dashboard`); long installer output between the headings is left out. The dashboard at the
// end is the tour's demo team (teams/tour.py).
export const team = 'tour';
export const start = null;
export const brand = 'Open source, start to finish.';
export const captions = [
  { eyebrow: 'From source', h1: 'For<br>builders', p: 'Python 3.11 or newer, and uv.' },
  { eyebrow: 'Install', h1: '<code>./install.sh</code>', p: 'Dependencies, in one pass.' },
  { eyebrow: 'Setup', h1: '<code>olano setup</code>', p: 'Model, safety, first agent.' },
  { eyebrow: 'Check', h1: '<code>olano doctor</code>', p: 'Is everything in place?' },
  { eyebrow: 'Run', h1: '<code>olano dashboard</code>', p: 'The runtime and the web app.' },
];

const step = (t) => `<span class="b" style="color:#60a5fa">━━ ${t}</span>`;
const ok = (t) => `  <span class="ok">✓</span> ${t}`;
const P = '<span class="ok">PASS</span>  ', W = '<span class="warn">WARN</span>  ';

export async function run(c) {
  const { app, S, sleep, tap, openApp, term, mark, PORTRAIT } = c;
  const DW = PORTRAIT ? 16 : 30;
  const row = (name, st, detail) => `│ ${name.padEnd(20)} │ ${st} │ ${(detail.length > DW ? detail.slice(0, DW - 1) + '…' : detail).padEnd(DW)} │`;
  const line = (l, m, r) => l + '─'.repeat(22) + m + '─'.repeat(8) + m + '─'.repeat(PORTRAIT ? 18 : 32) + r;

  mark('source');
  await term.on();
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(1200);
  await term.run('git clone https://github.com/olano-ai/olano.git', { cps: 26 });
  await term.out("Cloning into 'olano'...");
  await sleep(900);
  await term.run('cd olano', { cps: 20, pause: 300 });
  await sleep(1300);

  mark('install');
  await S('caption', 1);
  await term.run('./install.sh', { cps: 16 });
  for (const s of ['Detecting OS', 'Ensuring Python ≥3.12', 'Installing uv (Python package manager)', 'Installing Python dependencies (this can take a few minutes)',
    'Installing Node.js + npm', 'Downloading Chromium browser for Playwright (~150 MB; Chromium only)', 'Installing pre-commit git hooks']) {
    await term.out(['', step(s)], { gap: 0 });
    await sleep(760);
  }
  await term.out(['', step('Done'), '', '  <span class="b">Olano is ready.</span> Then: uv run olano setup, and uv run olano dashboard.', ''], { gap: 60 });
  await sleep(1700);

  mark('setup');
  await S('caption', 2);
  await term.clear();
  await term.run('uv run olano setup', { cps: 18 });
  await term.out(['', '  🦁  <span class="b">Welcome to Olano</span>', '  Setup for your Olano home, vault, and agent.', '', ok('Olano home: /home/you/.olano'), ''], { gap: 70 });
  await sleep(500);
  await term.out(['<span class="b">2/5 Model</span>', 'Choose a model provider', '  1. Anthropic', '  2. OpenAI', '  3. OpenAI Codex (ChatGPT OAuth)', '  4. Google Gemini (GenAI)', '  5. Ollama (local)', "  Provider (type 'b' to go back) [1]: <span class=\"b\">2</span>", ''], { gap: 80 });
  await sleep(900);
  await term.out(['<span class="b">3/5 Safety</span>', 'Choose a safety level', '  1. 🛡️ Guided workspace + shell', '  2. 🛠️ Faster local coding', '  3. 🏭 Read-only planning', "  Selection (type 'b' to go back) [1]: <span class=\"b\">1</span>", ''], { gap: 80 });
  await sleep(900);
  await term.out(['<span class="b">4/5 First Agent</span>', 'Choose your first agent', '  1. 🦁 Merli  A flexible everyday assistant', '  2. 💼 Built-in specialist', '  3. ⚙️ Custom agent', "  Selection (type 'b' to go back) [1]: <span class=\"b\">1</span>", '  Agent name [Merli]: <span class="b">Merli</span>'], { gap: 80 });
  await sleep(2600);

  mark('check');
  await S('caption', 3);
  await term.clear();
  await term.run('uv run olano doctor', { cps: 18 });
  await term.out([
    line('┌', '┬', '┐'), `│ ${'Check'.padEnd(20)} │ Status │ ${'Detail'.padEnd(PORTRAIT ? 16 : 30)} │`, line('├', '┼', '┤'),
    row('Python >= 3.11', P, '3.13.18'), row('deepagents installed', P, 'found'), row('Docker available', P, 'docker info ok'),
    row('Ollama available', W, 'ollama not installed'), row('Vault initialized', P, '/home/you/.olano/vault'), row('Workspace valid', P, '/home/you/.olano/workspace'),
    row('Disk space >= 500MB', P, '50980MB free'), row('Configured agents', P, 'merli'), row('Model runtime', P, 'openai credentials available'),
    row('Model connectivity', P, "'merli': OK"),
    line('└', '┴', '┘'), '', '<span class="ok">✓ All required checks passed</span>',
  ], { gap: 110 });
  await sleep(3400);

  mark('run');
  await S('caption', 4);
  await term.clear();
  await term.run('uv run olano dashboard', { cps: 18 });
  await term.out(['Starting Olano Mission Control...', 'Dashboard: http://localhost:3200', '', '🔶 <span class="b">Olano</span>', '   Dashboard: <mark>http://localhost:3200</mark>', '   Runtime:   http://localhost:8765', '', 'Runtime ready. Press Ctrl+C to stop.'], { gap: 160 });
  await sleep(1500);
  await openApp('/overview', (a) => a.getByText('Scheduled tasks').first().waitFor({ timeout: 60000 }));
  await term.off();
  await sleep(1200);
  if (PORTRAIT) await sleep(1200); else { await tap(app.getByText('Manage agents'), { move: 1100 }); await app.getByText('Research Analyst').first().waitFor(); await S('hide'); }
  await sleep(3200);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
