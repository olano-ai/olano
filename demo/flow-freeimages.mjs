// The freeimages video: Pipeworx's public gateway for Wikimedia Commons
// (https://gateway.pipeworx.io/commons-wikimedia/mcp, no account) added to an agent as a custom server during the take, a search for free-to-use photos for
// a café poster, then the credit lines saved as a file. The search and file_info steps are real calls to that server
// (cached after the first success, since anonymous use is capped), and the reply is built from what it
// returned (teams/freeimages.py). Team: Little Fig Café. Needs network.
export const start = '/agents/otto/mcp';
export const ready = (app) => app.getByText('Add MCP server').first().waitFor({ timeout: 60000 });
export const timezone = 'Asia/Singapore';
export const brand = 'Free photos, credits included.';
export const keyboardWords = ['"Find"', 'Find', 'Yes', 'ht'];
export const captions = [
  { eyebrow: 'MCP', h1: 'Wikimedia Commons,<br>by its address', p: "Pipeworx's public gateway, no key." },
  { eyebrow: 'Connect', h1: 'Search and<br>licence tools', p: 'Saved, then loaded into Otto.' },
  { eyebrow: 'Ask', h1: 'Photos for<br>a poster', p: 'Free to use, even commercially.' },
  { eyebrow: 'Answer', h1: 'Photos with<br>their licences', p: 'As Wikimedia Commons lists them.' },
  { eyebrow: 'Credits', h1: 'Credit lines,<br>written out', p: 'Saved to a file, ready to paste.' },
];
export const voice = [
  "Hana runs Little Fig Café. She adds Wikimedia Commons to Otto, her marketing agent, through Pipeworx's public gateway. No key needed.",
  'It connects with a search tool, and one that reads each file\'s licence.',
  'Hana asks Otto for free-to-use photos of latte art for a café poster.',
  'Otto searches Commons, checks each licence, and lists photos she can use, with links to their pages.',
  'Then the part people skip: Otto writes out the credit lines and saves them to a file.',
  'Olano. Free photos, credits included.',
];

export async function run({ app, S, sleep, tap, point, type, send, keyboardOff, scrollTo, nav, mark, PORTRAIT }) {
  mark('add');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(1000);
  await tap(app.getByText('Add MCP server').first(), { move: 800 });
  const d = app.locator('[role=dialog]').first();
  await d.getByText('Notion', { exact: true }).first().waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(500);
  await tap(d.getByRole('button', { name: 'Custom', exact: true }), { move: 700 });
  await sleep(400);
  await type(d.locator('input[placeholder="https://example.com/mcp"]'), 'https://gateway.pipeworx.io/commons-wikimedia/mcp', { cps: 20 });
  await keyboardOff();
  const transport = d.locator('select').first();
  await point(transport, { move: 500 });
  await S('press'); await sleep(120);
  await transport.selectOption('http'); await S('release');
  await sleep(700);

  mark('connect');
  await S('caption', 1);
  await tap(d.getByRole('button', { name: /^Connect/ }).last(), { move: 700, dispatch: true });
  await d.getByText('Connection ready').first().waitFor({ timeout: 90000 });
  await S('hide');
  await d.getByText('commons_wikimedia_search', { exact: true }).first().scrollIntoViewIfNeeded().catch(() => {});
  await sleep(2000);
  await tap(d.getByRole('button', { name: 'Save server' }), { move: 700, dispatch: true });
  await app.locator('main').getByText('Connected', { exact: true }).first().waitFor({ timeout: 60000 });
  await S('hide');
  await sleep(500);
  await tap(app.getByRole('button', { name: 'Reload agent' }).first(), { move: 800 });
  await app.getByText('Reloaded MCP tools').first().waitFor({ timeout: 120000 });
  await S('hide');
  await sleep(1000);

  mark('ask');
  await S('caption', 2);
  await nav('/chat/otto', { click: false });
  await app.locator('textarea').last().waitFor({ timeout: 30000 });
  await sleep(600);
  await send('Find free-to-use photos of latte art for my café poster, with the credits I need.', { cps: PORTRAIT ? 36 : 26 });
  await app.getByText('Calling commons_wikimedia_search').first().waitFor({ timeout: 90000 });
  await app.getByText('Calling file_info').first().waitFor({ timeout: 90000 });
  await sleep(600);

  await app.getByText(/you can use on a poster|did not give me any photos/).first().waitFor({ timeout: 120000 });
  mark('answer');
  await S('caption', 3);
  await app.getByText(/write them out|try again/).first().waitFor({ timeout: 60000 });
  await sleep(500);
  await scrollTo(app.getByText(/you can use on a poster/).last(), PORTRAIT ? 90 : 120, 1100).catch(() => {});
  await sleep(PORTRAIT ? 4400 : 4800);

  mark('credits');
  await S('caption', 4);
  await send('Yes, write them out and save them.', { cps: PORTRAIT ? 34 : 24 });
  await app.getByText('Calling write_file').first().waitFor({ timeout: 60000 });
  await app.getByText('Open in Files').first().waitFor({ timeout: 60000 });
  await sleep(500);
  await scrollTo(app.getByText('Here are the credit lines').last(), PORTRAIT ? 90 : 120, 1100).catch(() => {});
  await sleep(PORTRAIT ? 4200 : 4600);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
