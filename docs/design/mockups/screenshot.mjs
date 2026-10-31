// Screenshots of the rendered mock-ups (html/*.html) into png/, light and dark, at each board's
// width from src/canvas.json. Run build.mjs first. Needs Playwright with Chromium installed
// (resolved through NODE_PATH, e.g. a global install): `node docs/design/mockups/screenshot.mjs`.
// The pages load IBM Plex from Google Fonts, so this step needs network access; the app itself
// will self-host the fonts.

import { mkdirSync, readFileSync, readdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const { chromium } = createRequire(import.meta.url)('playwright');
const canvas = JSON.parse(readFileSync(join(here, 'src', 'canvas.json'), 'utf8'));
const out = join(here, 'png');
mkdirSync(out, { recursive: true });

// The rail is a component; every page screenshot shows it in place.
const skip = new Set(['Rail']);

// Every page in html/, variants (<Board>.<variant>.html) at their board's size.
const names = readdirSync(join(here, 'html')).filter((f) => f.endsWith('.html')).map((f) => f.slice(0, -5)).sort();
const browser = await chromium.launch();
for (const name of names) {
  const boardName = name.split('.')[0];
  if (skip.has(boardName)) continue;
  const board = canvas.boards[`${boardName}.dc.html`];
  for (const theme of ['light', 'dark']) {
    const page = await browser.newPage({ viewport: { width: board.w, height: board.h } });
    const url = pathToFileURL(join(here, 'html', `${name}.html`)).href + `?theme=${theme}`;
    await page.goto(url, { waitUntil: 'networkidle' });
    await page.evaluate(() => document.fonts.ready);
    const suffix = theme === 'light' ? '' : '.dark';
    await page.screenshot({ path: join(out, `${name}${suffix}.png`), fullPage: true });
    await page.close();
  }
}
await browser.close();
console.log(`screenshots in ${out}`);
