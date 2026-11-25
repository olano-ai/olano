// The browser video: an agent that can use the web. Config, Browser (off by default; Live chosen and
// saved during the take), then one request in chat. Wren's browser steps are the product's own tools
// (browser_navigate, browser_fill, browser_click) driving a real Chromium in Wren's own olano-browser
// container; the picture in the chat is the product's live view of it; the approval card is the one the
// browser's consequence guard raises for the form's submit button; taking control and handing back are
// the product's own. Team: Thistledown Pottery (teams/browser.py, which also explains its stand-ins:
// the made-up supplier's order form is a small page served on this machine, reached at the docker
// network's private address with the product's LIONCLAW_BROWSER_ALLOW_PRIVATE_NETWORK switch on).
// Needs docker and the olano-browser:latest image (docker build -t olano-browser:latest docker/olano-browser).
export const start = '/config';
export const ready = (app) => app.getByTestId('browser-mode').waitFor({ state: 'attached', timeout: 60000 });
export const brand = 'Agents that browse, while you watch.';
export const keyboardWords = ['"Order"', 'Order', 'Or'];
export const captions = [
  { eyebrow: 'Config', h1: 'Off until<br>you allow it', p: 'Three modes: Off, Headless, Live.' },
  { eyebrow: 'Ask', h1: 'Ask for<br>a web task', p: 'Each browser step shows in the chat.' },
  { eyebrow: 'Live view', h1: 'Watch it<br>work', p: 'A real browser, as the agent drives it.' },
  { eyebrow: 'Approval', h1: 'Submitting<br>needs you', p: 'A form submit always asks first.' },
  { eyebrow: 'Take control', h1: 'Step in<br>yourself', p: 'The agent waits while you click.' },
  { eyebrow: 'Browser tab', h1: 'Then hand<br>it back', p: 'The page, and who is in control.' },
];
export const voice = [
  'Olano agents can use a web browser. It is off until an administrator turns it on. Here, Live mode: a visible browser for each agent.',
  "Wren orders supplies for a pottery studio. Ask for clay from the supplier's order form, and each browser step shows in the chat.",
  "A window in the chat shows Wren's browser, live. Open it, and watch Wren fill in the form.",
  'Placing the order would submit the form, so the browser stops and asks. Read what it will send, then approve.',
  'You can also step in. Take control, and Wren waits while you click in its browser yourself.',
  "Wren's Browser tab shows the page that is open, and who is in control. Hand the browser back when you are done.",
  'Olano. Agents that browse, while you watch.',
];

// "Track this order" on the supplier's confirmation page, as fractions of the browser picture
const TRACK = [0.112, 0.395];

export async function run({ page, app, S, sleep, tap, point, send, scrollTo, nav, mark, PORTRAIT }) {
  // The chat brings an approval card into view with scrollIntoView, which also scrolls the frames
  // around the app: on the stage that would slide the phone's screen up over its hidden keyboard.
  // The stage's own boxes around the app do not scroll.
  await page.evaluate(() => {
    for (let e = document.getElementById('app').parentElement; e && e !== document.body; e = e.parentElement) {
      if (getComputedStyle(e).overflow !== 'visible') e.style.overflow = 'clip';
    }
  });
  // 0. Config, Browser: off by default; the three modes; Live, saved
  mark('config');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  const mode = app.getByTestId('browser-mode');
  await sleep(700);
  await scrollTo(app.getByText('Browser', { exact: true }).last(), PORTRAIT ? 100 : 110, 1300);
  await sleep(900);
  await scrollTo(mode, PORTRAIT ? 330 : 400, 900);
  await sleep(500);
  // A native dropdown opens outside the page, where a recording cannot see it: the take steps
  // through its three options instead, as the arrow keys do.
  await point(mode, { move: 700 });
  await sleep(800);
  await S('press'); await mode.selectOption('headless'); await S('release');
  await sleep(1300);
  await S('press'); await mode.selectOption('live'); await S('release');
  await sleep(1200);
  await tap(app.getByRole('button', { name: 'Save changes' }), { move: 800 });
  const effective = app.getByTestId('browser-effective');
  await effective.getByText('Live').first().waitFor({ timeout: 30000 });
  await S('hide');
  await scrollTo(effective, PORTRAIT ? 480 : 560, 900).catch(() => {});
  await sleep(1100);

  // 1. a request in chat; each browser step is listed
  mark('ask');
  await S('caption', 1);
  await nav('/chat/wren', { click: false });
  await app.locator('textarea').last().waitFor({ timeout: 30000 });
  await sleep(900);
  await send('Order eight bags of buff stoneware from Northgate.', { cps: 24 });
  await app.getByText('Calling browser_navigate').first().waitFor({ timeout: 90000 });
  const dock = app.getByTestId('browser-dock');
  await dock.waitFor({ timeout: 120000 });
  if (!PORTRAIT) await dock.locator('img').first().waitFor({ timeout: 60000 });
  await sleep(1500);

  // 2. the live view: the same browser, large, while Wren fills in the form
  mark('watch');
  await S('caption', 2);
  await tap(app.getByTestId('dock-expand'), { move: 800 });
  const big = app.getByTestId('browser-dock-expanded');
  await big.locator('img').first().waitFor({ timeout: 60000 });
  await S('hide');
  await app.getByText('Calling browser_fill').first().waitFor({ state: 'attached', timeout: 120000 });
  await sleep(4200);

  // 3. the submit button: the guard stops the click and asks (the large view closes itself)
  const accept = app.getByRole('button', { name: 'Accept once' });
  await accept.waitFor({ timeout: 120000 });
  mark('approve');
  await S('caption', 3);
  await sleep(700);
  const card = app.locator('.approval-card').first();
  await scrollTo(card, PORTRAIT ? 250 : 300, 800).catch(() => {});
  if (!PORTRAIT) await point(card.getByText('The form is sent to').first(), { move: 900 }).catch(() => {});
  await sleep(3600);
  await tap(accept, { move: 800 });
  await app.getByText('Northgate confirmed').first().waitFor({ timeout: 120000 });
  await S('hide');
  await sleep(2400);

  // 4. take control: the person clicks in the agent's browser
  mark('control');
  await S('caption', 4);
  await tap(app.getByTestId('dock-expand'), { move: 800 });
  const picture = big.locator('img').first();
  await picture.waitFor({ timeout: 60000 });
  await sleep(1000);
  await tap(app.getByTestId('dock-take-control'), { move: 800 });
  await big.getByText('You have control').first().waitFor({ timeout: 30000 });
  await sleep(1300);
  // The picture keeps the page's shape inside its element (bars at the sides): find the page itself
  const box = await picture.boundingBox();
  const ratio = await picture.evaluate((img) => img.naturalWidth / img.naturalHeight);
  const w = Math.min(box.width, box.height * ratio), h = w / ratio;
  const at = [box.x + (box.width - w) / 2 + w * TRACK[0], box.y + (box.height - h) / 2 + h * TRACK[1]];
  await tap(picture, { move: 900, dispatch: false, at });
  await big.getByText('Order NG-2041 status').first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(3000);

  // 5. the agent's Browser tab: the page, who is in control, hand back
  mark('tab');
  await S('caption', 5);
  if (PORTRAIT) await nav('/agents/wren/browser');
  else {
    await nav('/agents/wren', { click: false });
    const tab = app.locator('main').getByText('Browser', { exact: true }).first();
    await tab.waitFor({ timeout: 30000 });
    await sleep(900);
    await tap(tab, { move: 800 });
  }
  const panel = app.getByTestId('browser-tab');
  await panel.getByText('You (the agent is waiting)').waitFor({ timeout: 30000 });
  await sleep(500);
  await scrollTo(panel, PORTRAIT ? 130 : 200, 900).catch(() => {});
  if (!PORTRAIT) await point(panel.getByText('You (the agent is waiting)'), { move: 800, dx: -270 });
  await sleep(2400);
  await tap(panel.getByRole('button', { name: 'Hand back to the agent' }), { move: 800 });
  await panel.getByText('The agent', { exact: true }).waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(PORTRAIT ? 3200 : 2400);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
