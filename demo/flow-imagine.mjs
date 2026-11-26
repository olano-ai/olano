// The imagine video: Olano's built-in image tools. A logo and a menu photo made with image_generate,
// then the photo changed with image_edit, each drawn inline in the reply, and the last one opened in
// Files. The pictures and the tool results come from running those two product tools once, for real,
// ahead of the take (teams/imagine.py says how). Team: Little Ember Bakehouse, a neighbourhood bakery.
export const start = '/chat/wren';
export const ready = (app) => app.locator('textarea').last().waitFor();
export const timezone = 'Asia/Singapore';
export const brand = 'Pictures on request.';
export const keyboardWords = ['"Make"', 'Make', 'Now', 'Ma'];
export const captions = [
  { eyebrow: 'Images', h1: 'A logo from<br>one sentence', p: 'Olano\'s built-in image tool.' },
  { eyebrow: 'Logo', h1: 'Right there<br>in the chat', p: 'Drawn, saved, shown in the reply.' },
  { eyebrow: 'Menu', h1: 'A photo for<br>the menu', p: 'Cardamom buns, on a wooden board.' },
  { eyebrow: 'Tweak', h1: 'Warmer,<br>with steam', p: 'One more line to change it.' },
  { eyebrow: 'Edit', h1: 'Same photo,<br>fresh from the oven', p: 'image_edit changed the picture.' },
  { eyebrow: 'Files', h1: 'Saved in<br>its files', p: 'Full size, ready to download.' },
];
export const voice = [
  'Nadia runs Little Ember Bakehouse. She asks Wren, her design agent, for a logo.',
  "Wren uses Olano's built-in image tool, and the logo appears right in the chat.",
  'Next, a photo of the cardamom buns for the menu. Wren makes that too.',
  'Then one small change: make it warmer, with a little steam.',
  'Wren edits the same photo. Now the buns look fresh from the oven.',
  "Every picture is saved in Wren's files, at full size.",
  'Olano. Pictures on request.',
];

export async function run({ app, S, sleep, tap, send, scrollTo, mark, PORTRAIT }) {
  const cps = PORTRAIT ? 34 : 24;
  const lastImg = () => app.locator('main img').last();

  mark('ask');
  await S('appIn');
  await sleep(300);
  await S('caption', 0);
  await sleep(1600);
  await send('Make a logo for my bakery, Little Ember Bakehouse. Warm and hand-drawn, with a small flame.', { cps });
  await app.getByText('Calling image_generate').first().waitFor({ timeout: 60000 });

  await app.getByText('Open in Files').first().waitFor({ timeout: 90000 });
  mark('logo');
  await S('caption', 1);
  await sleep(500);
  await scrollTo(lastImg(), PORTRAIT ? 120 : 110, 1300).catch(() => {});
  await sleep(PORTRAIT ? 3600 : 4200);

  mark('photo');
  await S('caption', 2);
  await send('Now a menu photo of our cardamom buns.', { cps });
  await app.getByText('Calling image_generate').nth(1).waitFor({ timeout: 60000 });
  await app.getByText('Open in Files').nth(1).waitFor({ timeout: 90000 });
  await sleep(500);
  await scrollTo(lastImg(), PORTRAIT ? 120 : 110, 1300).catch(() => {});
  await sleep(PORTRAIT ? 2600 : 3000);

  mark('tweak');
  await S('caption', 3);
  await send('Make it warmer, with a little steam.', { cps });
  await app.getByText('Calling image_edit').first().waitFor({ timeout: 60000 });
  await sleep(600);

  await app.getByText('Open in Files').nth(2).waitFor({ timeout: 90000 });
  mark('edit');
  await S('caption', 4);
  await sleep(400);
  await scrollTo(lastImg(), PORTRAIT ? 120 : 110, 1300).catch(() => {});
  await sleep(PORTRAIT ? 3800 : 4400);

  mark('files');
  await S('caption', 5);
  const open = app.getByText('Open in Files').last();
  await scrollTo(open, PORTRAIT ? 560 : 560, 800).catch(() => {});
  await sleep(300);
  await tap(open, { move: 800 });
  await app.locator('main img').first().waitFor({ timeout: 30000 });
  await S('hide');
  await sleep(3600);

  mark('end');
  await S('caption', null);
  await S('end');
  await sleep(3200);
}
