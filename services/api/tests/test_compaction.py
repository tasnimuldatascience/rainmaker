"""Op-log compaction: the watermark, the three rules, and the proof it loses nothing.

COMPACTION IS THE ONLY CODE IN THE SYNC LAYER THAT DELETES A CLIENT'S DATA. Everything else
fails loudly — a rejected op returns 422, a dropped subscriber reconnects, a malformed batch
raises. A compaction bug returns success, logs a cheerful count, and takes a character out of
somebody's note on one laptop. So the tests here are weighted accordingly: the rule tests state
what each rule may drop, and the property tests replay hundreds of randomised logs and assert
the materialised state is byte-identical before and after.

WHAT THIS FILE CANNOT PROVE, said plainly. `crm/materialise.py` has no RGA — by design, see its
docstring — so the assertions below reach note LENGTH and not note TEXT. Length is exactly the
property an anchor bug leaves intact: an orphaned character is still in the log, still applied,
still counted, and simply never rendered. `test_the_typescript_fixture_matches_the_compactor`
below is the seam that closes it: the fixture it checks is replayed through the real replica in
`packages/crdt/test/compaction.test.ts`, which is the only implementation that can render text.
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any

import pytest
from compaction_fixture import OUT as FIXTURE_PATH
from compaction_fixture import Author, Clock, build_fixture, jittered, random_history

from rainmaker.crm.materialise import materialise
from rainmaker.sync.compaction import compact_workspace, plan_compaction
from rainmaker.sync.oplog import OpLog
from rainmaker.sync.replicas import ReplicaRegistry


@pytest.fixture
def oplog() -> OpLog:
    return OpLog(":memory:")


@pytest.fixture
def clock() -> Clock:
    return Clock()


def stored(oplog: OpLog, workspace: str = "ws") -> list[dict[str, Any]]:
    """The workspace's log as plain ops, in sequence order."""
    return [op.payload for op in oplog.since(workspace, 0, limit=100_000)]


def op_ids(oplog: OpLog, workspace: str = "ws") -> list[str]:
    return [op["id"] for op in stored(oplog, workspace)]


def compact_all(oplog: OpLog, ops: list[dict[str, Any]], *, hold_back: int = 0):
    """Append `ops` and compact everything except the last `hold_back` of them.

    THE HELD-BACK TAIL IS NOT OPTIONAL PADDING. `OpLog.compact` never prunes the head op, so a
    watermark of exactly `len(ops)` silently becomes `len(ops) - 1` and the last op of every test
    ends up above the line. A test whose final op is the delete it is about would then be
    asserting the head guard rather than the rule it was written for.
    """
    oplog.append("ws", ops)
    return oplog.compact("ws", len(ops) - hold_back)


# ───────────────────────────────────────────────────────────── the watermark
class TestTheWatermarkIsThePositionEveryLiveReplicaHasPassed:
    @pytest.fixture
    def registry(self) -> ReplicaRegistry:
        return ReplicaRegistry(":memory:")

    def test_the_watermark_is_the_lowest_acknowledgement_not_the_highest(
        self, registry: ReplicaRegistry
    ):
        """The whole point. One replica being up to date says nothing about the others."""
        registry.ack("ws", "alice", 900)
        registry.ack("ws", "bob", 120)
        registry.ack("ws", "carol", 4000)
        assert registry.watermark("ws") == 120

    def test_a_workspace_nobody_has_acknowledged_compacts_nothing(
        self, registry: ReplicaRegistry
    ):
        """An empty registry is ignorance, not consensus.

        Reading "no replicas" as "everyone has seen everything" would collapse the log of a
        workspace whose users merely happen to be asleep, and the first one to open their laptop
        would hold ops that no longer exist anywhere else.
        """
        assert registry.watermark("ws") == 0

    def test_an_acknowledgement_never_moves_backwards(self, registry: ReplicaRegistry):
        """A client that lost its local database reconnects with `since=0`.

        Believing it would drag the watermark to zero and, worse, leave it there: the ops below
        the old watermark may already be pruned, so that replica can never acknowledge them
        again. A rewound client needs a full resync, which `since=0` already gives it.
        """
        registry.ack("ws", "alice", 500)
        assert registry.ack("ws", "alice", 0) == 500
        assert registry.watermark("ws") == 500

    def test_a_replica_unseen_past_the_horizon_stops_holding_the_watermark_down(self):
        """One laptop reinstalled in March must not pin the log at its March sequence number.

        This is the failure mode of every min-based collector, and it is silent: compaction keeps
        reporting success and dropping nothing, because the minimum is held by a machine that no
        longer exists.
        """
        now = [1_000_000.0]
        registry = ReplicaRegistry(":memory:", horizon_seconds=100, now=lambda: now[0])
        registry.ack("ws", "gone", 10)
        now[0] += 50
        registry.ack("ws", "here", 900)
        assert registry.watermark("ws") == 10

        now[0] += 60          # "gone" is now 110 seconds old, past the 100-second horizon
        assert registry.watermark("ws") == 900
        assert [r.actor for r in registry.replicas("ws") if r.evicted] == ["gone"]

    def test_a_revoked_replica_releases_the_watermark_immediately(
        self, registry: ReplicaRegistry
    ):
        """Revocation is a decision that the replica is gone; waiting out the horizon for a
        member who has been removed from the workspace is two weeks of not compacting for no
        reason."""
        registry.ack("ws", "alice", 900)
        registry.ack("ws", "leaver", 3)
        assert registry.forget("ws", "leaver") is True
        assert registry.watermark("ws") == 900

    def test_a_heartbeat_keeps_a_replica_alive_without_advancing_its_position(
        self, registry: ReplicaRegistry
    ):
        """Liveness and progress are different facts. A heartbeat that also advanced the sequence
        would authorise pruning ops the idle client has not applied."""
        registry.ack("ws", "alice", 40)
        registry.touch("ws", "alice")
        assert registry.watermark("ws") == 40
        registry.touch("ws", "newcomer")
        assert registry.watermark("ws") == 0

    def test_replicas_are_scoped_to_a_workspace(self, registry: ReplicaRegistry):
        registry.ack("ws1", "alice", 5)
        registry.ack("ws2", "alice", 900)
        assert registry.watermark("ws1") == 5
        assert registry.watermark("ws2") == 900

    def test_the_registry_names_which_replica_is_holding_the_watermark(
        self, registry: ReplicaRegistry
    ):
        """"Why has compaction stopped" is an operational question with a name for an answer."""
        registry.ack("ws", "alice", 900)
        registry.ack("ws", "slowpoke", 12)
        described = registry.describe("ws")
        assert described["watermark"] == 12
        holding = [r["actor"] for r in described["replicas"] if r["holding_watermark"]]
        assert holding == ["slowpoke"]

    def test_positions_survive_a_restart(self, tmp_path: Path):
        """The watermark is durable state. Forgetting it on restart would make every deploy a
        window in which the log is compactable to zero."""
        path = tmp_path / "replicas.sqlite3"
        first = ReplicaRegistry(path)
        first.ack("ws", "alice", 77)
        first.close()

        reopened = ReplicaRegistry(path)
        assert reopened.watermark("ws") == 77
        reopened.close()


# ───────────────────────────────────────────────────────────── the line itself
class TestNothingAboveTheWatermarkIsEverDropped:
    def test_a_superseded_write_above_the_watermark_survives(self, oplog: OpLog, clock: Clock):
        """Being a loser is not enough; being a loser everyone has already seen is.

        A replica that has not reached seq 2 still needs the op at seq 2 delivered, even though a
        later write supersedes it -- not for its value, which loses, but because the client
        advances its checkpoint over the ops it receives and cannot advance over a gap it is
        never told about.
        """
        alice = Author("alice", clock)
        ops = [
            alice.set("d1", "stage", "discovery"),
            alice.set("d1", "stage", "proposal"),
            alice.set("d1", "stage", "won"),
        ]
        oplog.append("ws", ops)
        report = oplog.compact("ws", 1)
        assert report.by_rule["lww_loser"] == 1
        assert op_ids(oplog) == [ops[1]["id"], ops[2]["id"]]

    def test_an_insert_and_its_tombstone_split_by_the_watermark_both_survive(
        self, oplog: OpLog, clock: Clock
    ):
        """The case that makes the watermark necessary rather than merely prudent.

        A replica sitting exactly on the watermark holds the character and has not yet heard it
        was deleted. Dropping the insert leaves it with a character no remaining op explains, and
        no future op can ever remove it -- the delete would have been the one, and compaction ate
        it.
        """
        alice = Author("alice", clock)
        anchor = alice.insert("d1", "notes", None, "a")
        doomed = alice.insert("d1", "notes", anchor["charId"], "b")
        ops = [anchor, doomed, alice.delete("d1", "notes", doomed["charId"])]
        oplog.append("ws", ops)
        report = oplog.compact("ws", 2)          # the delete, at seq 3, is above the line
        assert report.dropped == 0

    def test_the_head_op_is_never_pruned_so_the_reported_head_cannot_go_backwards(
        self, oplog: OpLog, clock: Clock
    ):
        """Clients checkpoint on `head` and resume with `since=head`.

        A log that shrinks at the top would tell a client that correctly acknowledged seq 3 that
        the workspace is at seq 2, and nothing downstream is written to survive that.
        """
        alice = Author("alice", clock)
        ops = [alice.set("d1", "stage", str(n)) for n in range(4)]
        oplog.append("ws", ops)
        head_before = oplog.head("ws")
        report = oplog.compact("ws", 4)          # "everything is acknowledged"
        assert report.watermark == 3             # capped at head - 1
        assert oplog.head("ws") == head_before
        assert op_ids(oplog)[-1] == ops[-1]["id"]

    def test_a_workspace_with_no_acknowledgements_is_left_entirely_alone(
        self, oplog: OpLog, clock: Clock
    ):
        alice = Author("alice", clock)
        ops = [alice.set("d1", "stage", "one"), alice.set("d1", "stage", "two")]
        oplog.append("ws", ops)
        registry = ReplicaRegistry(":memory:")
        report = compact_workspace(oplog, registry, "ws")
        assert report.watermark == 0
        assert report.dropped == 0


# ───────────────────────────────────────────────────────────── the three rules
class TestEachRuleDropsExactlyWhatItClaims:
    def test_a_superseded_register_write_goes_and_the_hlc_winner_stays(
        self, oplog: OpLog, clock: Clock
    ):
        alice = Author("alice", clock)
        losers = [alice.set("d1", "stage", "discovery"), alice.set("d1", "stage", "proposal")]
        winner = alice.set("d1", "stage", "won")
        report = compact_all(oplog, [*losers, winner], hold_back=0)
        assert report.by_rule["lww_loser"] == 2
        assert op_ids(oplog) == [winner["id"]]
        assert materialise(stored(oplog))[0]["stage"] == "won"

    def test_the_loser_is_chosen_by_the_clock_and_not_by_arrival_order(
        self, oplog: OpLog, clock: Clock
    ):
        """The log's order is arrival order, which on a flaky link is not causal order.

        Compacting by "last one in the log wins" would delete the op the replicas actually
        resolved to -- and the two would disagree from then on with no way to notice.
        """
        alice, bob = Author("alice", clock), Author("bob", clock)
        newer = bob.set("d1", "owner", "bob")
        older = alice.set("d1", "owner", "alice")
        # Backdated after minting, which is what a reconnecting client's queued op looks like:
        # later in the log, earlier on the clock.
        older["ts"] = {"wall": newer["ts"]["wall"] - 5000, "counter": 0, "actor": "alice"}
        compact_all(oplog, [newer, older, alice.set("d1", "stage", "won")], hold_back=1)
        assert older["id"] not in op_ids(oplog)
        assert newer["id"] in op_ids(oplog)

    def test_different_fields_and_entities_do_not_supersede_each_other(
        self, oplog: OpLog, clock: Clock
    ):
        """A register is keyed by (kind, entity, field). Collapsing on any coarser key would let
        a stage write delete an amount write, which is data loss dressed up as compaction."""
        alice = Author("alice", clock)
        ops = [
            alice.set("d1", "stage", "won"),
            alice.set("d1", "amount", 100),
            alice.set("d2", "stage", "lost"),
        ]
        report = compact_all(oplog, ops, hold_back=0)
        assert report.dropped == 0

    def test_a_cancelled_tag_instance_goes_together_with_the_removal_that_cancelled_it(
        self, oplog: OpLog, clock: Clock
    ):
        alice = Author("alice", clock)
        add = alice.add_tag("d1", "vip")
        remove = alice.remove_tag("d1", "vip", [add["instance"]])
        report = compact_all(oplog, [add, remove, alice.set("d1", "stage", "won")], hold_back=1)
        assert report.by_rule["orset_add"] == 1
        assert report.by_rule["orset_remove"] == 1
        assert materialise(stored(oplog))[0]["tags"] == []

    def test_a_removal_is_kept_while_any_add_it_names_is_kept(self, oplog: OpLog, clock: Clock):
        """Half-collecting an annihilating pair resurrects the tag.

        The remove is the only thing standing between a surviving add and a tag the user deleted,
        so it may only go when every instance it names goes with it.
        """
        alice = Author("alice", clock)
        early = alice.add_tag("d1", "vip")
        late = alice.add_tag("d1", "vip")
        remove = alice.remove_tag("d1", "vip", [early["instance"], late["instance"]])
        oplog.append("ws", [early, late, remove])
        # `late` is above the line, so it stays -- and so must the removal that cancels it.
        report = oplog.compact("ws", 1)
        assert remove["id"] in op_ids(oplog)
        assert late["id"] in op_ids(oplog)
        assert report.by_rule["orset_remove"] == 0

    def test_a_removal_naming_an_instance_the_log_has_never_seen_is_kept(
        self, oplog: OpLog, clock: Clock
    ):
        """The add may still be in some client's outbox and will arrive ABOVE the watermark.

        This is `pendingDeletes`' problem in tag form: a cancellation that arrives before the
        thing it cancels has to be durable, and dropping it because "there is nothing to cancel"
        resurrects the tag the moment the add lands.
        """
        alice = Author("alice", clock)
        remove = alice.remove_tag("d1", "vip", ["ghost:1"])
        report = compact_all(oplog, [remove, alice.set("d1", "stage", "won")], hold_back=1)
        assert report.by_rule["orset_remove"] == 0
        assert remove["id"] in op_ids(oplog)

    def test_a_deleted_character_that_anchors_nothing_goes_with_its_tombstone(
        self, oplog: OpLog, clock: Clock
    ):
        alice = Author("alice", clock)
        keeper = alice.insert("d1", "notes", None, "k")
        doomed = alice.insert("d1", "notes", None, "x")
        ops = [keeper, doomed, alice.delete("d1", "notes", doomed["charId"]),
               alice.set("d1", "stage", "won")]
        report = compact_all(oplog, ops, hold_back=1)
        assert report.by_rule["rga_insert"] == 1
        assert report.by_rule["rga_delete"] == 1
        assert op_ids(oplog) == [keeper["id"], ops[3]["id"]]

    def test_a_deleted_character_that_a_live_character_anchors_to_is_kept_as_a_tombstone(
        self, oplog: OpLog, clock: Clock
    ):
        """THE REGRESSION TEST FOR THE RULE THAT IS EASY TO GET WRONG.

        Alice types "ab" and deletes "b"; Bob, who had not yet seen the delete, types "X" after
        "b". "b" is invisible, so collecting it looks free -- and it is not, because "X" names it
        as its anchor. Delete the insert of "b" and every replica that receives "X" afterwards
        parks it in `orphans` and never renders it. The note is one character shorter on those
        replicas only, no error is raised, and the log no longer contains the evidence.

        The count of characters is unchanged either way, which is why this cannot be checked
        through `materialise`: see `packages/crdt/test/compaction.test.ts`, which renders it.
        """
        alice, bob = Author("alice", clock), Author("bob", clock)
        a = alice.insert("d1", "notes", None, "a")
        b = alice.insert("d1", "notes", a["charId"], "b")
        x = bob.insert("d1", "notes", b["charId"], "X")
        delete_b = alice.delete("d1", "notes", b["charId"])
        report = compact_all(oplog, [a, b, x, delete_b, alice.set("d1", "stage", "won")],
                             hold_back=1)

        assert report.dropped == 0
        assert report.anchors_retained == 1
        assert b["id"] in op_ids(oplog), "the tombstone anchor was collected; X is now an orphan"
        assert delete_b["id"] in op_ids(oplog), (
            "the anchor was kept but its tombstone was not, so a deleted character is visible"
        )

    def test_a_whole_chain_of_tombstones_under_a_live_character_is_kept(
        self, oplog: OpLog, clock: Clock
    ):
        """Retention has to be transitive.

        Keeping only the immediate parent orphans THAT parent instead -- the bug moves one
        character to the left rather than going away. The ancestor of a survivor is a survivor,
        all the way to the head of the document.
        """
        alice, bob = Author("alice", clock), Author("bob", clock)
        chain, after = [], None
        for char in "abc":
            op = alice.insert("d1", "notes", after, char)
            chain.append(op)
            after = op["charId"]
        tail = bob.insert("d1", "notes", after, "Z")
        deletes = [alice.delete("d1", "notes", op["charId"]) for op in chain]
        report = compact_all(
            oplog, [*chain, tail, *deletes, alice.set("d1", "stage", "won")], hold_back=1
        )
        assert report.dropped == 0
        assert report.anchors_retained == 3

    def test_a_run_of_deleted_characters_under_nothing_live_goes_entirely(
        self, oplog: OpLog, clock: Clock
    ):
        """The counterweight. A compactor that retains every tombstone is trivially correct and
        completely useless, so a chain that anchors nothing must actually disappear."""
        alice = Author("alice", clock)
        # One surviving character in the same field, so the field is not emptied and the witness
        # rule below has nothing to do here. Without it this test would be measuring that rule.
        survivor = alice.insert("d1", "notes", None, "K")
        chain, after = [], None
        for char in "scratch":
            op = alice.insert("d1", "notes", after, char)
            chain.append(op)
            after = op["charId"]
        deletes = [alice.delete("d1", "notes", op["charId"]) for op in chain]
        report = compact_all(
            oplog, [survivor, *chain, *deletes, alice.set("d1", "stage", "won")], hold_back=1
        )
        assert report.by_rule["rga_insert"] == 7
        assert report.by_rule["rga_delete"] == 7
        assert report.anchors_retained == 0
        assert survivor["id"] in op_ids(oplog)

    def test_a_tombstone_whose_insert_has_not_arrived_is_never_dropped(
        self, oplog: OpLog, clock: Clock
    ):
        """A delete that overtook its insert. The insert is still on its way, above the
        watermark, and the tombstone has to be there to meet it."""
        alice = Author("alice", clock)
        orphan_delete = alice.delete("d1", "notes", "nobody:1")
        report = compact_all(oplog, [orphan_delete, alice.set("d1", "stage", "won")],
                             hold_back=1)
        assert report.dropped == 0

    def test_text_fields_are_compacted_independently(self, oplog: OpLog, clock: Clock):
        """Character ids are only unique per replica, not per field. Pooling two fields'
        characters would let a live character in one note retain a tombstone in another -- or,
        far worse, let a delete in one field collect an insert in the other."""
        alice = Author("alice", clock)
        notes = alice.insert("d1", "notes", None, "n")
        # The SAME character id in a different field. Pooling the two would let the delete below
        # collect the summary's character as well.
        summary = alice.insert("d1", "summary", None, "s", char_id=notes["charId"])
        survivor = alice.insert("d1", "notes", None, "N")
        ops = [notes, summary, survivor, alice.delete("d1", "notes", notes["charId"]),
               alice.set("d1", "stage", "won")]
        report = compact_all(oplog, ops, hold_back=1)
        assert summary["id"] in op_ids(oplog)
        assert notes["id"] not in op_ids(oplog)
        assert report.by_rule["rga_insert"] == 1


# ───────────────────────────────────────────────────────────── existence
class TestCompactionCannotDeleteAThingOutOfExistence:
    """The three rules are each correct and together they can empty an entity completely.

    A deal whose whole history is one tag added and removed has no surviving op once the pair
    annihilates -- and an entity nothing mentions is an entity nobody created. It stops appearing
    on the pipeline board, in `/api/deals`, and in `Replica.list`. Whether a deal exists is not a
    conflict for the server to resolve, so compaction keeps one witness rather than deciding.
    """

    def test_an_entity_whose_every_op_annihilates_still_exists_afterwards(
        self, oplog: OpLog, clock: Clock
    ):
        alice = Author("alice", clock)
        add = alice.add_tag("d1", "vip")
        remove = alice.remove_tag("d1", "vip", [add["instance"]])
        # A second entity so the log has a head op that is not part of the pair under test.
        compact_all(oplog, [add, remove, alice.set("d2", "stage", "won")], hold_back=1)

        assert [d["id"] for d in materialise(stored(oplog))] == ["d1", "d2"]
        assert materialise(stored(oplog))[0]["tags"] == []
        assert remove["id"] in op_ids(oplog), "a removal alone is inert, and the cheapest witness"
        assert add["id"] not in op_ids(oplog), "keeping the add as well would resurrect the tag"

    def test_a_note_typed_and_entirely_deleted_keeps_one_tombstone_pair(
        self, oplog: OpLog, clock: Clock
    ):
        alice = Author("alice", clock)
        chain, after = [], None
        for char in "gone":
            op = alice.insert("d1", "notes", after, char)
            chain.append(op)
            after = op["charId"]
        deletes = [alice.delete("d1", "notes", op["charId"]) for op in chain]
        report = compact_all(
            oplog, [*chain, *deletes, alice.set("d1", "stage", "won")], hold_back=1
        )
        # Three of the four characters go; the head of the document stays with its tombstone.
        assert report.by_rule["rga_insert"] == 3
        assert report.by_rule["rga_delete"] == 3
        assert materialise(stored(oplog))[0]["note_lengths"] == {"notes": 0}

    def test_the_witness_is_the_head_of_the_document_and_not_the_last_character_typed(
        self, oplog: OpLog, clock: Clock
    ):
        """RETAINING THE LAST CHARACTER WOULD RETAIN THE WHOLE NOTE, or orphan the witness.

        The last character anchors to the one before it, which anchors to the one before that.
        Keeping it means keeping the chain -- which collects nothing -- and keeping it WITHOUT the
        chain leaves an insert whose `after` is gone, which the replica parks in `orphans`
        forever. That is compaction manufacturing the exact orphan the anchor rule exists to
        prevent, so the witness is taken from the head of the document instead.
        """
        alice = Author("alice", clock)
        chain, after = [], None
        for char in "abcdef":
            op = alice.insert("d1", "notes", after, char)
            chain.append(op)
            after = op["charId"]
        deletes = [alice.delete("d1", "notes", op["charId"]) for op in chain]
        compact_all(oplog, [*chain, *deletes, alice.set("d1", "stage", "won")], hold_back=1)

        survivors = op_ids(oplog)
        assert chain[0]["id"] in survivors
        assert deletes[0]["id"] in survivors
        assert chain[-1]["id"] not in survivors
        # And every retained insert still has the anchor it names.
        kept = {op["charId"] for op in stored(oplog) if op["type"] == "insertText"}
        anchors = {op["after"] for op in stored(oplog) if op["type"] == "insertText"}
        assert anchors - kept == {None}

    def test_an_entity_that_keeps_a_surviving_op_gets_no_witness(
        self, oplog: OpLog, clock: Clock
    ):
        """The rule is a floor, not a quota. An entity with a surviving `set` needs nothing."""
        alice = Author("alice", clock)
        add = alice.add_tag("d1", "vip")
        remove = alice.remove_tag("d1", "vip", [add["instance"]])
        report = compact_all(
            oplog, [alice.set("d1", "stage", "won"), add, remove, alice.set("d2", "x", 1)],
            hold_back=1,
        )
        assert report.by_rule["orset_add"] == 1
        assert report.by_rule["orset_remove"] == 1


# ───────────────────────────────────────────────────────────── reporting
class TestCompactionSaysWhatItDid:
    def test_a_dry_run_reports_the_same_plan_without_deleting_anything(
        self, oplog: OpLog, clock: Clock
    ):
        """The plan and the deletion are the same code path, so a dry run is a real answer.

        Anyone turning this on for the first time on a log they care about should be able to see
        the number before it happens.
        """
        alice = Author("alice", clock)
        ops = [alice.set("d1", "stage", "one"), alice.set("d1", "stage", "two"),
               alice.set("d1", "stage", "three")]
        oplog.append("ws", ops)
        dry = oplog.compact("ws", 3, dry_run=True)
        assert dry.dry_run is True
        assert dry.dropped == 2
        assert len(op_ids(oplog)) == 3

        wet = oplog.compact("ws", 3)
        assert wet.dropped == dry.dropped
        assert wet.by_rule == dry.by_rule

    def test_the_report_carries_every_rule_name_including_the_ones_that_did_nothing(
        self, oplog: OpLog, clock: Clock
    ):
        """A missing key is not a zero anywhere that plots it, and "this rule fired zero times"
        is the observation you most want on the run after a change."""
        alice = Author("alice", clock)
        report = compact_all(oplog, [alice.set("d1", "stage", "one")], hold_back=0)
        assert set(report.by_rule) == {
            "lww_loser", "orset_add", "orset_remove", "rga_insert", "rga_delete"
        }
        assert report.to_dict()["head"] == 1

    def test_compact_workspace_takes_the_watermark_from_the_registry(
        self, oplog: OpLog, clock: Clock
    ):
        """The watermark is a fact about the registry, not a number a caller invents. The two
        arriving from different places is how a maintenance job compacts to something nobody
        acknowledged."""
        alice = Author("alice", clock)
        ops = [alice.set("d1", "stage", str(n)) for n in range(5)]
        oplog.append("ws", ops)
        registry = ReplicaRegistry(":memory:")
        registry.ack("ws", "alice", 5)
        registry.ack("ws", "bob", 2)
        report = compact_workspace(oplog, registry, "ws")
        assert report.watermark == 2
        assert report.dropped == 2


# ───────────────────────────────────────────────────────────── the property
class TestCompactionPreservesTheStateTheLogMaterialisesTo:
    """Hundreds of randomised logs, compacted at a randomised watermark, materialised twice.

    Hand-picked cases cover the interleavings the author already thought of, which is the wrong
    coverage for a rule about which ops are redundant: the dangerous inputs are the ones where a
    tag removal names half of what it saw, or a character is anchored to one that four other ops
    later delete. Those get generated.
    """

    @staticmethod
    def _both_ways(seed: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
        rng = random.Random(seed)
        ops = random_history(seed, length=rng.randint(20, 80))
        # HALF THE HISTORIES ARRIVE OUT OF ORDER, which is the only way this property sees a
        # removal sitting below the watermark while the add it cancels is above. Generating in
        # causal order made that shape impossible: dropping every removal below the watermark --
        # a rule that resurrects deleted tags -- passed 200 in-order seeds without a murmur.
        if seed % 2:
            ops = jittered(ops, seed)
        oplog = OpLog(":memory:")
        oplog.append("ws", ops)
        full = [op.payload for op in oplog.since("ws", 0, limit=100_000)]
        # A watermark anywhere in the log, not one of four tidy fractions. Where the line falls
        # decides which halves of which pairs are eligible, so it is the most important thing to
        # vary -- and the shapes that break a rule straddle it by one op.
        watermark = rng.randint(0, len(ops))
        report = oplog.compact("ws", watermark)
        compacted = [op.payload for op in oplog.since("ws", 0, limit=100_000)]
        oplog.close()
        return full, compacted, report.dropped, watermark

    def test_two_hundred_random_logs_materialise_identically_after_compaction(self):
        total_dropped = 0
        for seed in range(200):
            full, compacted, dropped, watermark = self._both_ways(seed)
            total_dropped += dropped
            assert materialise(compacted) == materialise(full), (
                f"seed {seed} at watermark {watermark}: compaction changed the materialised "
                f"state, dropping {dropped} of {len(full)} ops"
            )
        # A compactor that returns its input passes the assertion above on every seed. This is
        # the line that says the property was not vacuous.
        assert total_dropped > 1000, f"only {total_dropped} ops dropped across 200 logs"

    def test_no_surviving_insert_is_left_without_the_anchor_it_names(self):
        """The RGA invariant, checked structurally because Python cannot check it by rendering.

        If a surviving insert names an anchor that the full log had and the compacted log does
        not, the replica parks that insert as an orphan forever. This is the assertion that would
        have caught a non-transitive retention walk; the rendered-text version lives in
        `packages/crdt/test/compaction.test.ts`.
        """
        for seed in range(200):
            full, compacted, _dropped, watermark = self._both_ways(seed)
            before = _chars_by_field(full)
            after = _chars_by_field(compacted)
            for field_key, chars in after.items():
                for char_id, anchor in chars.items():
                    if anchor is None or anchor not in before[field_key]:
                        # An anchor the full log never had is an orphan the CRDT already handles;
                        # compaction is only answerable for anchors it removed itself.
                        continue
                    assert anchor in chars, (
                        f"seed {seed} at watermark {watermark}: {char_id} in {field_key} "
                        f"anchors to {anchor}, which compaction collected"
                    )

    def test_compacting_twice_at_the_same_watermark_drops_nothing_the_second_time(self):
        """Idempotence, and a cheap check that no rule depends on ops another rule removed.

        A second pass that keeps finding work means the rules disagree with each other, and a
        scheduled job would grind the log down a little further every hour.
        """
        for seed in range(40):
            ops = jittered(random_history(seed, length=50), seed)
            oplog = OpLog(":memory:")
            oplog.append("ws", ops)
            first = oplog.compact("ws", len(ops))
            second = oplog.compact("ws", len(ops))
            oplog.close()
            assert second.dropped == 0, (
                f"seed {seed}: a second pass dropped {second.dropped} more ops after the first "
                f"dropped {first.dropped}"
            )

    def test_the_plan_never_names_a_sequence_above_the_watermark(self):
        """Stated as its own property because it is the one mistake that cannot be undone.

        Every rule checks the watermark itself; this asserts the conjunction of them, over inputs
        nobody chose.
        """
        for seed in range(100):
            ops = jittered(random_history(seed, length=50), seed)
            oplog = OpLog(":memory:")
            oplog.append("ws", ops)
            for watermark in (0, 5, len(ops) // 2, len(ops)):
                plan = plan_compaction(oplog.since("ws", 0, limit=100_000), watermark)
                assert all(seq <= watermark for seq in plan.drop), (
                    f"seed {seed}: plan at watermark {watermark} names {plan.drop}"
                )
            oplog.close()


def _chars_by_field(ops: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, str | None]]:
    """charId -> anchor, per (kind, entity, field). The shape the anchor rule reasons about."""
    fields: dict[tuple[str, str, str], dict[str, str | None]] = {}
    for op in ops:
        if op.get("type") != "insertText":
            continue
        key = (op["kind"], op["entityId"], op["field"])
        fields.setdefault(key, {}).setdefault(op["charId"], op.get("after"))
    return fields


# ───────────────────────────────────────────────────────────── cross-implementation
class TestTheTypeScriptSideCanStillCheckTheTextItRenders:
    """The Python suite proves compaction preserves fields, tags and character counts.

    It cannot prove it preserves the text, and the text is where the expensive bug is. The seam
    is a fixture generated by the real compactor and replayed through the real replica in
    `packages/crdt/test/compaction.test.ts` — the mirror image of `crdt_agreement.json`, which
    goes the other way.
    """

    def test_the_committed_fixture_matches_what_the_compactor_produces_today(self):
        """A generated fixture that is not regenerated is a comment.

        If a compaction rule changes and this file is not rebuilt, the TypeScript side keeps
        asserting agreement with a compactor that no longer exists. Regenerate with
        `python services/api/tests/compaction_fixture.py`.
        """
        assert FIXTURE_PATH.exists(), (
            "the compaction fixture is missing; run "
            "`python services/api/tests/compaction_fixture.py`"
        )
        committed = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        assert committed == build_fixture(), (
            "the committed compaction fixture disagrees with the compactor. Regenerate it with "
            "`python services/api/tests/compaction_fixture.py` and commit the result."
        )

    def test_the_fixture_carries_the_anchor_case_the_typescript_test_looks_up_by_name(self):
        """The TypeScript test finds that case BY NAME. Renaming it here turns the regression
        test over there into a silently skipped lookup."""
        committed = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        names = {case["name"] for case in committed["cases"]}
        assert "deleted-anchor-with-a-live-child" in names
        assert any(case["dropped"] > 0 for case in committed["cases"]), (
            "no case in the fixture drops anything, so the TypeScript comparison is vacuous"
        )


# ───────────────────────────────────────────────────────────── durability
class TestCompactionSurvivesTheThingsTheRelayDoesAroundIt:
    def test_a_compacted_log_still_resumes_from_a_checkpoint(self, oplog: OpLog, clock: Clock):
        """`since` is the whole client protocol, and it is a range scan over sequence numbers.

        Deleting rows leaves gaps, which is fine -- the client asks for "everything after N", not
        "the next N ops" -- but it is worth asserting, because a compactor that renumbered rows to
        close those gaps would break every checkpoint in existence.
        """
        alice = Author("alice", clock)
        ops = [alice.set("d1", "stage", str(n)) for n in range(6)]
        oplog.append("ws", ops)
        oplog.compact("ws", 4)
        resumed = oplog.since("ws", 3)
        assert [op.seq for op in resumed] == [5, 6]
        assert oplog.head("ws") == 6

    def test_a_compacted_workspace_does_not_disturb_its_neighbours(
        self, oplog: OpLog, clock: Clock
    ):
        alice = Author("alice", clock)
        theirs = [alice.set("d1", "stage", "one"), alice.set("d1", "stage", "two")]
        oplog.append("ws1", [alice.set("d1", "stage", "a"), alice.set("d1", "stage", "b")])
        oplog.append("ws2", theirs)
        oplog.compact("ws1", 10)
        assert len(oplog.since("ws2", 0)) == 2

    def test_compaction_persists(self, tmp_path: Path, clock: Clock):
        """The deletion is committed, not merely applied to an in-memory connection."""
        alice = Author("alice", clock)
        path = tmp_path / "log.sqlite3"
        first = OpLog(path)
        first.append("ws", [alice.set("d1", "stage", str(n)) for n in range(4)])
        first.compact("ws", 4)
        surviving = len(first.since("ws", 0))
        first.close()

        reopened = OpLog(path)
        assert len(reopened.since("ws", 0)) == surviving == 1
        reopened.close()

    def test_an_empty_workspace_compacts_to_nothing_without_raising(self, oplog: OpLog):
        """`head` is zero, so the watermark caps to -1. Arithmetic on an empty log is exactly the
        kind of edge a scheduled job hits at 3am on a workspace nobody used."""
        report = oplog.compact("ws", 100)
        assert report.dropped == 0
        assert report.watermark == -1

    def test_compaction_is_not_slow_enough_to_block_the_relay(self, oplog: OpLog):
        """Not a benchmark -- a smoke alarm.

        The plan is a few linear passes and a walk up the anchor tree, so a log of this size
        should be milliseconds. If this ever fails it is because a rule became quadratic, which on
        a real workspace means the append path stalls behind it.
        """
        ops: list[dict[str, Any]] = []
        for seed in range(40):
            ops.extend(random_history(seed, length=50))
        # Each history mints ids from its own clock, so forty of them collide. Renumbering is
        # cheaper than generating one enormous history and exercises the same code.
        for index, op in enumerate(ops):
            op["id"] = f"{op['id']}#{index}"
        oplog.append("ws", ops)

        started = time.perf_counter()
        report = oplog.compact("ws", len(ops))
        elapsed = time.perf_counter() - started
        assert elapsed < 5.0, f"compacting {len(ops)} ops took {elapsed:.1f}s"
        assert report.scanned == len(ops)
