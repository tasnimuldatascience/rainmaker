/**
 * Capture console screenshots for the README.
 *
 * Scripted rather than hand-captured so the images can be regenerated after a UI change
 * instead of silently going stale. Both themes, and the disconnected state — which is a
 * claim nobody believes without a picture.
 *
 *   node scripts/screenshots.mjs [--base http://127.0.0.1:5174] [--only call]
 *
 * RAISE THE CALL RATE LIMIT FIRST. Admission counts calls per VISITOR, a visitor is an IP, and
 * every run comes from 127.0.0.1 — so the default of six an hour is spent by the fifth shot and
 * the rest silently sit in the lobby with no error on the page. Start the API with:
 *
 *   RAINMAKER_CALLS_PER_VISITOR_HOUR=200
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

// Which shots to take. Empty means all of them; `--only mouth` is one, named the same as the
// file it writes, so debugging one picture does not cost a run of every other.
const ONLY = process.argv.includes("--only")
  ? process.argv[process.argv.indexOf("--only") + 1]
  : "";
const wanted = (name) => !ONLY || ONLY === name;

mkdirSync(OUT, { recursive: true });

// Audio must actually play for the call shot: the mouth is driven by clips coming out of the
// Web Audio graph, so a suspended AudioContext would produce a screenshot of a static face and
// no indication that anything was wrong.
const browser = await chromium.launch({
  args: ["--autoplay-policy=no-user-gesture-required", "--use-fake-ui-for-media-stream"],
});
const ctx = await browser.newContext({
  viewport: { width: 1500, height: 980 },
  deviceScaleFactor: 2,
});
const page = await ctx.newPage();

async function shot(name, prepare, settleMs = 600) {
  if (!wanted(name)) return;
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

/**
 * Fill the front door and start a call.
 *
 * THREE FIELDS NOW, and `fill(".intake input")` would match all of them — Playwright's strict
 * mode turns that into an error rather than a wrong screenshot, which is the good outcome, but
 * it still has to be written once rather than in five shots.
 */
async function startCall(p) {
  await p.click('.nav button:has-text("Live call")');
  await p.fill('.intake input[autocomplete="name"]', "Dana Whitfield");
  await p.fill('.intake input[type="email"]', "dana.whitfield@stripe.com");
  await p.fill('.intake input[autocomplete="organization"]', "Stripe");
  await p.click('button:has-text("Start the call")');
}

/**
 * Wait until she has said three things: the disclosure, the holding line, and the greeting.
 *
 * The conversation rail carries the turns, so they are in the DOM without opening anything.
 * Waiting on the caption instead would race the clause stream.
 */
const greeted = async (p) => {
  // The rail carries the conversation, so the turns are in the DOM without opening anything.
  await p.waitForFunction(() => document.querySelectorAll(".turn").length >= 3, null, {
    timeout: 120000,
  });
};

const say = async (p, text) => {
  await p.fill(".meet-input input", text);
  await p.click(".meet-send");
};

await shot("pipeline-dark", async () => setTheme("dark"));
await shot("pipeline-light", async () => setTheme("light"));

await shot("deal-drawer", async (p) => {
  await setTheme("dark");
  // THE DEAL A CALL ACTUALLY LANDED ON, not whichever card sorts first. The README caption
  // beside this image promises "the outcome, the transcript, and the notes, all written by the
  // call" -- and `.deal` opened Helios Robotics, a seeded deal with an empty notes field and no
  // call attached. The picture quietly said the opposite of the sentence under it.
  await p.click('.deal:has-text("Corvus Data")');
  await p.waitForSelector(".drawer");
  // The notes are the part the caption is about, so wait for them rather than for the drawer.
  await p.waitForFunction(
    () => (document.querySelector("#notes")?.value?.length ?? 0) > 0,
    null,
    { timeout: 15000 },
  ).catch(() => console.warn("deal-drawer: notes never populated"));
});

await shot("research", async (p) => {
  await setTheme("dark");
  await p.click('.nav button:has-text("Research")');
  await p.fill("#root input.input", "acme.dev");
});

await shot("call", async (p) => {
  await setTheme("dark");
  // The front door is a form, not a button: everything she knows before she speaks comes from
  // the domain in that address, so the shot has to start the way the product does.
  await startCall(p);

  // She discloses, reads their site, then greets using something she found. Three turns.
  await greeted(p);

  // Then ask to see it, so the shot shows the thing that makes this a demo rather than a chat:
  // OUR product, driven live in a real browser, with Nadia talking over it.
  await say(p, "show me what it looks like");
  await p.waitForSelector(".screen-frame", { timeout: 120000 });

  // LET THE PAGE FINISH SCROLLING FIRST. The scroll is a 2.4s eased transform on a picture of a
  // whole page, and the shutter used to fire somewhere in the middle of it — which produced a
  // README image with a table row torn through the site's sticky header. It looks like a
  // rendering bug in the product rather than a screenshot taken too early, which is the worst
  // way for a picture on a front page to be wrong.
  await p
    .waitForFunction(
      () => {
        const el = document.querySelector(".screen-frame");
        return el && !el.hasAttribute("data-scrolling");
      },
      null,
      { timeout: 30000 },
    )
    .catch(() => console.warn("the frame never left its scrolling state"));

  // Wait for a frame where she is AUDIBLY speaking: `--loud` is the RMS of the audio playing at
  // that instant, so a high value means the shutter caught her mid-word rather than in the gap
  // between two clauses.
  await p
    .waitForFunction(
      () => {
        const el = document.querySelector(".meet .portrait, .meet [data-speaking]");
        if (el?.getAttribute("data-speaking") !== "true") return false;
        return Number(getComputedStyle(el).getPropertyValue("--loud")) > 0.2;
      },
      null,
      { timeout: 45000, polling: 20 },
    )
    .catch(() => console.warn("no loud frame captured within the window"));
}, 0);

// What she found, and the times she actually has. Two more panels from the same call, because
// the research and the booking are the parts a reader does not believe without seeing.
await shot("research-live", async (p) => {
  await setTheme("dark");
  await startCall(p);
  // The research result is a dossier now, not a bulleted list. `.fact-list` was the old markup.
  await p.waitForSelector(".dossier-rows dd", { timeout: 120000 });
}, 400);

await shot("booking", async (p) => {
  await setTheme("dark");
  await startCall(p);
  await greeted(p);
  await say(p, "can we book something");
  await p.waitForSelector(".slot", { timeout: 120000 });
  await p.click(".slot");
  await p.waitForSelector(".big-tick", { timeout: 120000 });
}, 400);

// The two panels that make it a closing agent rather than a lead form: their number, and a
// checkout for it. Both figures are computed on the server, so what is captured here is the
// same arithmetic a buyer would agree to.
await shot("quote", async (p) => {
  await setTheme("dark");
  await startCall(p);
  await greeted(p);
  // ASK IT THE WAY THIS TENANT'S BUYERS ASK IT. "40 people" is a seats question, and the
  // console opens on a GPU cloud -- so the card answered "sized from the 40 GPU-hours you
  // mentioned" about a sentence that said people. This also exercises the half of the quote
  // that is easy to get wrong: a rate unit multiplied by a stated duration.
  await say(p, "how much for 64 GPUs for two weeks?");
  await p.waitForSelector(".quote-figure", { timeout: 120000 });
}, 400);

await shot("checkout", async (p) => {
  await setTheme("dark");
  await startCall(p);
  await greeted(p);
  // ASK IT THE WAY THIS TENANT'S BUYERS ASK IT. "40 people" is a seats question, and the
  // console opens on a GPU cloud -- so the card answered "sized from the 40 GPU-hours you
  // mentioned" about a sentence that said people. This also exercises the half of the quote
  // that is easy to get wrong: a rate unit multiplied by a stated duration.
  await say(p, "how much for 64 GPUs for two weeks?");
  await p.waitForSelector(".quote-figure", { timeout: 120000 });
  await say(p, "great, sign me up");
  await p.waitForSelector('a:has-text("Open the checkout")', { timeout: 120000 });
}, 400);

// A GPU CLOUD'S OWN SITE, with their agent in the corner. Not our console: a different page, a
// different stylesheet, and the widget reached through the iframe boundary it actually ships
// behind — which is the point of the picture and the reason it is driven rather than mocked.
if (wanted("embed")) {
  // ITS OWN CONTEXT, BECAUSE THE SERVICE WORKER OWNS THIS ORIGIN. The console registers a
  // worker that answers navigations with the app shell, so once any console shot has run, the
  // customer's page is served OUR page and the launcher never exists. A fresh context has no
  // worker and no cache, which is also what a first-time visitor to their site actually gets.
  const shopCtx = await browser.newContext({
    viewport: { width: 1500, height: 980 },
    deviceScaleFactor: 2,
  });
  const page = await shopCtx.newPage();

  await page.goto(`${BASE}/demo/tessera.html`, { waitUntil: "networkidle" });
  await page.click("#rainmaker-embed button");
  const w = page.frameLocator("#rainmaker-embed iframe");

  await w.locator('input[autocomplete="name"]').fill("Priya Raman");
  await w.locator('input[type="email"]').fill("priya@anthology.ai");
  await w.locator('input[autocomplete="organization"]').fill("Anthology");
  await w.locator("button.w-go").click();

  // Her disclosure, the holding line, then the greeting -- the same three turns the console
  // shot waits for, counted through the frame.
  await w.locator(".w-turn").nth(2).waitFor({ timeout: 120000 });

  await w.locator(".w-bar input").fill("what does it cost for about 2,000 GPU hours a month?");
  await w.locator("button.w-send").click();

  // Wait for the priced answer rather than for a duration: the number is the claim, and a
  // fixed sleep photographs whatever happened to be on screen.
  await w
    .locator(".w-turn", { hasText: /per GPU-hour|GPU-hour/ })
    .last()
    .waitFor({ timeout: 120000 });
  await page.waitForTimeout(900);
  await page.screenshot({ path: resolve(OUT, "embed.png") });
  console.log("wrote docs/img/embed.png");
  await shopCtx.close();
}

// The offline shot. This is the product's actual claim, so it is captured against a genuinely
// severed connection rather than mocked: route abort kills the socket and every fetch, then
// edits are made and shown persisting with a pending count.
await shot("disconnected", async (p) => {
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
      "Edited with the server unreachable. Held on this device, reconciles on its own.",
    );
    await p.waitForTimeout(500);
    await p.keyboard.press("Escape");
  }
  await p.waitForTimeout(800);
});
await ctx.setOffline(false);

await browser.close();
console.log("done");
