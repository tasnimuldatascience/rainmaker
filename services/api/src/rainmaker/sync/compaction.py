"""Pruning the op log without changing what it means.

THE SERVER STILL DOES NOT MERGE. `oplog.py` opens by saying so and this module does not walk it
back. Compaction does not materialise state, does not decide what a note says, and does not
write an op — it only DELETES ops that are provably redundant, and "provably" is doing real work
in that sentence. Everything left in the log is a byte-identical op that some client originated.

Why not the obvious design. The textbook answer is a snapshot: replay the log, write the
resulting state, throw the log away. That requires the server to implement the merge, and for
text it requires an RGA — a second sequence CRDT in a second language, which `crm/materialise.py`
declines to build and explains at length why. Two sequence CRDTs drift, and when they drift the
server's snapshot silently rewrites a customer's notes. Pruning needs none of that: the rules
below are about which ops are REDUNDANT, which is a question about the log, not about the text.

The three rules, each stated as the theorem it relies on.

  LWW REGISTER   For a given (kind, entity, field), `set` resolves to the HLC-maximum. A
                 non-maximal op cannot become the maximum by any future arrival, because a max
                 is unaffected by deleting non-maximal elements. Losers are dead weight.

  OR-SET         An `addTag` whose instance appears in some `removeTag`'s `instances` can never
                 be live on any replica: the client applies the cancellation whether or not the
                 add has arrived. Add and remove annihilate — but ONLY together. Dropping the
                 remove while any of the adds it names survives resurrects the tag, which is why
                 a remove is only collectable when every instance it names goes with it.

  RGA            A deleted character is invisible, so its insert and its tombstone can go — UNLESS
                 something still anchors to it. `after` is a character id, and a surviving
                 character whose anchor has been deleted from the log is an orphan: the replica
                 parks it in `orphans` forever and the text loses a character on that replica
                 only. So a deleted character that anchors a survivor is RETAINED as a tombstone
                 anchor, transitively — the ancestor of a survivor is a survivor.

  EXISTENCE      and a rule about the other three together, because each is correct and the
                 combination is not. An entity whose every op annihilates has no surviving op at
                 all, and an entity nothing mentions is an entity nobody created. One witness is
                 retained per entity and per text field — both halves of an annihilating pair,
                 which is exactly equivalent to keeping neither except that the thing still
                 exists.

And the rule that makes all four sound: NOTHING ABOVE THE WATERMARK IS EVER TOUCHED. See
`replicas.py` for why. Every rule below re-checks it rather than trusting the caller, because
this is the one module in the sync layer whose bugs delete data.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

# The HLC comparator is IMPORTED, not reimplemented. It is private to `materialise`, and reaching
# past the underscore is the lesser evil: a third copy of the total order is a third thing to
# drift, and this one decides which `set` op gets deleted. If the tiebreak here disagreed with
# the client's by even the actor rule, compaction would delete the winner and keep the loser.
from ..crm.materialise import _compare_hlc

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

    from .oplog import OpLog, StoredOp
    from .replicas import ReplicaRegistry

log = logging.getLogger("rainmaker.sync.compaction")

#: The rule names reported in `by_rule`. Spelled out as a constant so a report always has every
#: key, including the zeroes — a dashboard that plots "rga_insert" cannot plot a missing key, and
#: "the rule fired zero times" is the observation you most want on the run after a change.
RULES = ("lww_loser", "orset_add", "orset_remove", "rga_insert", "rga_delete")


@dataclass(frozen=True, slots=True)
class CompactionPlan:
    """What compaction would delete, and why. Pure: computed from ops, touching no storage."""

    watermark: int
    scanned: int
    #: Sequence numbers to delete, ascending.
    drop: tuple[int, ...]
    by_rule: dict[str, int]
    #: Deleted characters kept because a surviving character anchors to them. Not a failure —
    #: this is the number that says the anchor rule is doing something, and a log full of
    #: interleaved concurrent edits will have plenty.
    anchors_retained: int


@dataclass(frozen=True, slots=True)
class CompactionReport:
    """What compaction actually did. Shaped for a log line and a health endpoint."""

    workspace: str
    watermark: int
    scanned: int
    dropped: int
    by_rule: dict[str, int]
    anchors_retained: int
    head: int
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "watermark": self.watermark,
            "scanned": self.scanned,
            "dropped": self.dropped,
            "by_rule": dict(self.by_rule),
            "anchors_retained": self.anchors_retained,
            "head": self.head,
            "dry_run": self.dry_run,
        }


@dataclass(slots=True)
class _Text:
    """One (kind, entity, field) text field's ops, indexed by character."""

    inserts: dict[str, StoredOp] = field(default_factory=dict)
    deletes: dict[str, list[StoredOp]] = field(default_factory=lambda: defaultdict(list))


def plan_compaction(ops: Iterable[StoredOp], watermark: int) -> CompactionPlan:
    """Decide what is safe to delete at or below `watermark`.

    Separated from the deletion so it can be tested exhaustively without a database, and so
    `dry_run` is the same code path as the real thing rather than a second one that might not
    agree with it.
    """
    ops = list(ops)
    # seq -> the rule that proposed it. A dict rather than a set because the last step below
    # RETRACTS proposals, and a count incremented as each rule fired would then be a count of
    # what was proposed rather than of what was deleted.
    proposed: dict[int, str] = {}

    # ── pass one: index the WHOLE log, not just the part below the watermark ──────────────
    # A `set` op above the watermark can still make an op below it a loser, and a `removeTag`
    # above the watermark still cancels an add below it. Indexing only the compactable prefix
    # would keep ops that a later arrival has already made redundant — harmless, but it would
    # also make the LWW winner depend on where the watermark happened to fall, which is the kind
    # of "works until the watermark moves" behaviour that is impossible to reproduce in a test.
    lww_winner: dict[tuple[str, str, str], StoredOp] = {}
    adds: dict[tuple[str, str, str, str], list[StoredOp]] = defaultdict(list)
    cancelled: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    texts: dict[tuple[str, str, str], _Text] = defaultdict(_Text)

    for op in ops:
        payload = op.payload
        op_type = payload.get("type")
        entity = (str(payload.get("kind", "")), str(payload.get("entityId", "")))

        if op_type == "set":
            key = (*entity, str(payload.get("field", "")))
            current = lww_winner.get(key)
            # Strictly greater, so an exact HLC tie keeps the op with the LOWER sequence number.
            # That matches the replica, whose `applySet` rejects a non-greater timestamp and so
            # keeps whichever op it applied first — which, replaying the log, is the earlier seq.
            if current is None or _compare_hlc(_stamp(payload), _stamp(current.payload)) > 0:
                lww_winner[key] = op

        elif op_type == "addTag":
            adds[(*entity, str(payload.get("tag", "")), str(payload.get("instance", "")))].append(op)

        elif op_type == "removeTag":
            cancelled[(*entity, str(payload.get("tag", "")))].update(payload.get("instances") or [])

        elif op_type == "insertText":
            text = texts[(*entity, str(payload.get("field", "")))]
            # First insert wins on a duplicated character id, exactly as the replica does
            # (`applyInsert` returns early when the id is already present).
            text.inserts.setdefault(str(payload.get("charId", "")), op)

        elif op_type == "deleteText":
            text = texts[(*entity, str(payload.get("field", "")))]
            text.deletes[str(payload.get("charId", ""))].append(op)

    # ── rule one: superseded register writes ─────────────────────────────────────────────
    winning_seqs = {op.seq for op in lww_winner.values()}
    for op in ops:
        if op.payload.get("type") == "set" and op.seq <= watermark and op.seq not in winning_seqs:
            proposed[op.seq] = "lww_loser"

    # ── rule two: annihilated tag instances ──────────────────────────────────────────────
    # The add goes first, and the remove only goes if EVERY instance it names went with it.
    collected_instances: set[tuple[str, str, str, str]] = set()
    for key, add_ops in adds.items():
        kind, entity_id, tag, instance = key
        if instance not in cancelled.get((kind, entity_id, tag), frozenset()):
            continue
        if any(op.seq > watermark for op in add_ops):
            # Part of this instance's history is above the line. Leave the whole instance alone
            # rather than half-collecting it: the remove must stay too, and a remove that stays
            # while its add is gone is the state we are trying to avoid reasoning about.
            continue
        for op in add_ops:
            proposed[op.seq] = "orset_add"
        collected_instances.add(key)

    for op in ops:
        payload = op.payload
        if payload.get("type") != "removeTag" or op.seq > watermark:
            continue
        tag_key = (str(payload.get("kind", "")), str(payload.get("entityId", "")),
                   str(payload.get("tag", "")))
        # An instance with NO add op in the log is the dangerous case: the add may still be
        # sitting in some client's outbox and will arrive above the watermark, at which point the
        # cancellation has to be there to meet it. `pendingDeletes`' sibling problem, for tags.
        if all(
            (*tag_key, instance) in collected_instances
            for instance in (payload.get("instances") or [])
        ):
            proposed[op.seq] = "orset_remove"

    # ── rule three: deleted characters that anchor nothing ───────────────────────────────
    anchors_retained = 0
    for text in texts.values():
        collectable = {
            char_id
            for char_id, insert in text.inserts.items()
            if char_id in text.deletes
            and insert.seq <= watermark
            and all(d.seq <= watermark for d in text.deletes[char_id])
        }
        # Walk UP from every character that is staying. Seeding with the survivors' anchors and
        # following `after` is the whole anchor rule: a collectable character reached this way is
        # load-bearing and must remain as a tombstone, and so must ITS anchor, all the way to the
        # head of the document. Getting this wrong is not a lost tombstone, it is a live
        # character that never renders again on the one replica that parked it as an orphan.
        keep: set[str] = set()
        stack = [
            insert.payload.get("after")
            for char_id, insert in text.inserts.items()
            if char_id not in collectable
        ]
        while stack:
            parent = stack.pop()
            if parent is None or parent in keep or parent not in collectable:
                continue
            keep.add(parent)
            stack.append(text.inserts[parent].payload.get("after"))

        anchors_retained += len(keep)
        for char_id in sorted(collectable - keep):
            proposed[text.inserts[char_id].seq] = "rga_insert"
            for delete_op in text.deletes[char_id]:
                proposed[delete_op.seq] = "rga_delete"

    # ── the retraction: existence is state too ───────────────────────────────────────────
    _retain_a_witness(ops, proposed)

    # THE LAST GATE. Every rule above already checks the watermark; this checks them. The cost is
    # one comprehension per compaction and the benefit is that a future rule which forgets the
    # check deletes nothing rather than deleting a replica's history.
    above = [seq for seq in proposed if seq > watermark]
    if above:  # pragma: no cover - a bug in a rule, not a reachable state
        log.error("compaction rule proposed %d ops above the watermark; refusing", len(above))
        for seq in above:
            del proposed[seq]

    counts = dict.fromkeys(RULES, 0)
    for rule in proposed.values():
        counts[rule] += 1

    return CompactionPlan(
        watermark=watermark,
        scanned=len(ops),
        drop=tuple(sorted(proposed)),
        by_rule=counts,
        anchors_retained=anchors_retained,
    )


def _retain_a_witness(ops: list[StoredOp], proposed: dict[int, str]) -> None:
    """Never let an entity or a text field lose its LAST op.

    THE THREE RULES ARE EACH CORRECT AND TOGETHER THEY CAN ERASE A THING'S EXISTENCE. A deal whose
    entire history is one tag added and removed, or one note typed and then deleted, has no
    surviving op once every add annihilates its remove and every character is collected — and an
    entity nothing mentions is an entity that never existed. It stops being a deal with no tags
    and becomes a deal nobody created, which changes the pipeline board, the `/api/deals`
    response, and `Replica.list`. Existence is state, and deciding an entity away is a merge
    decision, which is not the server's to make.

    The fix is one op — or rather one UNIT, both halves of an annihilating pair, never one: a
    retained add without its removal resurrects the tag, and a retained insert without its
    tombstone makes a deleted character visible. Keeping both is exactly equivalent to keeping
    neither, except that the thing still exists.

    The cost is bounded and small: at most one character pair per text field and one tag pair per
    otherwise-empty entity, for the lifetime of the workspace.
    """
    by_field: dict[tuple[str, str, str], list[StoredOp]] = defaultdict(list)
    by_entity: dict[tuple[str, str], list[StoredOp]] = defaultdict(list)
    for op in ops:
        payload = op.payload
        entity = (str(payload.get("kind", "")), str(payload.get("entityId", "")))
        by_entity[entity].append(op)
        if payload.get("type") in ("insertText", "deleteText"):
            by_field[(*entity, str(payload.get("field", "")))].append(op)

    # Fields first. An entity whose only ops are text is rescued by its field, so the entity pass
    # then finds a survivor and does nothing more.
    for group in (*by_field.values(), *by_entity.values()):
        if any(op.seq not in proposed for op in group):
            continue
        for seq in _witness_unit(group):
            proposed.pop(seq, None)


def _witness_unit(group: list[StoredOp]) -> set[int]:
    """The cheapest set of ops that keeps `group`'s subject in existence without changing it.

    THE WITNESS MUST NOT BE AN ORPHAN. The obvious choice — the group's last op — is wrong for
    text: the last op is usually a delete of the last character typed, and retaining that
    character means retaining every character it anchors through, which for a note that was typed
    and then deleted is the entire note. Worse, retaining it WITHOUT its ancestors leaves an
    insert whose `after` is gone, which the replica parks in `orphans` forever — compaction
    creating exactly the orphan the anchor rule exists to prevent.

    So the preference order is by how self-contained the op is: a removal cancels something that
    is no longer there and needs nothing else; a character at the head of the document anchors to
    nothing. Only when a field has no head character at all does this fall back to walking up the
    anchor chain, which is the shape a log has when its first characters were never delivered.
    """
    removals = [op for op in group if op.payload.get("type") == "removeTag"]
    if removals:
        return {max(removals, key=lambda op: op.seq).seq}

    roots = [
        op
        for op in group
        if op.payload.get("type") == "insertText" and op.payload.get("after") is None
    ]
    if roots:
        return _char_unit(max(roots, key=lambda op: op.seq), group)

    last = max(group, key=lambda op: op.seq)
    op_type = last.payload.get("type")
    if op_type in ("insertText", "deleteText"):
        return _char_unit(last, group)
    if op_type == "addTag":
        # An add is only ever collected because something cancelled it, and that something has to
        # come back too or the tag returns.
        tag, instance = last.payload.get("tag"), last.payload.get("instance")
        return {last.seq} | {
            other.seq
            for other in group
            if other.payload.get("type") == "removeTag"
            and other.payload.get("tag") == tag
            and instance in (other.payload.get("instances") or [])
        }
    return {last.seq}


def _char_unit(op: StoredOp, group: list[StoredOp]) -> set[int]:
    """A character's insert and every tombstone for it, plus the same for each anchor above it.

    The walk stops at the head of the document or at an anchor the log never carried — the second
    being a character some client has yet to deliver, which the replica already parks and drains
    on arrival. It is bounded by the number of characters in the field and terminates on a cycle,
    which a well-formed RGA cannot contain but a corrupted log can.
    """
    field = op.payload.get("field")
    in_field = [o for o in group if o.payload.get("field") == field]
    inserts = {
        o.payload.get("charId"): o for o in in_field if o.payload.get("type") == "insertText"
    }

    unit: set[int] = set()
    seen: set[Any] = set()
    char_id = op.payload.get("charId")
    while char_id is not None and char_id not in seen:
        seen.add(char_id)
        unit.update(o.seq for o in in_field if o.payload.get("charId") == char_id)
        parent = inserts.get(char_id)
        char_id = parent.payload.get("after") if parent is not None else None
    return unit


def compact_workspace(
    oplog: OpLog,
    replicas: ReplicaRegistry,
    workspace: str,
    *,
    dry_run: bool = False,
) -> CompactionReport:
    """Compact one workspace up to what every live replica has acknowledged.

    The one-call entry point: the watermark is not a parameter a caller should be inventing, it
    is a fact about the registry, and the two arriving from different places is how a maintenance
    job ends up compacting to a number somebody typed.
    """
    return oplog.compact(workspace, replicas.watermark(workspace), dry_run=dry_run)


def _stamp(payload: dict[str, Any]) -> dict[str, Any]:
    """The op's HLC, with the actor filled in from the op if the timestamp omits it.

    The wire format carries the actor inside `ts`, but the op carries it too, and a client that
    sends only the outer one would otherwise KeyError inside the comparator — during a DELETE
    pass. Defaulting here keeps a malformed-but-orderable op orderable.
    """
    ts = payload.get("ts") or {}
    return {
        "wall": ts.get("wall", 0),
        "counter": ts.get("counter", 0),
        "actor": ts.get("actor", payload.get("actor", "")),
    }


def summarise(plans: Sequence[CompactionReport]) -> dict[str, Any]:
    """Roll several workspace reports into one line for a scheduled sweep."""
    return {
        "workspaces": len(plans),
        "scanned": sum(p.scanned for p in plans),
        "dropped": sum(p.dropped for p in plans),
        "by_rule": {rule: sum(p.by_rule.get(rule, 0) for p in plans) for rule in RULES},
        "anchors_retained": sum(p.anchors_retained for p in plans),
    }
