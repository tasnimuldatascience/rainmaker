"""Server-side materialisation of the op log into deal records.

A SECOND IMPLEMENTATION OF THE SAME SEMANTICS IS A LIABILITY, and it is worth being explicit
about why one exists anyway. The console has a full CRDT replica in TypeScript; this is a
Python reducer over the same ops. Two implementations of a merge rule will drift, and when
they drift the API and the UI disagree about a customer's pipeline.

Three things keep that from happening:

  1. This reducer implements only what the API needs — LWW fields and OR-Set tags. It does NOT
     re-implement RGA text. Notes are returned as a character count, not as merged content,
     because a second sequence CRDT is exactly where drift would be worst and the API has no
     use for the text.
  2. `tests/test_materialise.py` replays the same op log through both implementations and
     asserts the field values agree.
  3. Writes never come through here. This module is read-only; there is no path where the
     server's view becomes authoritative over a client's.

If the API ever needs merged note text, the right move is to run the TypeScript replica
server-side, not to port RGA into Python.
"""

from __future__ import annotations

from typing import Any


def _compare_hlc(a: dict[str, Any], b: dict[str, Any]) -> int:
    """Identical total order to `packages/crdt/src/clock.ts::compare`.

    Kept byte-for-byte equivalent in behaviour, including the actor tiebreak. Without the
    tiebreak two concurrent writes with the same wall and counter would resolve differently
    here than in the console, which is precisely the drift this file is trying not to cause.
    """
    if a["wall"] != b["wall"]:
        return a["wall"] - b["wall"]
    if a["counter"] != b["counter"]:
        return a["counter"] - b["counter"]
    return -1 if a["actor"] < b["actor"] else (1 if a["actor"] > b["actor"] else 0)


def materialise(ops: list[dict[str, Any]], kind: str = "deal") -> list[dict[str, Any]]:
    """Reduce an op log to current entity state.

    Order-independent: the input may arrive in any order and duplicates are tolerated, exactly
    as on the client. The function is pure, which is what makes the cross-implementation test
    possible.
    """
    fields: dict[str, dict[str, tuple[Any, dict[str, Any]]]] = {}
    tags: dict[str, dict[str, set[str]]] = {}
    removed: dict[str, dict[str, set[str]]] = {}
    text_chars: dict[str, dict[str, set[str]]] = {}
    text_deleted: dict[str, dict[str, set[str]]] = {}
    seen: set[str] = set()

    for op in ops:
        if op.get("kind") != kind:
            continue
        op_id = op.get("id")
        if op_id in seen:
            continue
        seen.add(op_id)

        entity = op["entityId"]
        op_type = op["type"]

        if op_type == "set":
            slot = fields.setdefault(entity, {})
            current = slot.get(op["field"])
            if current is None or _compare_hlc(op["ts"], current[1]) > 0:
                slot[op["field"]] = (op["value"], op["ts"])

        elif op_type == "addTag":
            # Check the cancellation set first -- a removal may have arrived before this add.
            if op["instance"] in removed.setdefault(entity, {}).setdefault(op["tag"], set()):
                continue
            tags.setdefault(entity, {}).setdefault(op["tag"], set()).add(op["instance"])

        elif op_type == "removeTag":
            cancelled = removed.setdefault(entity, {}).setdefault(op["tag"], set())
            cancelled.update(op.get("instances") or [])
            live = tags.setdefault(entity, {}).setdefault(op["tag"], set())
            live.difference_update(op.get("instances") or [])

        elif op_type == "insertText":
            chars = text_chars.setdefault(entity, {}).setdefault(op["field"], set())
            chars.add(op["charId"])

        elif op_type == "deleteText":
            # Recorded regardless of whether the insert has arrived, for the same reason the
            # client does: a delete that beats its insert must still count.
            text_deleted.setdefault(entity, {}).setdefault(op["field"], set()).add(op["charId"])

    entities = set(fields) | set(tags) | set(text_chars)
    out: list[dict[str, Any]] = []
    for entity in sorted(entities):
        record: dict[str, Any] = {"id": entity}
        for name, (value, _ts) in sorted(fields.get(entity, {}).items()):
            record[name] = value
        record["tags"] = sorted(
            tag for tag, instances in tags.get(entity, {}).items() if instances
        )
        # Length only. See the module docstring: the API has no use for merged note text and
        # porting RGA to get it would be the drift risk this design avoids.
        record["note_lengths"] = {
            field: len(chars - text_deleted.get(entity, {}).get(field, set()))
            for field, chars in text_chars.get(entity, {}).items()
        }
        out.append(record)
    return out


def pipeline_summary(deals: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate view: count and value by stage.

    Deals with no stage are counted under `unassigned` rather than dropped. Silently omitting
    them makes the totals disagree with the board, and a pipeline number that does not match
    what the rep can see is worse than no number.
    """
    by_stage: dict[str, dict[str, float]] = {}
    for deal in deals:
        stage = str(deal.get("stage") or "unassigned")
        bucket = by_stage.setdefault(stage, {"count": 0, "value": 0.0})
        bucket["count"] += 1
        try:
            bucket["value"] += float(deal.get("amount") or 0)
        except (TypeError, ValueError):
            pass
    return {
        "by_stage": by_stage,
        "total_count": sum(b["count"] for b in by_stage.values()),
        "total_value": round(sum(b["value"] for b in by_stage.values()), 2),
    }
