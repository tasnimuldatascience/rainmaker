"""An MCP server that writes what happened on the call into the pipeline.

    python -m rainmaker.mcp.servers.crm

WHY THE OP LOG AND NOT A TABLE. Everything a rep sees is materialised from an append-only log of
CRDT operations, and the console holds its own replica. If this server wrote to a `deals` table
the rep's laptop would never hear about it: the two views would diverge the moment the agent
touched a deal the rep also had open. Writing ops means an outcome recorded here reaches every
connected console the same way a rep's own edit does, and merges with edits made offline while
the call was happening.

WHICH IS THE POINT WORTH MAKING ABOUT MCP HERE. Exposing the CRM as a tool server is not a
wrapper over a database — it is the log, addressed through the protocol, so an agent that is not
Liv (a customer's own automation, another vendor's agent) can record an outcome without being
given a database handle.

A CALL IS ITS OWN ENTITY, NOT TEXT SMEARED INTO A DEAL'S NOTES. The first version of this
server appended the transcript into the deal's `notes` field, and that was wrong twice over.

Mechanically: notes are an RGA sequence, and an insert names the character it follows. To append
at the end you must know the last visible character id, which means running the RGA — and
`crm/materialise.py` deliberately does not implement it ("the right move is to run the
TypeScript replica server-side, not to port RGA into Python"). An insert whose `after` does not
exist is parked as an orphan by the console and never renders, so the transcript would have
vanished silently.

And modelling: a call has a start, an outcome and a transcript. It is a record, not a paragraph.
Making it an entity means a rep can see three calls on a deal instead of one note that grew.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from rainmaker.sync.oplog import OpLog

DATA_DIR = Path(os.environ.get("RAINMAKER_DATA", "data"))
WORKSPACE = os.environ.get("RAINMAKER_WORKSPACE", "demo")

#: The actor id every op from this server carries. A distinct one on purpose: a rep looking at a
#: deal should be able to tell what the agent changed from what a colleague changed, and the
#: CRDT needs a stable id to break concurrent-write ties consistently.
ACTOR = "liv-agent"

server = MCPServer(
    "rainmaker-crm",
    instructions=(
        "Record call outcomes into the sales pipeline. Writes are CRDT operations, so they "
        "merge with edits reps made offline rather than overwriting them."
    ),
)

_log: OpLog | None = None
_counter = 0


def _oplog() -> OpLog:
    global _log
    if _log is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _log = OpLog(DATA_DIR / "oplog.sqlite3")
        _log.ensure_workspace(WORKSPACE, "Demo workspace")
    return _log


def _stamp() -> dict[str, Any]:
    """A hybrid logical clock reading.

    Wall time alone is not enough: two ops written in the same millisecond need a total order,
    and the counter provides it.

    `actor` IS PART OF THE TIMESTAMP AND NOT OPTIONAL. Both `clock.ts::compare` and
    `crm/materialise.py::_compare_hlc` break a wall+counter tie on it, and the Python one indexes
    `a["actor"]` directly — omitting it does not merge badly, it raises `KeyError` the first time
    anything reads the deal back.
    """
    global _counter
    _counter += 1
    return {"wall": int(time.time() * 1000), "counter": _counter, "actor": ACTOR}


def _op(kind: str, entity_id: str, op_type: str, **fields: Any) -> dict[str, Any]:
    stamp = _stamp()
    return {
        # The console's format: `<hex wall>:<hex counter>:<actor>#<seq>`. Ids only have to be
        # unique for deduplication, but matching the shape means a log dumped from either side
        # sorts identically by eye, which is how the agreement tests get read.
        "id": f"{stamp['wall']:012x}:{stamp['counter']:08x}:{ACTOR}#{_counter}",
        "actor": ACTOR,
        "kind": kind,
        "entityId": entity_id,
        "type": op_type,
        "ts": stamp,
        **fields,
    }


def _append(ops: list[dict[str, Any]]) -> int:
    return len(_oplog().append(WORKSPACE, ops))


@server.tool(
    title="Record the outcome of a call",
    description=(
        "Write what happened on a call onto a deal: the stage it should move to, the outcome, "
        "and a one-line summary. Merges with anything a rep edited while the call ran."
    ),
)
def record_call_outcome(
    deal_id: str,
    outcome: str,
    stage: str = "",
    summary: str = "",
    company: str = "",
    contact_email: str = "",
) -> dict[str, Any]:
    """Args:
    deal_id: The deal this call was about.
    outcome: One of "meeting_booked", "not_a_fit", "handed_off", "no_decision".
    stage: Pipeline stage to move the deal to, if it should move.
    summary: One line a rep can read before their next call.
    company: The prospect's company, if the deal is new.
    contact_email: Who was on the call.
    """
    allowed = {"meeting_booked", "not_a_fit", "handed_off", "no_decision"}
    if outcome not in allowed:
        raise ValueError(f"outcome must be one of {sorted(allowed)}, got {outcome!r}")

    ops = [_op("deal", deal_id, "set", field="lastCallOutcome", value=outcome)]
    if stage:
        ops.append(_op("deal", deal_id, "set", field="stage", value=stage))
    if summary:
        ops.append(_op("deal", deal_id, "set", field="lastCallSummary", value=summary))
    if company:
        ops.append(_op("deal", deal_id, "set", field="company", value=company))
    if contact_email:
        ops.append(_op("deal", deal_id, "set", field="contactEmail", value=contact_email))
    ops.append(
        _op("deal", deal_id, "addTag", tag=f"outcome:{outcome}", instance=uuid.uuid4().hex[:12])
    )

    return {"deal_id": deal_id, "ops_written": _append(ops), "outcome": outcome}


@server.tool(
    title="Log a call against a deal",
    description=(
        "Record a call as its own entity linked to a deal: when it happened, what was said, "
        "and how it ended. Use this rather than writing into the deal's notes."
    ),
)
def log_call(
    deal_id: str,
    summary: str,
    transcript: str = "",
    outcome: str = "no_decision",
    duration_seconds: int = 0,
    contact_email: str = "",
    call_id: str = "",
) -> dict[str, Any]:
    """Args:
    deal_id: The deal this call belongs to.
    summary: One line a rep can read before their next call.
    transcript: The full exchange. Stored whole — a transcript is written once, not co-edited.
    outcome: How the call ended.
    duration_seconds: How long it ran.
    contact_email: Who was on it.
    call_id: Supply to update an existing record; omit to create one.
    """
    if not summary.strip():
        raise ValueError("a call record without a summary is a call nobody will read")
    # A transcript is a value, not a collaborative document, so it is one LWW field rather than
    # thousands of character operations. Ten minutes of speech is ~8KB; the cap is a guard
    # against a runaway loop writing a megabyte into every replica forever.
    if len(transcript) > 32_000:
        raise ValueError(f"transcript is {len(transcript)} characters; summarise it first")

    call_id = call_id or f"call_{uuid.uuid4().hex[:12]}"
    ops = [
        _op("call", call_id, "set", field="dealId", value=deal_id),
        _op("call", call_id, "set", field="summary", value=summary.strip()),
        _op("call", call_id, "set", field="outcome", value=outcome),
        _op("call", call_id, "set", field="endedAt", value=_now_iso()),
    ]
    if transcript:
        ops.append(_op("call", call_id, "set", field="transcript", value=transcript))
    if duration_seconds:
        ops.append(_op("call", call_id, "set", field="durationSeconds", value=duration_seconds))
    if contact_email:
        ops.append(_op("call", call_id, "set", field="contactEmail", value=contact_email))

    return {"call_id": call_id, "deal_id": deal_id, "ops_written": _append(ops)}


@server.tool(
    title="List calls on a deal",
    description="Every call recorded against a deal, most recent last.",
)
def list_calls(deal_id: str = "") -> dict[str, Any]:
    """Args:
    deal_id: Only calls on this deal. Omit for all of them.
    """
    from rainmaker.crm.materialise import materialise

    ops = [stored.payload for stored in _oplog().since(WORKSPACE, 0, limit=100_000)]
    calls = materialise(ops, kind="call")
    if deal_id:
        calls = [call for call in calls if call.get("dealId") == deal_id]
    calls.sort(key=lambda call: str(call.get("endedAt", "")))
    return {"calls": calls, "count": len(calls)}


@server.tool(
    title="Look up a deal",
    description="The current state of one deal, materialised from the log.",
)
def get_deal(deal_id: str) -> dict[str, Any]:
    """Args:
    deal_id: The deal to read.
    """
    from rainmaker.crm.materialise import materialise

    ops = [stored.payload for stored in _oplog().since(WORKSPACE, 0, limit=100_000)]
    for record in materialise(ops):
        if record.get("id") == deal_id:
            return {"found": True, "deal": record}
    return {"found": False, "deal_id": deal_id}


@server.tool(
    title="List deals in the pipeline",
    description="Every deal, materialised from the log. Read-only.",
)
def list_deals(stage: str = "") -> dict[str, Any]:
    """Args:
    stage: Only deals in this pipeline stage.
    """
    from rainmaker.crm.materialise import materialise

    ops = [stored.payload for stored in _oplog().since(WORKSPACE, 0, limit=100_000)]
    deals = materialise(ops)
    if stage:
        deals = [deal for deal in deals if deal.get("stage") == stage]
    return {"deals": deals, "count": len(deals)}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
