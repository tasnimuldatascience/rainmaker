/**
 * Capture console screenshots for the README.
 *
 * Scripted rather than hand-captured so the images can be regenerated after a UI change
 * instead of silently going stale. Both themes, and the offline state — which is the whole
 * point of the product and therefore the shot that has to be in the README.
 *
 *   node scripts/screenshots.mjs [--base http://127.0.0.1:5174]
 */

import { chromium } from "playwright";
import { mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const OUT = resolve(ROOT, "docs/img");
const BASE = process.argv.includes("--base")
  ? process.argv[process.argv.indexOf("--base") + 1]
  : "http://127.0.0.1:5174";

mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const ctx = await browser.newContext({
  viewport: { width: 1500, height: 980 },
  deviceScaleFactor: 2,
});
const page = await ctx.newPage();

async function shot(name, prepare, settleMs = 600) {
  await page.goto(BASE, { waitUntil: "networkidle" });
  // The seed writes through the op path, so give the replica a beat to hydrate and render.
  await page.waitForSelector(".deal", { timeout: 15000 }).catch(() => {});
  await prepare?.(page);
  // `settleMs` is per-shot on purpose. The call shot waits for a specific ANIMATION FRAME
  // (speaking, open viseme, not blinking) and a fixed settle afterwards throws that frame
  // away -- the mouth has moved on by the time the shutter fires.
  if (settleMs > 0) await page.waitForTimeout(settleMs);
  await page.screenshot({ path: resolve(OUT, `${name}.png`) });
  console.log(`wrote docs/img/${name}.png`);
}

const setTheme = (t) => page.evaluate((v) => (document.documentElement.dataset.theme = v), t);

await shot("pipeline-dark", async () => setTheme("dark"));
await shot("pipeline-light", async () => setTheme("light"));

await shot("deal-drawer", async (p) => {
  await setTheme("dark");
  await p.click(".deal");
  await p.waitForSelector(".drawer");
});

await shot("research", async (p) => {
  await setTheme("dark");
  await p.click('.nav button:has-text("Research")');
  await p.fill("#root input.input", "acme.dev");
});

await shot("call", async (p) => {
  await setTheme("dark");
  await p.click('.nav button:has-text("Live call")');
  await p.click('button:has-text("Start call")');
  // Let the transcript and the latency strip fill in.
  await p.waitForTimeout(9500);
  // Then wait for a GOOD FRAME rather than sleeping and hoping: she must be speaking, on an
  // open viseme, and not mid-blink. Sleeping caught her blinking on a closed mouth twice,
  // which made a working rig look static in the one image people actually look at.
  await p
    .waitForFunction(
      () => {
        const el = document.querySelector("svg[data-speaking]");
        return (
          el?.getAttribute("data-speaking") === "true" &&
          el.getAttribute("data-blink") === "false" &&
          Number(el.getAttribute("data-mouth-open") ?? 0) > 12
        );
      },
      null,
      { timeout: 8000, polling: 30 },
    )
    .catch(() => console.warn("no open-mouth frame captured within the window"));
}, 0);

// The offline shot. This is the product's actual claim, so it is captured against a genuinely
// severed connection rather than mocked: route abort kills the socket and every fetch, then
// edits are made and shown persisting with a pending count.
await shot("offline", async (p) => {
  await setTheme("dark");
  // Wait for the service worker to control the page BEFORE severing the connection --
  // otherwise the reload has no cached shell to boot from and the browser shows its own
  // error page, which is exactly the failure this shot exists to prove does not happen.
  await p.waitForFunction(() => navigator.serviceWorker?.controller != null, null, {
    timeout: 20000,
  }).catch(() => console.warn("service worker did not take control in time"));
  await ctx.setOffline(true);
  await p.reload({ waitUntil: "domcontentloaded" });
  await p.waitForSelector(".deal", { timeout: 15000 }).catch(() => {});
  const card = await p.$(".deal");
  if (card) {
    await card.click();
    await p.waitForSelector(".drawer");
    const notes = await p.$("#notes");
    await notes?.fill(
      "Edited with the network off. This is saved on this device and will sync on its own.",
    );
    await p.waitForTimeout(500);
    await p.keyboard.press("Escape");
  }
  await p.waitForTimeout(800);
});
await ctx.setOffline(false);

await browser.close();
console.log("done");
