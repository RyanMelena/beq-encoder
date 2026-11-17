// Usage: PLAYWRIGHT=<path to a playwright package> node journeys.mjs <base url> <out dir> [phone|desktop]
// Walks the journeys in docs/ux-hill-climb.md on the seeded app (serve.py), screenshots every
// screen in order, and counts the clicks of the two measured paths. A journey that changes the
// data needs a fresh seed, so boot serve.py again before each viewport.
import { createRequire } from 'node:module';
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT || 'playwright');
const [base, out, only] = process.argv.slice(2);
const VIEWPORTS = { desktop: { width: 1440, height: 900 }, phone: { width: 390, height: 844 } };

const browser = await chromium.launch();
const report = { clicks: {}, screens: [] };
for (const [vp, size] of Object.entries(VIEWPORTS).filter(([n]) => !only || n === only)) {
  const page = await (await browser.newContext({ viewport: size })).newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  let clicks = 0;
  let n = 0;
  let journey = '';
  const shot = async (name) => {
    const dir = join(out, vp, journey);
    mkdirSync(dir, { recursive: true });
    const file = join(dir, `${String(++n).padStart(2, '0')}-${name}.png`);
    await page.waitForLoadState('networkidle');
    await page.screenshot({ path: file, fullPage: true });
    report.screens.push({ viewport: vp, journey, step: n, name, url: page.url(), file });
  };
  const click = async (locator) => {
    clicks += 1;
    await Promise.all([page.waitForLoadState('load'), locator.first().click()]);
    await page.waitForLoadState('networkidle');
  };
  const start = async (name, url) => {
    journey = name; n = 0; clicks = 0;
    await page.goto(base + url);
    await shot('start');
  };

  await start('1-see-filters', '/?clear=1');
  await click(page.locator('td.film a[href="/films/1"]'));
  await shot('film-detail');
  report.clicks[`${vp}: Library to a film's filters`] = clicks;

  await start('2-add-with-defaults', '/films/2');
  await click(page.getByRole('link', { name: 'Add BEQ track', exact: true }));
  await shot('wizard-filter');
  await click(page.getByRole('button', { name: 'Continue to options' }));
  await shot('wizard-options');
  await click(page.getByRole('button', { name: 'Add BEQ track' }));
  await shot('film-with-activity');
  report.clicks[`${vp}: film detail page to Add BEQ track with the defaults`] = clicks;

  await start('3-design-then-add', '/films/3');
  await click(page.getByRole('link', { name: 'Add BEQ track', exact: true }));
  await shot('wizard-no-filter');
  await click(page.getByRole('button', { name: 'Design a filter' }));
  await shot('wizard-designing');
  await page.goto(`${base}/films/4`);
  await shot('film-with-forge-filter');
  await click(page.getByRole('link', { name: 'Add BEQ track', exact: true }));
  await shot('wizard-forge-filter');
  await click(page.getByRole('button', { name: 'Continue to options' }));
  await click(page.getByRole('button', { name: 'Add BEQ track' }));
  await shot('film-with-activity');

  await start('4-clip-then-add', '/films/1');
  await click(page.getByRole('link', { name: 'Make a BEQ clip' }));
  await shot('wizard-clip-filter');
  await click(page.getByRole('button', { name: 'Continue to options' }));
  await shot('wizard-clip-options');
  await click(page.getByRole('button', { name: 'Make BEQ clip' }));
  await shot('film-clip-queued');
  await click(page.locator('.clip-adds a.btn'));
  await shot('wizard-options-from-clip');
  await click(page.getByRole('button', { name: 'Add BEQ track' }));
  await shot('film-with-activity');

  await start('5-recover-from-failed-encode', '/films/7');
  await click(page.getByRole('button', { name: 'Try again' }));
  await shot('film-retrying');
  await page.goto(`${base}/activity`);
  await shot('activity');

  if (errors.length) report[`${vp} page errors`] = errors;
}
await browser.close();
writeFileSync(join(out, 'journeys.json'), JSON.stringify(report, null, 2));
console.log(JSON.stringify(report.clicks, null, 2));
if (Object.keys(report).some(k => k.endsWith('page errors'))) {
  console.error(JSON.stringify(report, null, 2));
  process.exit(1);
}
