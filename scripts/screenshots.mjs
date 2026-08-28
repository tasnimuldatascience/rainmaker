/**
 * Capture console screenshots for the README.
 *
 * Scripted rather than hand-captured so the images can be regenerated after a UI change
 * instead of silently going stale. Both themes, and the disconnected state — which is a
 * claim nobody believes without a picture.
 *
 *   node scripts/screenshots.mjs [--base http://127.0.0.1:5174]
 */

import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const OUT = resolve(ROOT, "docs/img");
const BASE = process.argv.includes("--base")
  ? process.argv[process.argv.indexOf("--base") + 1]
  : "http://127.0.0.1:5174";

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
  await say(p, "how much would it be for 40 people?");
  await p.waitForSelector(".quote-figure", { timeout: 120000 });
}, 400);

await shot("checkout", async (p) => {
  await setTheme("dark");
  await startCall(p);
  await greeted(p);
  await say(p, "how much would it be for 40 people?");
  await p.waitForSelector(".quote-figure", { timeout: 120000 });
  await say(p, "great, sign me up");
  await p.waitForSelector('a:has-text("Open the checkout")', { timeout: 120000 });
}, 400);

// Consecutive frames of her face mid-sentence, for the README's lip-sync claim. Cropped to the
// floating window and captured a fraction of a second apart, so the mouths visibly differ --
// the claim is that she is talking, and a single still cannot show that.
{
  await page.goto(BASE, { waitUntil: "networkidle" });
  await setTheme("dark");
  await page.click('.nav button:has-text("Live call")');
  // The mouth strip is evidence about Wav2Lip, so it has to be the photoreal face — the console
  // opens on the illustrated one, which is a different claim entirely.
  await page.waitForSelector(".face-switch", { timeout: 20000 });
  await page.click('.face-switch button:has-text("Photoreal")');
  await page.fill('.intake input[autocomplete="name"]', "Dana Whitfield");
  await page.fill('.intake input[type="email"]', "dana.whitfield@stripe.com");
  await page.fill('.intake input[autocomplete="organization"]', "Stripe");
  await page.click('button:has-text("Start the call")');

  const drawing = await page
    .waitForFunction(
      () => {
        const c = document.querySelector(".portrait-mouth");
        return c !== null && getComputedStyle(c).opacity === "1";
      },
      null,
      { timeout: 120000 },
    )
    .then(() => true)
    .catch(() => false);

  if (!drawing) {
    console.warn("no mouth frames — skipping mouth.png (is the Wav2Lip checkpoint installed?)");
  } else {
    const shots = [];
    for (let i = 0; i < 5; i += 1) {
      const pip = await page.$(".rail-face, .meet-hero");
      shots.push(await pip.screenshot());
      await page.waitForTimeout(120);
    }
    const { createCanvas, loadImage } = await import("node:module").then(() => ({}))
      .catch(() => ({}));
    // No canvas dependency: write the frames and stitch them with sharp-free maths in Python
    // is overkill, so the strip is assembled by the browser itself.
    const encoded = shots.map((b) => b.toString("base64"));
    const strip = await page.evaluate(async (frames) => {
      const images = await Promise.all(
        frames.map(
          (data) =>
            new Promise((resolve) => {
              const img = new Image();
              img.onload = () => resolve(img);
              img.src = `data:image/png;base64,${data}`;
            }),
        ),
      );
      const canvas = document.createElement("canvas");
      canvas.width = images[0].width * images.length;
      canvas.height = images[0].height;
      const ctx = canvas.getContext("2d");
      images.forEach((img, i) => ctx.drawImage(img, i * img.width, 0));
      return canvas.toDataURL("image/png").split(",")[1];
    }, encoded);
    writeFileSync(resolve(OUT, "mouth.png"), Buffer.from(strip, "base64"));
    console.log("wrote docs/img/mouth.png");
  }
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
