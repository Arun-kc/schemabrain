// One-shot Playwright screenshotter. Used as the visual-feedback
// loop while iterating on dashboard surfaces. Not part of the
// shipped test suite — keep it deliberately tiny.
//
// Usage:
//   node scripts/screenshot.mjs <url> <out> [width] [theme] [clickRow]
//
// clickRow (optional): "tr:has(td:has-text('order'))" or similar
// Playwright locator string. If set, clicks before screenshotting.

import { chromium } from "@playwright/test";

const [, , url, out, widthArg = "1440", theme = "light", clickRow = ""] = process.argv;
if (!url || !out) {
  console.error(
    "usage: node screenshot.mjs <url> <out> [width] [theme] [clickRow]",
  );
  process.exit(1);
}

const width = Number.parseInt(widthArg, 10);
const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width, height: 1000 },
  deviceScaleFactor: 2,
});
const page = await context.newPage();

await context.addInitScript((t) => {
  try {
    localStorage.setItem("schemabrain.theme", t);
  } catch {}
}, theme);

await page.goto(url, { waitUntil: "networkidle", timeout: 15_000 });
await page.waitForLoadState("networkidle");
await page.waitForTimeout(800);

if (clickRow) {
  if (clickRow.startsWith("hover:")) {
    await page.locator(clickRow.substring(6)).first().hover();
  } else {
    await page.locator(clickRow).first().click();
  }
  await page.waitForTimeout(800);
}

await page.screenshot({ path: out, fullPage: true });
await browser.close();
console.log(`wrote ${out} (${width}px, ${theme}${clickRow ? `, clicked: ${clickRow}` : ""})`);
