/**
 * Tests for the text diff that turns an edit box into sync operations.
 *
 * This function sits on the boundary between the UI and the sync layer. Every keystroke in a
 * shared note goes through it, and a mistake here does not throw -- it produces operations that
 * are individually valid and collectively wrong, so two people editing the same note quietly end
 * up with different text.
 *
 * The property that matters is stated directly: applying the operations this produces must turn
 * `before` into `after`. Everything below is that property, on the cases that break it.
 */

import { describe, expect, it } from "vitest";
import { Replica } from "@rainmaker/crdt";
import { diffToOps } from "../src/lib/store";

/** Apply an edit through diffToOps and read back what the replica now holds. */
function applyEdit(before: string, after: string): string {
  const replica = new Replica("tester");
  if (before) {
    replica.applyAll(replica.insertText("deal", "d1", "notes", 0, before));
  }
  replica.applyAll(diffToOps(replica, "deal", "d1", "notes", before, after));
  return replica.text("deal", "d1", "notes");
}

describe("diffToOps", () => {
  it("emits nothing when the text has not changed", () => {
    const replica = new Replica("tester");
    expect(diffToOps(replica, "deal", "d1", "notes", "hello", "hello")).toEqual([]);
  });

  const cases: [name: string, before: string, after: string][] = [
    ["typing one character at the end", "hell", "hello"],
    ["typing one character at the start", "ello", "hello"],
    ["typing in the middle", "helo", "hello"],
    ["deleting one character", "hello", "hell"],
    ["deleting from the middle", "hello", "hllo"],
    ["writing into an empty field", "", "hello"],
    ["clearing the field entirely", "hello", ""],
    ["pasting over a selection", "call them Tuesday", "call them Friday"],
    ["replacing everything", "old note", "completely different"],
    ["a repeated character run", "aaa", "aaaa"],
    // The prefix and suffix scans can overlap when the same characters appear at both ends.
    // Getting the overlap wrong duplicates or drops a character, and only shows up on
    // symmetric strings like these.
    ["shared prefix and suffix", "abcba", "abba"],
    ["shared prefix and suffix, growing", "abba", "abcba"],
    ["the whole string is one repeated character", "xxxx", "xx"],
    // Emoji are two UTF-16 units but one character in the text sequence. Mixing the two puts
    // every later edit one position too far right, per emoji -- silently, and only for the
    // people who type emoji, which in sales notes is most of them.
    ["editing after an emoji", "deal 🎯 open", "deal 🎯 closed"],
    ["editing after several emoji", "🎯🔥🚀 open", "🎯🔥🚀 closed"],
    ["editing before an emoji", "open 🎯 deal", "closed 🎯 deal"],
    ["inserting an emoji", "great news", "great 🎉 news"],
    ["deleting an emoji", "great 🎉 news", "great news"],
    ["emoji at both ends", "🎯 call them 🔥", "🎯 email them 🔥"],
    ["accented characters", "café meeting", "café meetings"],
  ];

  for (const [name, before, after] of cases) {
    it(`applies the right edit when ${name}`, () => {
      expect(applyEdit(before, after)).toBe(after);
    });
  }

  it("emits the smallest edit rather than replacing the whole field", () => {
    // The reason this function exists. Replacing the whole note on every keystroke would be
    // correct and would also destroy anything a colleague typed at the same time, because a
    // wholesale replacement has no way to merge with a concurrent edit elsewhere in the text.
    const replica = new Replica("tester");
    replica.applyAll(replica.insertText("deal", "d1", "notes", 0, "budget approved"));
    const ops = diffToOps(replica, "deal", "d1", "notes", "budget approved", "budget approved!");
    expect(ops.length).toBeLessThan(5);
  });

  it("survives a round trip through many small edits", () => {
    // What typing actually looks like: one character at a time, each edit built on the last.
    const replica = new Replica("tester");
    let current = "";
    for (const char of "follow up next week") {
      const next = current + char;
      replica.applyAll(diffToOps(replica, "deal", "d1", "notes", current, next));
      current = next;
    }
    expect(replica.text("deal", "d1", "notes")).toBe("follow up next week");
  });

  it("two people editing different parts of a note keep both edits", () => {
    // The whole point of the sync layer, exercised through the diff that feeds it.
    const alice = new Replica("alice");
    const bob = new Replica("bob");
    const seed = alice.insertText("deal", "d1", "notes", 0, "call them");
    alice.applyAll(seed);
    bob.applyAll(seed);

    const aliceOps = diffToOps(alice, "deal", "d1", "notes", "call them", "please call them");
    const bobOps = diffToOps(bob, "deal", "d1", "notes", "call them", "call them Tuesday");

    // Delivered in opposite orders, as they would arrive over an unreliable connection.
    alice.applyAll(aliceOps);
    alice.applyAll(bobOps);
    bob.applyAll(bobOps);
    bob.applyAll(aliceOps);

    const merged = alice.text("deal", "d1", "notes");
    expect(bob.text("deal", "d1", "notes")).toBe(merged);
    expect(merged).toContain("please");
    expect(merged).toContain("Tuesday");
  });
});
