/**
 * The progress rail has to cover every step the agenda can emit.
 *
 * WHY THIS IS A TEST AND NOT A COMMENT. `opening` was missing from the rail, so for the three
 * seconds Nadia spends introducing herself `findIndex` returned -1, every pill fell through to
 * "idle", and the rail read as broken at exactly the moment a first-time viewer is looking at
 * it. Nothing failed; it just went blank. A list that has to stay in step with a Python enum is
 * the kind of thing that drifts silently on the next step someone adds.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const AGENDA = fileURLToPath(
  new URL("../../../services/api/src/rainmaker/calls/agenda.py", import.meta.url),
);
const CALL_VIEW = fileURLToPath(new URL("../src/components/CallView.tsx", import.meta.url));

/** The `Step` StrEnum in agenda.py, read from the source rather than duplicated here. */
function pythonSteps(): string[] {
  const source = readFileSync(AGENDA, "utf8");
  const block = source.slice(source.indexOf("class Step(StrEnum)"), source.indexOf("# ─", source.indexOf("class Step(StrEnum)")));
  return [...block.matchAll(/^\s+[A-Z_]+ = "([a-z_]+)"$/gm)].map((m) => m[1]!);
}

function railSteps(): string[] {
  const source = readFileSync(CALL_VIEW, "utf8");
  const block = source.slice(source.indexOf("const STEPS"), source.indexOf("];", source.indexOf("const STEPS")));
  return [...block.matchAll(/id: "([a-z_]+)"/g)].map((m) => m[1]!);
}

/**
 * Steps that are real but are not stages of the plan.
 *
 * `booking` is the ESCALATION: the product's claim is that a buyer never waits for a rep, so a
 * call that reaches the diary has left the funnel rather than advanced along it. Showing it as
 * the pill after "checkout" would read as though every call is supposed to end there. CallView
 * renders it separately, next to `handoff`.
 */
const NOT_ON_THE_RAIL = new Set(["handoff", "booking"]);

describe("the call progress rail", () => {
  it("knows about every step the agenda can be in", () => {
    const expected = pythonSteps().filter((step) => !NOT_ON_THE_RAIL.has(step));
    expect(railSteps()).toEqual(expected);
  });

  it("found some steps at all, so a rename does not silently pass this", () => {
    expect(pythonSteps().length).toBeGreaterThan(5);
    expect(railSteps().length).toBeGreaterThan(5);
  });
});
