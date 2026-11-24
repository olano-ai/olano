// Renders a chart recipe to a PNG with the product's own `charts` skill (the script an agent runs)
// and Chromium, the way the render_html_image tool does.
//   node chart.mjs assets/chat-sales.json assets/chat-sales.png [WxH] [theme]
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { chromium, REPO, WORK } from './common.mjs';
const [spec, out, size = '1200x760', theme = 'midnight'] = process.argv.slice(2);
const html = path.join(WORK, path.basename(out).replace(/\.png$/, '.html'));
execFileSync('python3', [path.join(REPO, 'olano/skills/built_in/charts/scripts/chart.py'), 'render', spec, '--theme', theme, '--size', size, '--out', html], { stdio: 'inherit' });
const [w, h] = size.split('x').map(Number);
const b = await chromium.launch();
const page = await b.newPage({ viewport: { width: w, height: h }, deviceScaleFactor: 2 });
await page.goto('file://' + html);
await page.locator('.render-done').waitFor({ state: 'attached' });
await page.screenshot({ path: out });
await b.close();
console.log(out);
