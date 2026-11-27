// The themes video: the dashboard's thirteen themes on screens with something on them. A chat with a
// chart, the account menu's Theme dropdown, the theme button walking through every theme, the
// Overview, the org chart (the Agents page on a phone, where the chart is too small to read), and the
// theme list in Config, which says the choice is kept by this browser.
// Team: Nightjar Records, an independent record shop (teams/themes.py).
//
// The Theme dropdown is a native <select>, whose popup a recording of the page cannot show: the
// take points at it, presses, and picks the value on the same element.
const PORTRAIT_TAKE = (process.env.LAYOUT || 'landscape') === 'portrait';

export const start = '/chat/lyra';
export const ready = (app) => app.locator('textarea').last().waitFor();
export const brand = 'Make it look like yours.';
export const keyboardWords = ['"Which"', 'Which', 'What'];
export const captions = [
  { eyebrow: 'Themes', h1: 'One dashboard,<br>thirteen looks', p: 'Here in Olano Dark, the default.' },
  { eyebrow: 'Pick', h1: 'From the<br>account menu', p: 'Theme, then the one you want.' },
  { eyebrow: 'All of them', h1: 'Dark ones,<br>pale ones', p: 'The theme button steps through each.' },
  // A phone picks from a drawer that covers the page, so it shows one theme per page, not two.
  PORTRAIT_TAKE
    ? { eyebrow: 'Overview', h1: 'Every page<br>follows', p: 'The Overview, in Midnight.' }
    : { eyebrow: 'Overview', h1: 'Every page<br>follows', p: 'Midnight, then Sepia.' },
  PORTRAIT_TAKE
    ? { eyebrow: 'Agents', h1: 'The whole<br>team, too', p: 'The Agents page, in Sakura.' }
    : { eyebrow: 'Org chart', h1: 'The whole<br>team, too', p: 'Volt, then Sakura.' },
  { eyebrow: 'Per browser', h1: 'Kept in<br>this browser', p: 'Not in config.yaml. Each browser has its own.' },
];
export const voice = [
  "Olano's dashboard comes in thirteen themes. Here is Lyra, the sales agent, answering with a chart.",
  'Pick a theme from the account menu. It changes straight away.',
  'Or press the theme button and step through them all: dark ones, pale ones, black and white, and high contrast.',
  'Every page follows the theme you pick. Here, the Overview.',
  'And the whole team, too.',
  'Config lists them too. The choice is kept in this browser, not on the server.',
  'Olano. Make it look like yours.',
];

export async function run({ app, S, sleep, tap, point, send, nav, scrollTo, mark, P, PORTRAIT }) {
  const hamburger = app.locator('button.mobile-nav-btn');
  const drawer = async (open) => {
    if (open) { await tap(hamburger, { move: 700 }); await sleep(700); }
    else { await tap(app.locator('.mobile-nav-backdrop'), { move: 500, at: P(0.92, 0.55) }); await sleep(500); }
  };
  /** The account menu's Theme dropdown: open the menu, pick, close it again. */
  const pickTheme = async (value, { pause = 500 } = {}) => {
    if (PORTRAIT) await drawer(true);
    const profile = app.locator('.profile-btn').first();
    await tap(profile, { move: 700 });
    await sleep(600);
    const select = app.locator('select[aria-label="Theme"]').first();
    await point(select, { move: 600 });
    await sleep(150);
    await S('press');
    await sleep(140);
    await select.selectOption(value);
    await S('release');
    await sleep(pause);
    await tap(profile, { move: 500 });     // the same button closes the menu
    await sleep(300);
    if (PORTRAIT) await drawer(false);
  };

  // 0. A chat with a chart, in the default theme
  mark('chat');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(600);
  await send('Which genres sold best in November?', { cps: 24 });
  await app.locator('img[src*="november-genres"]').first().waitFor({ timeout: 90000 });
  await sleep(2600);

  // 1. The account menu's Theme dropdown
  mark('pick');
  await S('caption', 1);
  await pickTheme('light', { pause: 1600 });
  await sleep(900);

  // 2. The theme button, through the other eleven
  mark('cycle');
  await S('caption', 2);
  if (PORTRAIT) await drawer(true);
  const toggle = app.locator('button.theme-toggle').first();
  await point(toggle, { move: 700 });
  await sleep(300);
  for (let i = 0; i < 11; i++) {
    await tap(toggle, { move: 60, hold: 90 });
    await sleep(950);
  }
  if (PORTRAIT) await drawer(false);
  await sleep(1400);

  // 3. The Overview, in two themes
  mark('overview');
  await S('caption', 3);
  await nav('/overview');
  await app.getByText('Scheduled tasks').first().waitFor();
  if (PORTRAIT) {
    await pickTheme('midnight');
    await sleep(2600);
  } else {
    await pickTheme('midnight');
    await sleep(1200);
    await pickTheme('sepia');
    await sleep(1200);
  }

  // 4. The org chart (the Agents page on a phone), in two more
  mark('team');
  await S('caption', 4);
  if (PORTRAIT) {
    await nav('/agents');
    await app.getByText('Shop Manager').first().waitFor();
  } else {
    await nav('/orgchart');
    await app.getByText('Shop Manager').first().waitFor();
  }
  await sleep(500);
  if (PORTRAIT) {
    await pickTheme('sakura');
    await sleep(2600);
  } else {
    await pickTheme('volt');
    await sleep(1200);
    await pickTheme('sakura');
    await sleep(1200);
  }

  // 5. Config's theme list, and what it says about where the choice lives
  mark('config');
  await nav('/config');
  const list = app.locator('.menu-item', { hasText: 'Harbor' }).first();
  await list.waitFor();
  await S('caption', 5);
  await sleep(400);
  await scrollTo(app.getByText('Appearance for this browser').first(), PORTRAIT ? 90 : 120, 900);
  await sleep(400);
  await tap(list, { move: 800 });
  await sleep(900);
  await point(app.getByText('Appearance for this browser').first(), { move: 800 });
  await sleep(2600);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
