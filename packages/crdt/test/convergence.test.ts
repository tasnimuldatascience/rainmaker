/**
 * The tests that decide whether this CRDT is correct.
 *
 * Everything else in the sync layer — the WebSocket, the op log, the retry logic — is
 * plumbing that fails loudly. A CRDT fails SILENTLY: two reps see different pipelines, nobody
 * gets an error, and the divergence is discovered weeks later when someone notices a deal in
 * two stages at once. So convergence is tested as a property over randomly generated
 * histories rather than with hand-picked examples, because hand-picked examples only cover the
 * interleavings you already thought of.
 *
 * The core property, stated once:
 *
 *   For any set of operations S and any two permutations p, q of S:
 *     apply(p) on replica A  ==  apply(q) on replica B
 *
 * Strong eventual consistency. If that holds for random permutations at scale, the op design
 * is commutative; if it does not, no amount of transport reliability will save it.
 */

import { describe, expect, it } from "vitest";
import fc from "fast-check";
import { Replica } from "../src/document";
import type { Op } from "../src/types";
import { HybridClock, compare, encode, decode, ClockDriftError } from "../src/clock";

// ─────────────────────────────────────────────────────────────── helpers
function snapshot(r: Replica) {
  return r
    .list("deal")
    .map((e) => ({
      id: e.id,
      fields: Object.fromEntries(
        Object.entries(e.fields).map(([k, v]) => [k, v.value]),
      ),
      tags: r.tags("deal", e.id),
      notes: r.text("deal", e.id, "notes"),
    }))
    .sort((a, b) => a.id.localeCompare(b.id));
}

function shuffle<T>(items: readonly T[], seed: number): T[] {
  // Deterministic Fisher-Yates so a failing case is reproducible from its seed alone.
  const out = [...items];
  let s = seed || 1;
  const rand = () => (s = (s * 1664525 + 1013904223) % 4294967296) / 4294967296;
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.floor(rand() * (i + 1));
    [out[i], out[j]] = [out[j]!, out[i]!];
  }
  return out;
}

// ─────────────────────────────────────────────────────────────── clock
describe("hybrid logical clock", () => {
  it("is strictly monotonic even when the wall clock goes backwards", () => {
    // NTP correction, VM suspend/resume, a user changing the system time.
    let t = 1000;
    const clock = new HybridClock("a", () => t);
    const first = clock.tick();
    t = 900; // clock jumps back 100ms
    const second = clock.tick();
    const third = clock.tick();
    expect(compare(second, first)).toBeGreaterThan(0);
    expect(compare(third, second)).toBeGreaterThan(0);
  });

  it("orders a received event before any subsequent local event", () => {
    const a = new HybridClock("a", () => 1000);
    const b = new HybridClock("b", () => 500); // b's clock is behind
    const fromA = a.tick();
    b.observe(fromA);
    const afterObserve = b.tick();
    expect(compare(afterObserve, fromA)).toBeGreaterThan(0);
  });

  it("breaks ties by actor so the total order is identical on every replica", () => {
    const t = { wall: 1, counter: 1 };
    expect(compare({ ...t, actor: "a" }, { ...t, actor: "b" })).toBeLessThan(0);
    expect(compare({ ...t, actor: "b" }, { ...t, actor: "a" })).toBeGreaterThan(0);
    expect(compare({ ...t, actor: "a" }, { ...t, actor: "a" })).toBe(0);
  });

  it("refuses a timestamp far in the future instead of adopting it", () => {
    // One replica with a badly wrong clock would otherwise pin every peer into the future.
    const clock = new HybridClock("a", () => 1_000_000);
    expect(() =>
      clock.observe({ wall: 1_000_000 + 120_000, counter: 0, actor: "evil" }),
    ).toThrow(ClockDriftError);
  });

  it("round-trips through its encoding and sorts lexicographically", () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 0, max: 2 ** 40 }),
        fc.integer({ min: 0, max: 2 ** 24 }),
        fc.string({ minLength: 1, maxLength: 8 }).filter((s) => !s.includes(":")),
        (wall, counter, actor) => {
          const t = { wall, counter, actor };
          expect(decode(encode(t))).toEqual(t);
        },
      ),
    );
  });

  it("encodes so that string sort agrees with compare()", () => {
    fc.assert(
      fc.property(
        fc.array(
          fc.record({
            wall: fc.integer({ min: 0, max: 10_000 }),
            counter: fc.integer({ min: 0, max: 100 }),
            actor: fc.constantFrom("a", "b", "c"),
          }),
          { minLength: 2, maxLength: 25 },
        ),
        (stamps) => {
          const byCompare = [...stamps].sort(compare).map(encode);
          const byString = [...stamps].map(encode).sort();
          expect(byString).toEqual(byCompare);
        },
      ),
    );
  });
});

// ─────────────────────────────────────────────────────────────── convergence
describe("strong eventual consistency", () => {
  it("converges for any delivery order of a random history", () => {
    fc.assert(
      fc.property(
        fc.array(
          fc.record({
            actor: fc.constantFrom("alice", "bob", "carol"),
            entity: fc.constantFrom("d1", "d2"),
            action: fc.constantFrom(
              "stage",
              "amount",
              "addTag",
              "removeTag",
              "type",
              "delete",
            ),
            value: fc.integer({ min: 0, max: 5 }),
          }),
          { minLength: 1, maxLength: 60 },
        ),
        fc.integer({ min: 1, max: 100_000 }),
        (history, seed) => {
          // Generate the ops on independent replicas — this is what makes them concurrent.
          const authors = new Map<string, Replica>();
          const ops: Op[] = [];
          for (const [i, h] of history.entries()) {
            const r =
              authors.get(h.actor) ?? authors.set(h.actor, new Replica(h.actor)).get(h.actor)!;
            switch (h.action) {
              case "stage":
                ops.push(r.set("deal", h.entity, "stage", `s${h.value}`));
                break;
              case "amount":
                ops.push(r.set("deal", h.entity, "amount", h.value * 1000));
                break;
              case "addTag":
                ops.push(r.addTag("deal", h.entity, `t${h.value}`));
                break;
              case "removeTag":
                ops.push(r.removeTag("deal", h.entity, `t${h.value}`));
                break;
              case "type":
                ops.push(...r.insertText("deal", h.entity, "notes", 0, String(h.value)));
                break;
              case "delete":
                ops.push(...r.deleteText("deal", h.entity, "notes", 0, 1));
                break;
            }
            void i;
          }

          // Replay the same ops into two fresh replicas in different orders.
          const left = new Replica("left");
          const right = new Replica("right");
          left.applyAll(shuffle(ops, seed));
          right.applyAll(shuffle(ops, seed * 7919 + 13));

          expect(snapshot(left)).toEqual(snapshot(right));
          expect(left.pendingOrphans).toBe(0);
          expect(right.pendingOrphans).toBe(0);
        },
      ),
      { numRuns: 300 },
    );
  });

  it("is idempotent under duplicate delivery", () => {
    // The common case on a flaky connection: the client retries, the server redelivers.
    const author = new Replica("a");
    const ops = [
      author.set("deal", "d1", "stage", "won"),
      author.addTag("deal", "d1", "urgent"),
      ...author.insertText("deal", "d1", "notes", 0, "hello"),
    ];
    const once = new Replica("x");
    once.applyAll(ops);
    const twice = new Replica("y");
    twice.applyAll(ops);
    twice.applyAll(ops);
    twice.applyAll(shuffle(ops, 42));
    expect(snapshot(twice)).toEqual(snapshot(once));
  });

  it("survives ops arriving before the entity they refer to exists", () => {
    const author = new Replica("a");
    const setOp = author.set("deal", "ghost", "stage", "won");
    const tagOp = author.addTag("deal", "ghost", "vip");
    const replica = new Replica("b");
    // Deliberately reversed, and the entity was never "created" by any prior op.
    replica.applyAll([tagOp, setOp]);
    expect(replica.field("deal", "ghost", "stage")).toBe("won");
    expect(replica.tags("deal", "ghost")).toEqual(["vip"]);
  });
});

// ─────────────────────────────────────────────────────────────── LWW register
describe("last-writer-wins register", () => {
  it("resolves a concurrent edit identically on both replicas", () => {
    const alice = new Replica("alice", () => 1000);
    const bob = new Replica("bob", () => 1000); // identical wall clock: pure tiebreak
    const fromAlice = alice.set("deal", "d1", "stage", "negotiation");
    const fromBob = bob.set("deal", "d1", "stage", "closed-won");

    alice.apply(fromBob);
    bob.apply(fromAlice);
    expect(alice.field("deal", "d1", "stage")).toBe(bob.field("deal", "d1", "stage"));
  });

  it("does not let an older write clobber a newer one on redelivery", () => {
    let t = 1000;
    const r = new Replica("a", () => t);
    const old = r.set("deal", "d1", "amount", 100);
    t = 2000;
    r.set("deal", "d1", "amount", 500);
    r.apply(old); // late redelivery of the stale write
    expect(r.field("deal", "d1", "amount")).toBe(500);
  });
});

// ─────────────────────────────────────────────────────────────── OR-Set
describe("observed-remove set", () => {
  it("keeps a concurrent add that the remove never observed", () => {
    // The behaviour a remove-by-value implementation gets wrong.
    const alice = new Replica("alice");
    const bob = new Replica("bob");
    const add1 = alice.addTag("deal", "d1", "enterprise");
    bob.apply(add1);

    const remove = bob.removeTag("deal", "d1", "enterprise"); // bob saw add1
    const add2 = alice.addTag("deal", "d1", "enterprise"); // concurrent, unseen by bob

    alice.apply(remove);
    bob.apply(add2);
    expect(alice.tags("deal", "d1")).toEqual(["enterprise"]);
    expect(bob.tags("deal", "d1")).toEqual(["enterprise"]);
  });

  it("removes a tag when every observed instance is cancelled", () => {
    const r = new Replica("a");
    r.addTag("deal", "d1", "urgent");
    r.removeTag("deal", "d1", "urgent");
    expect(r.tags("deal", "d1")).toEqual([]);
  });

  it("handles a remove that arrives before its add", () => {
    const alice = new Replica("alice");
    const add = alice.addTag("deal", "d1", "vip");
    const remove = alice.removeTag("deal", "d1", "vip");
    const out = new Replica("b");
    out.applyAll([remove, add]); // reversed
    // The remove named this exact instance, so the add must not resurrect it.
    expect(out.tags("deal", "d1")).toEqual([]);
  });
});

// ─────────────────────────────────────────────────────────────── RGA text
describe("collaborative text", () => {
  it("preserves both edits when two people type at the same position", () => {
    const alice = new Replica("alice");
    const seed = alice.insertText("deal", "d1", "notes", 0, "hello");
    const bob = new Replica("bob");
    bob.applyAll(seed);

    const fromAlice = alice.insertText("deal", "d1", "notes", 5, " world");
    const fromBob = bob.insertText("deal", "d1", "notes", 5, " there");

    alice.applyAll(fromBob);
    bob.applyAll(fromAlice);

    const text = alice.text("deal", "d1", "notes");
    expect(text).toBe(bob.text("deal", "d1", "notes"));
    expect(text).toContain("hello");
    expect(text).toContain("world");
    expect(text).toContain("there");
    expect(text.length).toBe("hello".length + " world".length + " there".length);
  });

  it("converges when an insert arrives before the character it anchors to", () => {
    const author = new Replica("a");
    const ops = author.insertText("deal", "d1", "notes", 0, "abcdef");
    const out = new Replica("b");
    out.applyAll([...ops].reverse()); // every insert arrives before its parent
    expect(out.text("deal", "d1", "notes")).toBe("abcdef");
    expect(out.pendingOrphans).toBe(0);
  });

  it("keeps a concurrent insert anchored to a character that was deleted", () => {
    // Tombstones exist for exactly this: dropping the character would orphan the insert.
    const alice = new Replica("alice");
    const seed = alice.insertText("deal", "d1", "notes", 0, "abc");
    const bob = new Replica("bob");
    bob.applyAll(seed);

    const del = alice.deleteText("deal", "d1", "notes", 1, 1); // remove "b"
    const ins = bob.insertText("deal", "d1", "notes", 2, "X"); // anchored after "b"

    alice.applyAll(ins);
    bob.applyAll(del);
    expect(alice.text("deal", "d1", "notes")).toBe(bob.text("deal", "d1", "notes"));
    expect(alice.text("deal", "d1", "notes")).toContain("X");
  });

  it("deleting the same character twice is a no-op", () => {
    const r = new Replica("a");
    r.insertText("deal", "d1", "notes", 0, "abc");
    const first = r.deleteText("deal", "d1", "notes", 1, 1);
    r.applyAll(first);
    r.applyAll(first);
    expect(r.text("deal", "d1", "notes")).toBe("ac");
  });

  it("handles a long document without blowing the stack", () => {
    // The insertion tree is walked iteratively; a recursive walk dies well before this.
    const r = new Replica("a");
    r.insertText("deal", "d1", "notes", 0, "x".repeat(5000));
    expect(r.text("deal", "d1", "notes").length).toBe(5000);
  });
});

describe("sibling ordering", () => {
  /**
   * Character ids are `actor:seq`. Comparing them as strings puts "t:10" before "t:6", because
   * '1' sorts before '6' -- so text scrambles once a replica has emitted more than ten
   * operations, which is about two sentences of typing.
   *
   * The convergence tests above did not catch it, and could not: they assert that two replicas
   * AGREE, and both replicas agreed on the same wrong order. Convergence on a wrong answer is
   * still convergence. These tests assert the RESULT instead.
   */
  it("orders inserts correctly past sequence number 10", () => {
    const replica = new Replica("t");
    // Eleven characters, so the next insert is seq 11 and collides with seq 6 under a string
    // comparison.
    replica.applyAll(replica.insertText("deal", "d1", "notes", 0, "great news"));
    replica.applyAll(replica.insertText("deal", "d1", "notes", 6, "big "));
    expect(replica.text("deal", "d1", "notes")).toBe("great big news");
  });

  it("keeps a long note in order through many separate edits", () => {
    const replica = new Replica("t");
    replica.applyAll(replica.insertText("deal", "d1", "notes", 0, "one"));
    replica.applyAll(replica.insertText("deal", "d1", "notes", 3, " two"));
    replica.applyAll(replica.insertText("deal", "d1", "notes", 7, " three"));
    replica.applyAll(replica.insertText("deal", "d1", "notes", 13, " four"));
    replica.applyAll(replica.insertText("deal", "d1", "notes", 18, " five"));
    expect(replica.text("deal", "d1", "notes")).toBe("one two three four five");
  });

  it("inserts into the middle of a long note at the right place", () => {
    const replica = new Replica("t");
    const seed = "the quick brown fox jumps over the lazy dog";
    replica.applyAll(replica.insertText("deal", "d1", "notes", 0, seed));
    replica.applyAll(replica.insertText("deal", "d1", "notes", 4, "very "));
    expect(replica.text("deal", "d1", "notes")).toBe(
      "the very quick brown fox jumps over the lazy dog",
    );
  });

  it("two actors inserting at the same point still converge", () => {
    const alice = new Replica("alice");
    const bob = new Replica("bob");
    const seed = alice.insertText("deal", "d1", "notes", 0, "hello world");
    alice.applyAll(seed);
    bob.applyAll(seed);

    const aliceOps = alice.insertText("deal", "d1", "notes", 5, " there");
    const bobOps = bob.insertText("deal", "d1", "notes", 5, " big");

    alice.applyAll(aliceOps);
    alice.applyAll(bobOps);
    bob.applyAll(bobOps);
    bob.applyAll(aliceOps);

    const merged = alice.text("deal", "d1", "notes");
    expect(bob.text("deal", "d1", "notes")).toBe(merged);
    expect(merged).toContain("there");
    expect(merged).toContain("big");
    expect(merged.startsWith("hello")).toBe(true);
    expect(merged.endsWith("world")).toBe(true);
  });
});
