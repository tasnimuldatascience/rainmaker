"""The durable operation log.

THE SERVER DOES NOT MERGE. It appends, orders, and relays. All conflict resolution lives in
the CRDT on the clients, which is what makes this component small enough to be obviously
correct and what makes the system keep working when the server is unreachable.

What the server IS responsible for:

  DURABILITY      an op that was acknowledged is never lost. A rep who edits on a plane and
                  closes the laptop must find that edit on their phone.
  DEDUPLICATION   clients retry aggressively on a flaky link. The same op arriving five times
                  must be stored once. The CRDT would tolerate duplicates anyway, but storing
                  them makes the log grow without bound and makes replay slow.
  ORDERING        a global sequence number per workspace, so a client can say "give me
                  everything after 4,812" and resume in one round trip instead of
                  reconciling a set.
  AUTHORISATION   ops are attributed to an actor and scoped to a workspace. The CRDT has no
                  concept of permission; that boundary can only be enforced here.

SQLite, not Postgres. The op log is an append-only table with one index and no joins; the
bottleneck is fsync, not query planning. WAL mode gives concurrent readers alongside the
writer, which is the actual access pattern (many streaming clients, one append path). Moving
to Postgres later is a driver change, not a redesign, because nothing here uses SQLite
specifics.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("rainmaker.sync.oplog")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ops (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace   TEXT    NOT NULL,
    op_id       TEXT    NOT NULL,
    actor       TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    entity_id   TEXT    NOT NULL,
    op_type     TEXT    NOT NULL,
    hlc_wall    INTEGER NOT NULL,
    hlc_counter INTEGER NOT NULL,
    payload     TEXT    NOT NULL,
    received_at TEXT    NOT NULL
);

-- The dedup guarantee. A UNIQUE index rather than a check-then-insert: two concurrent
-- deliveries of the same retried op would both pass the check and both insert.
CREATE UNIQUE INDEX IF NOT EXISTS ops_workspace_opid ON ops (workspace, op_id);

-- The only query on the hot path: "everything in this workspace after seq N".
CREATE INDEX IF NOT EXISTS ops_workspace_seq ON ops (workspace, seq);

-- Entity-scoped replay, for opening a single deal without loading the workspace.
CREATE INDEX IF NOT EXISTS ops_entity ON ops (workspace, kind, entity_id, seq);

CREATE TABLE IF NOT EXISTS workspaces (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


@dataclass(slots=True, frozen=True)
class StoredOp:
    seq: int
    workspace: str
    op_id: str
    actor: str
    payload: dict[str, Any]

    def to_wire(self) -> dict[str, Any]:
        # `seq` is injected into the wire form so the client can checkpoint. It is server
        # state, not part of the op's identity, and is deliberately not fed to the CRDT.
        return {**self.payload, "_seq": self.seq}


#: Every column OpLog's insert reads out of an op. Kept beside the writer rather than in the
#: schema module because this is the list the validation pass has to agree with, and the two
#: drifting apart is how a "validated" op still raises on insert.
_REQUIRED = frozenset({"id", "actor", "kind", "entityId", "type", "ts"})


class OpLog:
    """Append-only op store. Thread-safe; one connection guarded by a lock."""

    def __init__(self, path: Path | str = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False plus an explicit lock: FastAPI runs sync handlers in a
        # threadpool, so the connection is legitimately touched from several threads. The
        # lock, not SQLite's own threading mode, is what serialises writes.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            # NORMAL, not FULL. FULL fsyncs on every commit and caps append throughput at the
            # disk's fsync rate; with WAL, NORMAL loses at most the last transaction on an OS
            # crash (not on a process crash), which is the right trade for a log whose clients
            # all retry unacknowledged ops anyway.
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ writing
    def append(self, workspace: str, ops: list[dict[str, Any]]) -> list[StoredOp]:
        """Append ops, skipping any already stored. Returns only the newly stored ones.

        Returning only the NEW ops is what makes the broadcast correct: relaying a duplicate
        to every connected client turns one client's retry storm into everyone's.
        """
        # VALIDATED IN FULL BEFORE ANYTHING IS WRITTEN. The insert loop used to raise partway
        # through on the first malformed op, which left the good ops before it committed and
        # still answered the client 422. That combination is the worst of both: the console does
        # `if (!res.ok) throw`, keeps the whole batch queued, and retries -- so the batch fails
        # forever on the same bad op while the good ones dedupe, the outbox never drains, and
        # nothing surfaces to the user because a queued write already looks successful to them.
        #
        # One poison op should reject its batch, not half-apply it.
        for op in ops:
            missing = _REQUIRED - op.keys()
            if missing:
                raise ValueError(f"malformed op, missing {sorted(missing)[0]!r}")
            if not isinstance(op.get("ts"), dict) or not {"wall", "counter"} <= op["ts"].keys():
                raise ValueError("malformed op, missing 'ts.wall' or 'ts.counter'")

        stored: list[StoredOp] = []
        now = datetime.now(UTC).isoformat()
        with self._lock:
            for op in ops:
                try:
                    cursor = self._conn.execute(
                        "INSERT INTO ops (workspace, op_id, actor, kind, entity_id, op_type,"
                        " hlc_wall, hlc_counter, payload, received_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            workspace,
                            op["id"],
                            op["actor"],
                            op["kind"],
                            op["entityId"],
                            op["type"],
                            int(op["ts"]["wall"]),
                            int(op["ts"]["counter"]),
                            json.dumps(op, separators=(",", ":")),
                            now,
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue          # already stored: a retry, which is the normal case
                except KeyError as exc:
                    raise ValueError(f"malformed op, missing {exc}") from exc
                stored.append(
                    StoredOp(
                        seq=int(cursor.lastrowid or 0),
                        workspace=workspace,
                        op_id=op["id"],
                        actor=op["actor"],
                        payload=op,
                    )
                )
            self._conn.commit()
        return stored

    # ------------------------------------------------------------------ reading
    def since(self, workspace: str, seq: int = 0, limit: int = 5000) -> list[StoredOp]:
        """Everything after `seq`, in order. The resume path."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, workspace, op_id, actor, payload FROM ops"
                " WHERE workspace = ? AND seq > ? ORDER BY seq LIMIT ?",
                (workspace, seq, limit),
            ).fetchall()
        return [_row_to_op(r) for r in rows]

    def for_entity(self, workspace: str, kind: str, entity_id: str) -> list[StoredOp]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, workspace, op_id, actor, payload FROM ops"
                " WHERE workspace = ? AND kind = ? AND entity_id = ? ORDER BY seq",
                (workspace, kind, entity_id),
            ).fetchall()
        return [_row_to_op(r) for r in rows]

    def head(self, workspace: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS head FROM ops WHERE workspace = ?",
                (workspace,),
            ).fetchone()
        return int(row["head"])

    def stats(self, workspace: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, COUNT(DISTINCT actor) AS actors,"
                " COUNT(DISTINCT entity_id) AS entities FROM ops WHERE workspace = ?",
                (workspace,),
            ).fetchone()
        return {
            "ops": int(row["n"]),
            "actors": int(row["actors"]),
            "entities": int(row["entities"]),
            "head": self.head(workspace),
        }

    def iter_all(self, workspace: str, batch: int = 1000) -> Iterator[list[StoredOp]]:
        """Stream the whole log in batches. Used by compaction and by export."""
        seq = 0
        while True:
            chunk = self.since(workspace, seq, limit=batch)
            if not chunk:
                return
            yield chunk
            seq = chunk[-1].seq

    # ------------------------------------------------------------------ workspaces
    def ensure_workspace(self, workspace_id: str, name: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO workspaces (id, name, created_at) VALUES (?,?,?)",
                (workspace_id, name or workspace_id, datetime.now(UTC).isoformat()),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row_to_op(row: sqlite3.Row) -> StoredOp:
    return StoredOp(
        seq=int(row["seq"]),
        workspace=row["workspace"],
        op_id=row["op_id"],
        actor=row["actor"],
        payload=json.loads(row["payload"]),
    )
