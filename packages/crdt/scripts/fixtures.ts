/**
 * Generate cross-implementation agreement fixtures.
 *
 * The Python server has its own reducer over the same op log (services/api/.../materialise.py).
 * Two implementations of one merge rule WILL drift, and when they drift the API and the UI
 * disagree about a customer's pipeline — silently, because neither side errors.
 *
 * This script runs scenarios through the REAL TypeScript replica and writes what it produced.
 * The Python test suite then asserts its own reducer agrees. The fixture is generated rather
 * than hand-written on purpose: a hand-written expectation encodes what the author *believed*
 * both implementations do, which is exactly the assumption under test.
 *
 *     npx tsx packages/crdt/scripts/fixtures.ts
 */

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Replica } from "../src/document";
import type { Op } from "../src/types";

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = resolve(HERE, "../../../services/api/tests/fixtures/crdt_agreement.json");

/**
 * A fixed, shared wall clock.
 *
 * WHY THIS EXISTS. Half the cases below used to construct replicas with the default clock,
 * which is `Date.now()`. That put real wall time into both the HLC timestamp and the op id
 * (which encodes the wall in hex), so regenerating the file produced different bytes every
 * single run. CI regenerates the fixtures and asserts `git diff --exit-code` — a check that
 * could therefore only pass in the same millisecond the file was committed, and failed on
 * every push forever after.
 *
 * A monotonic counter shared by every replica keeps the ordering semantics honest (later ops
 * really do have later wall times, as they would in life) while making the output byte-stable.
 * The base is arbitrary but fixed; it is well clear of zero so the values still look like
 * plausible epoch milliseconds rather than accidentally exercising a zero-wall edge case.
 */
const CLOCK_BASE = 1_700_000_000_000;
let tick = 0;
const nextTick = () => CLOCK_BASE + tick++;

/** A replica on the shared deterministic clock. */
const replica = (actor: string, fixed?: number) =>
  new Replica(actor, fixed === undefined ? nextTick : () => fixed);

interface Case {
  name: string;
  ops: Op[];
  expected: Record<string, { fields: Record<string, unknown>; tags: string[] }>;
}

/** Replay ops into a fresh replica and read out what it decided. */
function settle(name: string, ops: Op[], entities: string[]): Case {
  const verifier = new Replica("verifier", nextTick);
  verifier.applyAll(ops);
  const expected: Case["expected"] = {};
  for (const id of entities) {
    const state = verifier.get("deal", id);
    expected[id] = {
      fields: Object.fromEntries(
        Object.entries(state?.fields ?? {}).map(([k, v]) => [k, v.value]),
      ),
      tags: verifier.tags("deal", id),
    };
  }
  return { name, ops, expected };
}

const cases: Case[] = [];

// 1. Plain last-writer-wins, delivered out of order.
{
  const r = replica("alice", 1000);
  const first = r.set("deal", "d1", "stage", "discovery");
  const r2 = replica("alice", 2000);
  const second = r2.set("deal", "d1", "stage", "negotiation");
  cases.push(settle("lww-out-of-order", [second, first], ["d1"]));
}

// 2. Concurrent writes with an identical clock — resolved purely by the actor tiebreak.
//    This is the case most likely to diverge between implementations, because it is the one
//    where the answer is arbitrary and both sides must pick the SAME arbitrary answer.
{
  const alice = replica("alice", 1000);
  const bob = replica("bob", 1000);
  const a = alice.set("deal", "d2", "owner", "alice");
  const b = bob.set("deal", "d2", "owner", "bob");
  cases.push(settle("concurrent-tiebreak", [a, b], ["d2"]));
  cases.push(settle("concurrent-tiebreak-reversed", [b, a], ["d2"]));
}

// 3. OR-Set: a remove that never observed a concurrent add.
{
  const alice = replica("alice");
  const bob = replica("bob");
  const add1 = alice.addTag("deal", "d3", "enterprise");
  bob.apply(add1);
  const remove = bob.removeTag("deal", "d3", "enterprise");
  const add2 = alice.addTag("deal", "d3", "enterprise");
  cases.push(settle("orset-concurrent-add-survives", [add1, remove, add2], ["d3"]));
}

// 4. A cancellation that arrives before the thing it cancels.
{
  const alice = replica("alice");
  const add = alice.addTag("deal", "d4", "vip");
  const remove = alice.removeTag("deal", "d4", "vip");
  cases.push(settle("orset-remove-before-add", [remove, add], ["d4"]));
}

// 5. Several fields, several actors, shuffled — the everyday case.
{
  const alice = replica("alice");
  const bob = replica("bob");
  const ops: Op[] = [
    alice.set("deal", "d5", "stage", "discovery"),
    bob.set("deal", "d5", "amount", 25000),
    alice.addTag("deal", "d5", "inbound"),
    bob.addTag("deal", "d5", "mid-market"),
    alice.set("deal", "d5", "stage", "proposal"),
    bob.removeTag("deal", "d5", "inbound"),
    alice.set("deal", "d5", "owner", "alice@acme.dev"),
  ];
  // Deterministic shuffle so the fixture is stable across regenerations.
  const shuffled = [...ops];
  let s = 12345;
  for (let i = shuffled.length - 1; i > 0; i--) {
    s = (s * 1664525 + 1013904223) % 4294967296;
    const j = Math.floor((s / 4294967296) * (i + 1));
    [shuffled[i], shuffled[j]] = [shuffled[j]!, shuffled[i]!];
  }
  cases.push(settle("mixed-multi-actor", shuffled, ["d5"]));
}

// 6. Duplicate delivery — the flaky-connection case.
{
  const alice = replica("alice");
  const ops = [
    alice.set("deal", "d6", "stage", "won"),
    alice.addTag("deal", "d6", "closed"),
  ];
  cases.push(settle("duplicates", [...ops, ...ops, ...ops], ["d6"]));
}

mkdirSync(dirname(OUT), { recursive: true });
writeFileSync(
  OUT,
  JSON.stringify(
    {
      generated_by: "packages/crdt/scripts/fixtures.ts",
      note:
        "Generated by the real TypeScript replica. Do not hand-edit: the value of this " +
        "fixture is that it records what the implementation actually does, not what " +
        "someone believed it does.",
      cases,
    },
    null,
    2,
  ),
  "utf-8",
);

console.log(`wrote ${cases.length} cases to ${OUT}`);
for (const c of cases) {
  console.log(`  ${c.name.padEnd(34)} ${c.ops.length} ops`);
}
