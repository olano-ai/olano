// The editor tutorial (ACP, the Agent Client Protocol), in a terminal. Everything the terminal shows is
// what the commands printed on a real run, kept in assets/acp-session.json by assets/acp-capture.py:
// `olano acp --help`, a Zed settings file with Olano as a custom agent (Zed's documented
// `agent_servers` format), a real ACP session with the real `olano acp --agent sage` server, driven by
// a small client on the official `acp` SDK (assets/acp-client.py) that stands in for the editor, and the
// note the agent edited. No editor is drawn. The agent (sage, at the made-up Copperline Labs) answers
// from a script, as in every demo; the throwaway home is shown as /home/you.
import fs from 'node:fs';
import path from 'node:path';
import { DIR } from './common.mjs';

const LOG = JSON.parse(fs.readFileSync(path.join(DIR, 'assets/acp-session.json'), 'utf8'));
export const server = false;
export const start = null;
export const brand = 'In the editor you already use.';
export const captions = [
  { eyebrow: 'ACP', h1: 'Olano in<br>your editor', p: 'Over the Agent Client Protocol.' },
  { eyebrow: 'Editor', h1: 'One entry<br>in Zed', p: 'A custom agent in <code>settings.json</code>.' },
  { eyebrow: 'Connect', h1: 'The editor<br>starts it', p: 'Here, a small test client.' },
  { eyebrow: 'Ask', h1: 'Ask without<br>leaving', p: 'Its steps and its answer, streamed.' },
  { eyebrow: 'Work', h1: 'Same agent,<br>same files', p: 'It edits a note in its workspace.' },
];
export const voice = [
  'Editors such as Zed talk to agents over the Agent Client Protocol. Olano speaks it, with one command.',
  'In Zed, add Olano as a custom agent: the command, and the name of the agent to start.',
  'The editor starts the agent itself and opens a session. Here, a small test client stands in for the editor.',
  'Ask a question. The agent reads its own notes, and the answer streams back as it is written.',
  'It is the same agent you run in Olano, with the same workspace. Ask it to update a note, and the file changes.',
  'Olano. In the editor you already use.',
];

const esc = (s) => s.replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c]);
const dim = (s) => `<span class="dim">${esc(s)}</span>`;
/** Word-wrap one line of output to the terminal's width. */
function wrap(text, width) {
  const lines = [''];
  for (const word of text.split(' ')) {
    if (lines.at(-1) && (lines.at(-1) + ' ' + word).length > width) lines.push(word);
    else lines[lines.length - 1] += (lines.at(-1) ? ' ' : '') + word;
  }
  return lines;
}

export async function run(c) {
  const { S, sleep, term, mark, PORTRAIT } = c;
  const W = PORTRAIT ? 70 : 90;
  /** One of the client's output lines: tool and status lines at once, the answer word by word. */
  async function out(line) {
    if (line.startsWith('[')) { await term.out(line.startsWith('[tool]') ? `<span class="hi">[tool]</span>${esc(line.slice(6))}` : dim(line)); await sleep(650); return; }
    for (const row of wrap(line, W)) {
      await term.out('', { gap: 0 });
      let shown = '';
      for (const word of row.split(' ')) { shown += (shown ? ' ' : '') + word; await term.replace(esc(shown)); await sleep(75); }
    }
    await sleep(300);
  }
  /** The client's own "> " prompt, with the question typed at it. */
  async function ask(text) {
    await term.out(['', '<span class="b">&gt;</span> '], { gap: 0 });
    await sleep(900);
    let typed = '';
    for (const ch of text) { typed += ch; await term.replace(`<span class="b">&gt; ${esc(typed)}</span>`); await sleep(48 + Math.random() * 40); }
    await sleep(500);
  }

  // 0. the command
  mark('acp');
  await term.on();
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(1300);
  await term.run('olano acp --help', { cps: 16 });
  await term.out(LOG.help.map((l) => (l.includes('--agent') ? esc(l).replace('-a, --agent TEXT', '<mark>-a, --agent TEXT</mark>') : esc(l))), { gap: 110 });
  await term.prompt();
  await sleep(4200);

  // 1. the editor's side: one entry in Zed's settings
  mark('editor');
  await S('caption', 1);
  await term.clear();
  await term.run('cat ~/.config/zed/settings.json', { cps: 20 });
  await term.out(LOG.settings.map((l) => (/"(command|args)"/.test(l) ? l.replace(/^(\s*)(.*?)(,?)$/, (_, sp, body, comma) => `${sp}<mark>${esc(body)}</mark>${comma}`) : esc(l))), { gap: 130 });
  await term.prompt();
  await sleep(5200);

  // 2. a real session: the client starts `olano acp` and connects
  mark('connect');
  await S('caption', 2);
  await term.clear();
  await term.run('python acp-client.py sage', { cps: 18 });
  await sleep(1400);                                 // the agent is built before it answers
  for (const l of LOG.client[0].out) { await term.out(esc(l).replace(/^(\w+):/, '<span class="ok">$1:</span>')); await sleep(750); }
  await sleep(3400);

  // 3. a question; the tool call and the answer arrive as session updates
  mark('ask');
  await S('caption', 3);
  await ask(LOG.client[1].prompt);
  await sleep(600);
  for (const l of LOG.client[1].out) await out(l);
  await sleep(2600);

  // 4. the same agent and workspace: it edits a note, and the file shows it
  mark('work');
  await S('caption', 4);
  await ask(LOG.client[2].prompt);
  await sleep(600);
  for (const l of LOG.client[2].out) await out(l);
  await term.out(['', '<span class="b">&gt;</span> '], { gap: 0 });   // an empty line ends the client
  await sleep(900);
  await term.run('cat ~/.olano/agents/sage/workspace/notes/checklist.md', { cps: 30 });
  const last = LOG.checklist.length - 1;
  await term.out(LOG.checklist.map((l, i) => (i === last ? `<mark>${esc(l)}</mark>` : esc(l))), { gap: 120 });
  await term.prompt();
  await sleep(4600);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
