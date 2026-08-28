"""Generate the compacted-vs-full agreement fixture for the TypeScript replica.

THE PYTHON SUITE CANNOT CHECK THE PART THAT MATTERS MOST. `crm/materialise.py` deliberately does
not implement RGA — it reports a note's length, not its text — and porting RGA to Python to test
compaction would create the second sequence CRDT that module exists to avoid. So the Python tests
can prove compaction preserves fields, tags and character counts, and they cannot prove it
preserves the TEXT. Character count is exactly the property an anchor bug does not change: an
orphaned character is still counted, it just never renders.

This is the same bridge `packages/crdt/scripts/fixtures.ts` builds, pointed the other way. That
script runs scenarios through the real replica so Python can assert agreement; this one runs
scenarios through the real compactor so TypeScript can. `packages/crdt/test/compaction.test.ts`
replays each case's `full` log and its `compacted` log into two replicas and asserts the rendered
text is identical — which is the claim, stated in the only implementation that can evaluate it.

Regenerate with:

    python services/api/tests/compaction_fixture.py

`test_compaction.py` asserts the committed file matches what this produces, so it cannot go stale
without a test failing. That means the output must be BYTE-STABLE: every timestamp comes from the
counter below and every random choice from a fixed seed, for the same reason fixtures.ts stopped
using `Date.now()` — a fixture that changes on every run is a fixture nobody can check.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Any

# Runnable as a script from anywhere in the repo, which means the package it is testing has to be
# importable without an editable install. The suite itself gets this from the installed package;
# a developer regenerating the fixture on a fresh clone does not.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rainmaker.sync.oplog import OpLog  # noqa: E402

OUT = Path(__file__).resolve().parent / "fixtures" / "compaction_agreement.json"

#: Well clear of zero so the values look like plausible epoch milliseconds, and safely in the past
#: so the replica's drift guard (`MAX_DRIFT_MS`) never refuses one of these timestamps. Same base
#: as fixtures.ts, for no reason other than that a reader comparing the two files should not have
#: to wonder whether the difference means something.
CLOCK_BASE = 1_700_000_000_000


class Clock:
    """A monotonic wall clock shared by every author in a case.

    Shared rather than per-author because a later op must really have a later timestamp — that is
    what makes the generated histories orderable the way real ones are. Authors that each counted
    from zero would produce ties on every op and exercise only the actor tiebreak.
    """

    def __init__(self) -> None:
        self._tick = 0

    def stamp(self, actor: str) -> dict[str, Any]:
        self._tick += 1
        return {"wall": CLOCK_BASE + self._tick, "counter": 0, "actor": actor}

    def concurrent(self, actor: str) -> dict[str, Any]:
        """A timestamp at the CURRENT wall, so two authors collide and the actor breaks the tie."""
        return {"wall": CLOCK_BASE + self._tick, "counter": 0, "actor": actor}


class Author:
    """One replica's op factory: the wire form the console would have sent.

    Deliberately hand-built rather than driven through the TypeScript replica. The point of these
    logs is to contain shapes a well-behaved replica emits rarely — a character anchored to one
    another replica already deleted, a delete that overtakes its insert — and reproducing those
    through the real client would mean orchestrating replicas and delivery orders to provoke them.
    """

    def __init__(self, actor: str, clock: Clock, kind: str = "deal"):
        self.actor = actor
        self.kind = kind
        self.clock = clock
        self._seq = 0

    def _op(self, op_type: str, entity: str, ts: dict[str, Any], **extra: Any) -> dict[str, Any]:
        self._seq += 1
        # Mirrors `document.ts`'s `${encode(ts)}#${seq}` so the ids in the fixture look like the
        # ids a real client mints, and so no two authors can collide on one.
        op_id = f"{ts['wall']:012x}:{ts['counter']:08x}:{self.actor}#{self._seq}"
        return {
            "id": op_id,
            "ts": ts,
            "actor": self.actor,
            "kind": self.kind,
            "entityId": entity,
            "type": op_type,
            **extra,
        }

    def set(self, entity: str, field: str, value: Any, *, concurrent: bool = False):
        ts = self.clock.concurrent(self.actor) if concurrent else self.clock.stamp(self.actor)
        return self._op("set", entity, ts, field=field, value=value)

    def add_tag(self, entity: str, tag: str, instance: str | None = None):
        instance = instance or f"{self.actor}:{self._seq + 1}"
        return self._op("addTag", entity, self.clock.stamp(self.actor), tag=tag,
                        instance=instance)

    def remove_tag(self, entity: str, tag: str, instances: list[str]):
        return self._op("removeTag", entity, self.clock.stamp(self.actor), tag=tag,
                        instances=list(instances))

    def insert(self, entity: str, field: str, after: str | None, char: str,
               char_id: str | None = None):
        char_id = char_id or f"{self.actor}:{self._seq + 1}"
        return self._op("insertText", entity, self.clock.stamp(self.actor), field=field,
                        after=after, charId=char_id, char=char)

    def delete(self, entity: str, field: str, char_id: str):
        return self._op("deleteText", entity, self.clock.stamp(self.actor), field=field,
                        charId=char_id)


def unacknowledged_tail(name: str) -> dict[str, Any]:
    """An op nobody has acknowledged yet, appended to a fully-acked case.

    WITHOUT THIS EVERY "FULLY ACKED" CASE WOULD TEST THE WRONG THING. `OpLog.compact` never
    prunes the workspace's head op, so a watermark equal to the last sequence number collapses to
    `head - 1` and the final delete of the log falls above the line — which drags its whole anchor
    chain back into retention and the case quietly stops exercising the rule it was written for.
    The first draft of this file did exactly that: `fully-deleted-run` dropped nothing and looked
    like a passing test. A real workspace always has a tail nobody has confirmed; the fixture
    should too.
    """
    ts = {"wall": CLOCK_BASE + 900_000, "counter": 0, "actor": "janitor"}
    return {
        "id": f"tail#{name}",
        "ts": ts,
        "actor": "janitor",
        "kind": "deal",
        "entityId": "d9",
        "type": "set",
        "field": "heartbeat",
        "value": name,
    }


def compacted_case(
    name: str, ops: list[dict[str, Any]], watermark: int | None = None
) -> dict[str, Any]:
    """Run one op log through the real compactor and record both sides of the comparison.

    `watermark=None` means every op listed has been acknowledged by every replica; see
    `unacknowledged_tail` for why that case still needs one op that has not.
    """
    if watermark is None:
        watermark = len(ops)
        ops = [*ops, unacknowledged_tail(name)]
    oplog = OpLog(":memory:")
    oplog.append("ws", ops)
    report = oplog.compact("ws", watermark)
    compacted = [stored.payload for stored in oplog.since("ws", 0, limit=100_000)]
    oplog.close()
    return {
        "name": name,
        "watermark": report.watermark,
        "full": ops,
        "compacted": compacted,
        "dropped": report.dropped,
        "by_rule": report.by_rule,
        "anchors_retained": report.anchors_retained,
    }


# ───────────────────────────────────────────────────────────── hand-built shapes
def _case_deleted_anchor_with_a_live_child() -> dict[str, Any]:
    """The crux. "ab" typed, "b" deleted, and a character anchored to the dead "b".

    If compaction collects "b" the anchored character has no parent, the replica parks it in
    `orphans`, and the text loses a character on that replica alone. Every other rule in the
    compactor is arithmetic; this one is the reason the module needed a cross-implementation test.
    """
    clock = Clock()
    alice, bob = Author("alice", clock), Author("bob", clock)
    a = alice.insert("d1", "notes", None, "a")
    b = alice.insert("d1", "notes", a["charId"], "b")
    # Bob typed after "b" before Alice's delete reached him: the everyday concurrent edit.
    x = bob.insert("d1", "notes", b["charId"], "X")
    delete_b = alice.delete("d1", "notes", b["charId"])
    ops = [a, b, x, delete_b]
    return compacted_case("deleted-anchor-with-a-live-child", ops)


def _case_tombstone_chain() -> dict[str, Any]:
    """Three deleted characters in a row, the last of them anchoring a live one.

    Retention has to be transitive: keeping only the immediate parent leaves that parent an
    orphan instead, which moves the bug one character to the left rather than fixing it.
    """
    clock = Clock()
    alice, bob = Author("alice", clock), Author("bob", clock)
    chain = []
    after: str | None = None
    for char in "abc":
        op = alice.insert("d1", "notes", after, char)
        chain.append(op)
        after = op["charId"]
    tail = bob.insert("d1", "notes", after, "Z")
    deletes = [alice.delete("d1", "notes", op["charId"]) for op in chain]
    ops = [*chain, tail, *deletes]
    return compacted_case("tombstone-chain-under-a-live-character", ops)


def _case_fully_deleted_run() -> dict[str, Any]:
    """A word typed and then deleted with nothing anchored to it: all of it should go.

    The counterweight to the two cases above. A compactor that retains every tombstone is
    trivially correct and completely useless, so at least one case has to assert that ops
    actually disappear.
    """
    clock = Clock()
    alice = Author("alice", clock)
    inserts = []
    after: str | None = None
    for char in "scratch":
        op = alice.insert("d1", "notes", after, char)
        inserts.append(op)
        after = op["charId"]
    keeper = alice.insert("d1", "notes", None, "K")
    deletes = [alice.delete("d1", "notes", op["charId"]) for op in inserts]
    ops = [*inserts, keeper, *deletes]
    return compacted_case("fully-deleted-run", ops)


def _case_delete_before_its_insert() -> dict[str, Any]:
    """The tombstone arrives at the log before the character it cancels.

    `pendingDeletes` exists for this and the compactor must not out-clever it: the pair is only
    collectable together, and the insert being later in the log does not change that.
    """
    clock = Clock()
    alice = Author("alice", clock)
    seed = alice.insert("d1", "notes", None, "h")
    doomed = alice.insert("d1", "notes", seed["charId"], "q")
    ops = [seed, alice.delete("d1", "notes", doomed["charId"]), doomed]
    return compacted_case("delete-before-its-insert", ops)


def _case_watermark_splits_a_pair() -> dict[str, Any]:
    """A character whose insert is acknowledged and whose delete is not.

    Nothing about the pair may be collected: a replica sitting on the watermark has the character
    and has not yet heard it was deleted, and dropping the insert leaves it with a character no
    remaining op explains.
    """
    clock = Clock()
    alice = Author("alice", clock)
    a = alice.insert("d1", "notes", None, "a")
    b = alice.insert("d1", "notes", a["charId"], "b")
    delete_b = alice.delete("d1", "notes", b["charId"])
    ops = [a, b, delete_b]
    # Acknowledged through the inserts only; the delete is above the line.
    return compacted_case("watermark-splits-an-insert-delete-pair", ops, watermark=2)


def _case_mixed_entity() -> dict[str, Any]:
    """Registers, tags and text on the same entity, so the three rules run over one log."""
    clock = Clock()
    alice, bob = Author("alice", clock), Author("bob", clock)
    tag = alice.add_tag("d1", "enterprise")
    text: list[dict[str, Any]] = []
    after: str | None = None
    for char in "call them":
        op = alice.insert("d1", "notes", after, char)
        text.append(op)
        after = op["charId"]
    ops = [
        alice.set("d1", "stage", "discovery"),
        bob.set("d1", "amount", 1000),
        tag,
        *text,
        alice.set("d1", "stage", "proposal"),
        bob.remove_tag("d1", "enterprise", [tag["instance"]]),
        alice.delete("d1", "notes", text[4]["charId"]),
        bob.insert("d1", "notes", text[4]["charId"], "!"),
        alice.set("d1", "stage", "won"),
        bob.add_tag("d1", "enterprise"),
    ]
    return compacted_case("mixed-registers-tags-and-text", ops)


# ───────────────────────────────────────────────────────────── generated shapes
def random_history(seed: int, length: int = 60) -> list[dict[str, Any]]:
    """A pseudo-random but reproducible op log across three authors and two entities.

    Weighted towards the interactions compaction has to survive rather than towards what a user
    typically does: deletes are frequent, anchors are chosen from ALL characters including deleted
    ones, and tag removals name a subset of what was observed.
    """
    rng = random.Random(seed)
    clock = Clock()
    authors = [Author(name, clock) for name in ("alice", "bob", "carol")]
    entities = ["d1", "d2"]
    chars: dict[str, list[str]] = {e: [] for e in entities}
    deleted: dict[str, set[str]] = {e: set() for e in entities}
    instances: dict[tuple[str, str], list[str]] = {}
    ops: list[dict[str, Any]] = []

    for _ in range(length):
        author = rng.choice(authors)
        entity = rng.choice(entities)
        action = rng.choices(
            ["set", "insert", "delete", "addTag", "removeTag"],
            weights=[3, 6, 4, 2, 2],
        )[0]

        if action == "set":
            field = rng.choice(["stage", "amount", "owner"])
            ops.append(
                author.set(entity, field, rng.randint(0, 9), concurrent=rng.random() < 0.25)
            )
        elif action == "insert":
            # Anchoring to a deleted character is the interesting case, so it is not excluded.
            after = rng.choice([None, *chars[entity]]) if chars[entity] else None
            op = author.insert(entity, "notes", after, rng.choice("abcdefg"))
            chars[entity].append(op["charId"])
            ops.append(op)
        elif action == "delete" and chars[entity]:
            target = rng.choice(chars[entity])
            deleted[entity].add(target)
            ops.append(author.delete(entity, "notes", target))
        elif action == "addTag":
            tag = rng.choice(["vip", "inbound"])
            op = author.add_tag(entity, tag)
            instances.setdefault((entity, tag), []).append(op["instance"])
            ops.append(op)
        elif action == "removeTag":
            tag = rng.choice(["vip", "inbound"])
            observed = instances.get((entity, tag), [])
            if observed:
                # A subset, because a replica names only what IT saw.
                cut = rng.randint(1, len(observed))
                ops.append(author.remove_tag(entity, tag, observed[:cut]))
    return ops


def jittered(ops: list[dict[str, Any]], seed: int, window: int = 6) -> list[dict[str, Any]]:
    """Re-order a history the way a lossy link does, without changing what is in it.

    THE LOG'S ORDER IS ARRIVAL ORDER, NOT CAUSAL ORDER, and a generator that always appends an
    add before the removal that cancels it can never produce the shape the OR-Set rule is most
    afraid of: a removal below the watermark whose add is still above it. A property test over
    perfectly ordered histories proved compaction safe for logs that do not exist -- deliberately
    dropping every removal below the watermark passed 200 of them.

    The window is small because that is what a real reordering looks like: a client's outbox
    flushing while a peer's frame is in flight, not a total shuffle.
    """
    rng = random.Random(seed * 31 + 7)
    out = list(ops)
    for i in range(len(out)):
        # Most reordering is local -- a peer's frame overtaking a client's flush by a few ops.
        # One in ten is not: a console that was offline posts its whole outbox at reconnect, and
        # an op queued minutes ago lands far down the log. That is the displacement that puts a
        # removal below the watermark while the add it cancels is still above it.
        reach = window if rng.random() < 0.9 else window * 5
        j = min(len(out) - 1, i + rng.randint(0, reach))
        if j != i and rng.random() < 0.5:
            out[i], out[j] = out[j], out[i]
    return out


def build_fixture() -> dict[str, Any]:
    cases = [
        _case_deleted_anchor_with_a_live_child(),
        _case_tombstone_chain(),
        _case_fully_deleted_run(),
        _case_delete_before_its_insert(),
        _case_watermark_splits_a_pair(),
        _case_mixed_entity(),
    ]
    for seed in (1, 7, 42, 1337):
        ops = random_history(seed)
        # Two watermarks per history: everything acknowledged, and two thirds acknowledged. The
        # second is the realistic one — a live workspace always has a tail nobody has confirmed.
        cases.append(compacted_case(f"random-{seed}-fully-acked", ops))
        cases.append(
            compacted_case(f"random-{seed}-partially-acked", ops, watermark=(len(ops) * 2) // 3)
        )
        # And the same history as it would actually have landed: cancellations overtaking the
        # things they cancel. See `jittered`.
        cases.append(compacted_case(f"random-{seed}-out-of-order", jittered(ops, seed)))
    return {
        "generated_by": "services/api/tests/compaction_fixture.py",
        "note": (
            "Produced by the real Python compactor. Each case holds an op log and the same log "
            "after compaction at the stated watermark; replaying both through the TypeScript "
            "replica must render identical text. Do not hand-edit — the value of this fixture "
            "is that it records what the compactor actually does."
        ),
        "cases": cases,
    }


def main() -> None:
    fixture = build_fixture()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # Explicit encoding, and no arrows in the summary below: this script is run on Windows too,
    # where the console codepage is cp1252 and a stray "->" drawn as an arrow aborts it after the
    # file has already been written.
    OUT.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(fixture['cases'])} cases to {OUT}")
    for case in fixture["cases"]:
        print(
            f"  {case['name']:<44} {len(case['full']):>3} ops -> {len(case['compacted']):>3}"
            f"  ({case['anchors_retained']} anchors retained)"
        )


if __name__ == "__main__":
    main()
