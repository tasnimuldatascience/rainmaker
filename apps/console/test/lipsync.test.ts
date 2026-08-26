/**
 * The mouth patch has to land exactly where it was cut from.
 *
 * `calls/lipsync.py` crops the portrait at FACE_FRACTIONS, hands that crop to Wav2Lip, and sends
 * back a patch of the same region. The console draws it into a rectangle defined in CSS. Those
 * are the same rectangle expressed twice, in two languages, and nothing links them — so a change
 * to one silently slides the generated mouth off the real one, which looks like a broken model
 * rather than a broken constant.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const LIPSYNC = fileURLToPath(
  new URL("../../../services/api/src/rainmaker/calls/lipsync.py", import.meta.url),
);
const PORTRAIT = fileURLToPath(new URL("../src/components/Portrait.tsx", import.meta.url));

/** FACE_FRACTIONS = (left, top, right, bottom) as fractions of the portrait. */
function pythonFractions(): [number, number, number, number] {
  const source = readFileSync(LIPSYNC, "utf8");
  const match = source.match(/FACE_FRACTIONS\s*=\s*\(([^)]+)\)/);
  if (!match) throw new Error("FACE_FRACTIONS is gone from lipsync.py");
  const parts = match[1]!.split(",").map((n) => Number(n.trim()));
  return [parts[0]!, parts[1]!, parts[2]!, parts[3]!];
}

/** `MOUTH_BOX` in Portrait.tsx: the same rectangle, in the language that draws it. */
function consoleBox(): number[] {
  const source = readFileSync(PORTRAIT, "utf8");
  const match = source.match(/MOUTH_BOX\s*=\s*\[([^\]]+)\]/);
  if (!match) throw new Error("MOUTH_BOX is gone from Portrait.tsx");
  return match[1]!.split(",").map((n) => Number(n.trim()));
}

describe("the generated mouth", () => {
  it("is drawn over exactly the region the model was given", () => {
    expect(consoleBox()).toEqual([...pythonFractions()]);
  });

  it("is placed against the rendered image, not against its container", () => {
    // The bug this pins: the fractions are of the 512x512 PORTRAIT, and the window it sits in
    // is 4:3 with `object-fit: cover`. Written as CSS percentages of the container, the
    // generated mouth landed over her eye.
    const source = readFileSync(PORTRAIT, "utf8");
    expect(source).toContain("coverRect");
    expect(source).toMatch(/naturalWidth/);
  });

  it("covers the lower half of the face, which is what Wav2Lip regenerates", () => {
    const [, top, , bottom] = pythonFractions();
    expect(top).toBeGreaterThan(0.25);
    expect(bottom).toBeGreaterThan(top);
  });

  it("moves with the portrait, or it slides off during the drift", () => {
    const source = readFileSync(
      fileURLToPath(new URL("../src/styles/app.css", import.meta.url)),
      "utf8",
    );
    const block = source.slice(source.indexOf(".portrait-mouth"), source.indexOf(".portrait-bloom"));
    // The photo is translated and scaled every frame; the patch has to take the same transform
    // or it detaches from the face it belongs to.
    expect(block).toContain("--drift-x");
    expect(block).toContain("--scale");
  });
});
