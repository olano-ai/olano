// The DeepWiki video: DeepWiki's public MCP server (no account) added from the MCP directory, its
// three tools, then a question about a public repository. Ines's tool steps are real calls to the
// server and the reply is built from what they returned (teams/deepwiki.py). Team: Tessellate Data.
// Olano's own repository is not indexed on DeepWiki, so the question is about langchain-ai/langgraph.
export const start = '/agents/ines/mcp';
export const ready = (app) => app.getByText('Add MCP server').first().waitFor({ timeout: 60000 });
export const brand = 'Ask a codebase how it works.';
export const keyboardWords = ['"How"', 'How', 'Hi'];
export const captions = [
  { eyebrow: 'MCP', h1: 'DeepWiki, from<br>the directory', p: 'A public server. No account.' },
  { eyebrow: 'Tools', h1: 'Three tools,<br>loaded', p: 'Saved, then reloaded into Ines.' },
  { eyebrow: 'Ask', h1: 'How is it<br>organised?', p: 'A public repository on GitHub.' },
  { eyebrow: 'Answer', h1: 'From the<br>wiki itself', p: 'Its sections, then its packages.' },
  { eyebrow: 'Verbose', h1: 'What DeepWiki<br>sent back', p: 'Every tool result, in full.' },
];
export const voice = [
  'DeepWiki writes a wiki for public GitHub repositories, and runs a public MCP server. Add it to Ines from the directory.',
  'Ines connects, and three tools appear. Save the server, then reload it into the running agent.',
  'Ask Ines how a repository is organised. Ines reads the wiki\'s outline, then asks DeepWiki the question.',
  'The answer lists the wiki\'s sections and the packages, as DeepWiki described them.',
  'Switch the chat to verbose, and every tool result is there, word for word.',
  'Olano. Ask a codebase how it works.',
];

export async function run({ app, S, sleep, tap, type, send, keyboardOff, scroll, scrollTo, nav, mark, PORTRAIT }) {
  // 0. the directory: DeepWiki, ready to connect
  mark('mcp');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(1400);
  await tap(app.getByText('Add MCP server').first(), { move: 700 });
  const d = app.locator('[role=dialog]').first();
  await d.getByText('Connect a service').first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(900);
  await type(d.locator('input[placeholder^="Search"]'), 'deepwiki', { cps: 14 });
  await keyboardOff();
  const preset = d.getByText('DeepWiki', { exact: true }).first();
  await preset.waitFor({ timeout: 60000 });
  await sleep(500);
  await tap(preset, { move: 500 });
  await d.getByText('Ready to connect').first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(1600);

  // 1. connect: the tools, saved, then reloaded into the running agent
  mark('tools');
  await S('caption', 1);
  await tap(d.getByRole('button', { name: /^Connect/ }).last(), { move: 700 });
  await d.getByText('Connection ready').first().waitFor({ timeout: 90000 });
  await S('hide');
  await d.getByText('read_wiki_structure').first().scrollIntoViewIfNeeded().catch(() => {});
  await sleep(2200);
  await tap(d.getByRole('button', { name: 'Save server' }), { move: 700 });
  await app.locator('main').getByText('Connected', { exact: true }).first().waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(700);
  await tap(app.getByRole('button', { name: 'Reload agent' }), { move: 700 });
  await app.getByText('Reloaded MCP tools').first().waitFor({ timeout: 90000 });
  await S('hide');
  await sleep(1500);

  // 2. the question
  mark('ask');
  await S('caption', 2);
  await nav('/chat/ines', { click: false });
  await app.locator('textarea').last().waitFor({ timeout: 30000 });
  await sleep(700);
  await send('How is langchain-ai/langgraph organised? Check DeepWiki.', { cps: 22 });
  await app.getByText('Calling read_wiki_structure').first().waitFor({ timeout: 90000 });
  await app.getByText('Calling ask_wiki_question').first().waitFor({ timeout: 90000 });
  await sleep(1200);

  // 3. the answer, built from the two results
  await app.getByText('Here is how', { exact: false }).first().waitFor({ timeout: 180000 });
  mark('answer');
  await S('caption', 3);
  await app.getByText('The full answer, with its sources', { exact: false }).first().waitFor({ timeout: 120000 });
  await sleep(600);
  await scrollTo(app.getByText('The wiki\'s sections').first(), PORTRAIT ? 110 : 150, 1200).catch(() => {});
  await sleep(2200);
  if (PORTRAIT) { await scroll(330, 1200); await sleep(1600); }
  else await sleep(1400);

  // 4. verbose: the tool results themselves
  mark('verbose');
  await S('caption', 4);
  await tap(app.getByText('Normal', { exact: true }).first(), { move: 700 });
  await app.getByText('Verbose', { exact: true }).first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(600);
  await scrollTo(app.getByText('Available pages for').first(), PORTRAIT ? 120 : 140, 1200).catch(() => {});
  await sleep(1800);
  await scroll(PORTRAIT ? 420 : 380, 1600);
  await sleep(1800);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
