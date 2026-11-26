// The Hugging Face video: Hugging Face's MCP server added as a custom server with no account (its
// directory entry signs in with OAuth; anonymously the server still lists four tools), hf_whoami
// switched off, then a model search in the chat. Sekar's tool steps are real calls to the server and
// the reply is built from what they returned (teams/huggingface.py). Team: Lontar Labs.
export const start = '/agents/sekar/mcp';
export const ready = (app) => app.getByText('Add MCP server').first().waitFor({ timeout: 60000 });
export const brand = 'Find open models from the chat.';
export const keyboardWords = ['"Find"', 'Find', 'Fi'];
export const captions = [
  { eyebrow: 'MCP', h1: 'Add a<br>custom server', p: 'Just the address. No account.' },
  { eyebrow: 'Tools', h1: 'Four tools,<br>one off', p: 'hf_whoami is not needed here.' },
  { eyebrow: 'Ask', h1: 'Find a<br>model', p: 'A Hub search, then the details.' },
  { eyebrow: 'Answer', h1: 'A pick, with<br>its licence', p: 'From the cards the Hub returned.' },
  { eyebrow: 'Verbose', h1: 'What the Hub<br>sent back', p: 'Every tool result, in full.' },
];
export const voice = [
  "Hugging Face runs an MCP server for its Hub. Sekar's team adds it as a custom server: just the address, no account.",
  'Sekar connects, and four tools appear. One reads the signed-in account, so it is switched off.',
  'Ask Sekar for an Indonesian speech-to-text model. Sekar searches the Hub, then reads the top results.',
  'The answer lists each model with its licence and languages, and picks one, with its link.',
  'In verbose mode, the chat shows what the Hub sent back, word for word.',
  'Olano. Find open models from the chat.',
];

export async function run({ app, S, sleep, tap, point, type, send, keyboardOff, scroll, scrollTo, nav, mark, PORTRAIT }) {
  // 0. a custom server: the address and the transport
  mark('mcp');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(1400);
  await tap(app.getByText('Add MCP server').first(), { move: 700 });
  const d = app.locator('[role=dialog]').first();
  await d.getByText('Connect a service').first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(800);
  await tap(d.getByText('Custom', { exact: true }).first(), { move: 600 });
  await sleep(400);
  await type(d.locator('input[placeholder="https://example.com/mcp"]'), 'https://huggingface.co/mcp', { cps: 18 });
  await keyboardOff();
  const transport = d.locator('select').first();
  await sleep(300);
  // A native dropdown opens outside the page, where a recording cannot see it: pick the option directly.
  await S('move', ...(await transport.boundingBox().then((b) => [b.x + b.width / 2, b.y + b.height / 2])), 600);
  await sleep(700);
  await S('press'); await transport.selectOption('http'); await S('release');
  await S('hide');
  await sleep(1200);

  // 1. connect: four tools; hf_whoami off; saved and reloaded
  mark('tools');
  await S('caption', 1);
  await tap(d.getByRole('button', { name: /^Connect/ }).last(), { move: 700 });
  await d.getByText('Connection ready').first().waitFor({ timeout: 90000 });
  await S('hide');
  // The first tool in the list is hf_whoami (checked: the switch's row names it)
  const whoami = d.getByRole('switch').first();
  await d.locator('code', { hasText: /^hf_whoami$/ }).first().scrollIntoViewIfNeeded().catch(() => {});
  const row = await whoami.evaluate((el) => el.parentElement.textContent);
  if (!row.includes('hf_whoami')) throw new Error('first switch is not hf_whoami: ' + row.slice(0, 80));
  await sleep(1400);
  await tap(whoami, { move: 700 });
  await sleep(1400);
  await tap(d.getByRole('button', { name: 'Save server' }), { move: 700 });
  await app.locator('main').getByText('Connected', { exact: true }).first().waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(600);
  await tap(app.getByRole('button', { name: 'Reload agent' }), { move: 700 });
  await app.getByText('Reloaded MCP tools').first().waitFor({ timeout: 90000 });
  await S('hide');
  await sleep(1300);

  // 2. the request
  mark('ask');
  await S('caption', 2);
  await nav('/chat/sekar', { click: false });
  await app.locator('textarea').last().waitFor({ timeout: 30000 });
  await sleep(700);
  await send('Find an open speech-to-text model for Indonesian.', { cps: 22 });
  await app.getByText('Calling hub_repo_search').first().waitFor({ timeout: 90000 });
  await app.getByText('Calling hub_repo_details').first().waitFor({ timeout: 90000 });
  await sleep(1000);

  // 3. the answer, built from the two results
  await app.getByText('I searched the Hub', { exact: false }).first().waitFor({ timeout: 180000 });
  mark('answer');
  await S('caption', 3);
  await app.getByText('My pick', { exact: false }).first().waitFor({ timeout: 120000 });
  await sleep(900);
  await scrollTo(app.getByText('I searched the Hub').first(), PORTRAIT ? 110 : 150, 1200).catch(() => {});
  await sleep(2000);
  if (PORTRAIT) { await scroll(300, 1200); await sleep(1800); }
  else { await point(app.getByText('My pick', { exact: false }).first(), { move: 800 }); await sleep(1800); }

  // 4. verbose: the tool results themselves
  mark('verbose');
  await S('caption', 4);
  await tap(app.getByText('Normal', { exact: true }).first(), { move: 700 });
  await app.getByText('Verbose', { exact: true }).first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(600);
  await scrollTo(app.getByText('Found ', { exact: false }).first(), PORTRAIT ? 120 : 140, 1200).catch(() => {});
  await sleep(1800);
  await scroll(PORTRAIT ? 420 : 380, 1600);
  await sleep(1800);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
