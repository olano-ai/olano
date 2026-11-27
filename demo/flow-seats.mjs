// The seats video: a dashboard shared by a team where each person owns an agent. The admin invites
// someone and gives her an agent of her own; the org chart; she signs in through her invite link and
// sees only her agent; her agent asks a colleague's agent on her behalf; the colleague signs in and
// finds his agent's side of it. Team: Brightmoor Architects (teams/seats.py). Every sign-in is the
// product's own auth: the take starts signed in as the admin, then the browser really becomes Ines
// (invite link) and then Tom (sign-in form).
export const start = '/admin';
export const ready = (app) => app.getByText('Invite a teammate').first().waitFor();
export const brand = 'Each person, their own agent.';
export const keyboardWords = ['"Cost"', 'Cost', 'Costs'];
export const captions = [
  { eyebrow: 'Invite', h1: 'Add a<br>person', p: 'She gets a link to sign in.' },
  { eyebrow: 'Seats', h1: 'Her own<br>agent', p: 'Ines owns cobb. Tom owns tilde.' },
  { eyebrow: 'Org chart', h1: 'People and<br>their agents', p: 'Who owns which agent.' },
  { eyebrow: 'Member view', h1: 'Only what<br>is hers', p: 'Her agent. No admin pages.' },
  { eyebrow: 'On her behalf', h1: 'Agent asks<br>agent', p: "cobb asks Tom's agent, then answers her." },
  { eyebrow: "Tom's view", h1: 'What his<br>agent shared', p: 'Only exchanges with tilde.' },
];
export const voice = [
  'Nadia runs Brightmoor Architects. From the Admin page she invites Ines, and gets a link to send her.',
  'Then she gives Ines a seat. Ines owns cobb, the cost planning agent.',
  'The org chart shows each person and the agent they own.',
  'Ines opens her link and sets a password. She sees her own agent, and no admin pages.',
  "Ines asks cobb for a costing. Cobb asks Tom's agent for the floor area, and brings the answer back to her.",
  "Tom signs in to his own view. He sees what tilde shared with Ines's agent, and only that.",
  'Olano. Each person, their own agent.',
];

const ADMIN = { email: 'nadia@brightmoor.example', password: 'brightmoor-nadia-1' };

/** The take starts signed in as the admin, through the product's own sign-in endpoint. */
export async function init(ctx, { server }) {
  const r = await ctx.request.post(server.base_url + '/api/auth/login', { data: ADMIN });
  if (!r.ok()) throw new Error('admin sign-in failed: ' + r.status());
}

export async function run(h) {
  const { app, S, sleep, tap, point, type, send, keyboardOff, scrollTo, nav, openApp, mark, PORTRAIT } = h;
  const drawer = async () => {      // a phone keeps the sidebar in a drawer
    await tap(app.locator('button[aria-label="Open navigation"]'), { move: 600 });
    await sleep(700);
  };
  const side = () => app.locator('aside.sidebar');

  mark('invite');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(1200);
  const form = app.getByText('Invite a teammate').first();
  await scrollTo(form, PORTRAIT ? 70 : 90, 1200);
  await sleep(300);
  await type(app.getByPlaceholder('Name', { exact: true }), 'Ines Duarte', { cps: 18 });
  await type(app.getByPlaceholder('Email', { exact: true }), 'ines@brightmoor.example', { cps: 24 });
  await keyboardOff();
  await tap(app.getByRole('button', { name: 'Send invite' }), { move: 600 });
  const link = app.locator('.modal-overlay input').first();
  await link.waitFor({ timeout: 30000 });
  await S('hide');
  const invitePath = new URL(await link.inputValue()).pathname;
  await sleep(2600);
  await tap(app.getByRole('button', { name: 'Done' }), { move: 600 });

  mark('seat');
  await S('caption', 1);
  // The Agent access table loaded with the page, before Ines existed, so the admin reloads it.
  await h.frame.evaluate(() => { window.__seatsOld = true; location.reload(); });
  for (let i = 0; i < 400; i++) {      // until the old document has gone
    if (!(await h.frame.evaluate(() => !!window.__seatsOld).catch(() => true))) break;
    await sleep(30);
  }
  const access = app.getByText('Agent access', { exact: true }).first();
  await access.waitFor({ timeout: 30000 });
  await scrollTo(access, PORTRAIT ? 60 : 80, 1200);
  await sleep(500);
  await tap(app.locator('tr', { hasText: 'cobb' }).getByText('Set an owner'), { move: 700 });
  const dlg = app.locator('[role=dialog]');
  const ines = dlg.locator('.picker-item', { hasText: 'Ines Duarte' });
  await ines.waitFor({ timeout: 30000 });
  await sleep(700);
  await tap(ines, { move: 600 });
  await sleep(500);
  await tap(dlg.locator('button.btn.primary'), { move: 600 });
  await app.getByText('Access updated.').first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(2600);

  mark('org');
  await S('caption', 2);
  await nav('/orgchart');
  await app.getByText('Ines Duarte').first().waitFor({ timeout: 30000 });
  await sleep(1400);
  if (!PORTRAIT) {
    await point(app.locator('.org-node.human', { hasText: 'Ines Duarte' }), { move: 800 });
    await sleep(900);
    await point(app.locator('.org-node', { hasText: 'Cost Planning' }), { move: 800 });
  }
  if (PORTRAIT) {       // a phone draws the whole chart small; bring it closer
    await sleep(800);
    for (let i = 0; i < 2; i++) { await tap(app.getByRole('button', { name: 'Zoom in' }), { move: 500 }); await sleep(350); }
  }
  await sleep(PORTRAIT ? 1600 : 1800);

  mark('ines');
  await S('caption', 3);
  await openApp(invitePath, (a) => a.locator('input[name=new-password]').waitFor({ timeout: 30000 }));
  await sleep(900);
  await type(app.locator('input[name=new-password]'), 'brightmoor-ines-1', { cps: 26 });
  await type(app.locator('input[name=confirm-password]'), 'brightmoor-ines-1', { cps: 26 });
  await keyboardOff();
  await tap(app.getByRole('button', { name: /Set password/ }), { move: 600 });
  await app.getByText('Overview').first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(1200);
  if (PORTRAIT) await drawer();
  await tap(app.locator('.profile-btn'), { move: 800 });
  await sleep(2200);
  await tap(app.locator('.profile-btn'), { move: 300 });
  await sleep(400);

  mark('ask');
  await S('caption', 4);
  if (PORTRAIT) await nav('/chat/cobb');
  else await tap(side().getByText('cobb', { exact: true }), { move: 700 });
  await app.locator('textarea').last().waitFor();
  await sleep(600);
  await send('Cost the Fenwick extension from plan revision C.', { cps: 26 });
  await app.getByText('Calling talk_to_agent').first().waitFor({ timeout: 90000 });
  await app.getByText('The estimate is in').first().waitFor({ timeout: 120000 });
  await sleep(600);
  await scrollTo(app.getByText("I asked Tom's agent").first(), PORTRAIT ? 90 : 110, 1200).catch(() => {});
  await sleep(2800);

  mark('tom');
  await S('caption', 5);
  if (PORTRAIT) await drawer();
  await tap(app.locator('.profile-btn'), { move: 800 });
  await sleep(500);
  await tap(app.getByText('Sign out', { exact: true }), { move: 500 });
  await sleep(600);
  // Tom opens the dashboard on his own computer.
  await openApp('/', (a) => a.locator('input[type=email]').waitFor({ timeout: 30000 }));
  await sleep(500);
  await type(app.locator('input[type=email]'), 'tom@brightmoor.example', { cps: 26 });
  await type(app.locator('input[type=password]'), 'brightmoor-tom-1', { cps: 26 });
  await keyboardOff();
  await tap(app.getByRole('button', { name: 'Sign in', exact: true }), { move: 500 });
  await app.getByText('Overview').first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(1400);
  await nav('/activity', { click: false });
  if (PORTRAIT) {       // a phone shows the list of conversations first
    const row = app.locator('[data-testid="a2a-rail"]').getByText('tilde').first();
    await row.waitFor({ timeout: 30000 });
    await sleep(1200);
    await tap(row, { move: 700 });
  }
  const q = app.locator(':text("What is the floor area of the Fenwick extension"):visible').first();
  await q.waitFor({ timeout: 30000 });
  await sleep(700);
  if (!PORTRAIT) await point(q, { move: 900 });
  await sleep(3600);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
