"""What each replica has acknowledged, and the watermark that follows from it.

THE OP LOG CANNOT BE PRUNED WITHOUT THIS TABLE. Compaction rewrites history: it drops ops that
some later op supersedes. That is only sound for ops every replica has already applied, because
a replica that never receives the loser and never receives the winner keeps whatever it decided
locally — forever, and only on that one machine.

The concrete failure, which is the one that motivated this module. Replica R originates an
insert at seq 11. Another replica deletes that character at seq 20. R has only acknowledged up
to seq 10, so it has seen neither op — but it has the character, because it typed it. Prune the
insert/delete pair as "a deleted character nobody needs" and R never learns the character was
deleted. Its note reads differently from everyone else's, no error is raised anywhere, and no
subsequent op can repair it because the evidence has been deleted from the log. That is
permanent, silent divergence produced by a maintenance job.

So the rule is: only prune at or below what EVERY replica has acknowledged. This module is the
"every replica" half of that sentence.

  ACK        a replica states the highest sequence number it holds. The console already sends
             this on every connect (`?since=`) and advances it from the `head` in each frame,
             so no client change is needed to start recording it.
  WATERMARK  the minimum acked sequence across the workspace's replicas. Compaction's ceiling.
  EVICTION   a replica that has not been seen for `horizon` seconds stops holding the watermark
             down. Without this one laptop that was reinstalled in March pins the log at its
             March sequence number and compaction never runs again — the failure mode of every
             min-based garbage collector.

Eviction is the one lossy part, and it is deliberate: an evicted replica that comes back may
find ops it never saw already pruned. Its recovery is a resync from zero, which is correct
because the compacted log still materialises to the same state — that is exactly the invariant
`tests/test_compaction.py` asserts. What it is NOT allowed to do is resume from its stale
checkpoint, so `ack` never moves a sequence number backwards: a replica asking for old ops is
told about the head it must catch up to, not quietly given a lower watermark that would let
compaction start deleting ops other replicas still need.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("rainmaker.sync.replicas")

#: How long a replica may be unheard-from before it stops holding the watermark down. Two weeks:
#: longer than any holiday a laptop is closed for, short enough that a decommissioned device does
#: not disable compaction for a quarter. A replica evicted in error pays a full resync, which is
#: cheap and correct; a replica evicted too eagerly while it still holds unsynced local edits
#: pays nothing at all, because its own edits are in ITS log and get appended on reconnect.
DEFAULT_HORIZON_SECONDS = int(os.environ.get("RAINMAKER_REPLICA_HORIZON", str(14 * 24 * 3600)))

SCHEMA = """
CREATE TABLE IF NOT EXISTS replica_acks (
    workspace  TEXT    NOT NULL,
    actor      TEXT    NOT NULL,
    acked_seq  INTEGER NOT NULL,
    last_seen  REAL    NOT NULL,
    seen_at    TEXT    NOT NULL,
    PRIMARY KEY (workspace, actor)
);
"""


@dataclass(frozen=True, slots=True)
class ReplicaAck:
    """One replica's position in the log, as of the last time it said anything."""

    workspace: str
    actor: str
    acked_seq: int
    last_seen: float
    seen_at: str
    #: True when `last_seen` is older than the horizon, so this row is excluded from the
    #: watermark. Reported rather than hidden: "why did compaction not run" is a question the
    #: health endpoint has to be able to answer, and the answer is usually a name in this list.
    evicted: bool = False


class ReplicaRegistry:
    """Per-(workspace, actor) acknowledgement positions.

    Deliberately keyed by ACTOR, not by connection. One person with a laptop and a phone is two
    actors and holds the watermark at the lower of the two, which is right — the phone's copy of
    the workspace is as real as the laptop's. Keying by connection id instead would make every
    reconnect a brand-new replica at seq 0 and the watermark would never leave zero.
    """

    def __init__(
        self,
        path: Path | str = ":memory:",
        horizon_seconds: int = DEFAULT_HORIZON_SECONDS,
        now: Callable[[], float] = time.time,
    ):
        self.path = str(path)
        self.horizon_seconds = horizon_seconds
        # Injected so eviction is testable without sleeping for two weeks. Everything else in
        # this module reads the clock through it.
        self._now = now
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # Same connection discipline as OpLog and Members: one connection, one lock, touched
        # from FastAPI's threadpool.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ writing
    def ack(self, workspace: str, actor: str, seq: int) -> int:
        """Record that `actor` holds everything up to `seq`. Returns the stored position.

        MONOTONIC, ALWAYS. A client that lost its IndexedDB reconnects with `since=0`, and
        taking that at face value would drag the watermark back to zero — which is not merely
        conservative, it is wrong in the other direction too: it would mean an op the log had
        already pruned is now "unacknowledged" by a replica that will never receive it, and the
        watermark would never recover past that point. A rewound client needs a full resync, and
        a full resync is what `since=0` already gives it.
        """
        stamp = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO replica_acks (workspace, actor, acked_seq, last_seen, seen_at)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(workspace, actor) DO UPDATE SET"
                "   acked_seq = MAX(acked_seq, excluded.acked_seq),"
                "   last_seen = excluded.last_seen,"
                "   seen_at   = excluded.seen_at",
                (workspace, actor, max(0, int(seq)), stamp, _iso(stamp)),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT acked_seq FROM replica_acks WHERE workspace = ? AND actor = ?",
                (workspace, actor),
            ).fetchone()
        return int(row["acked_seq"])

    def touch(self, workspace: str, actor: str) -> None:
        """Mark a replica alive without moving its position.

        A client can be connected and idle for hours. Liveness and progress are different facts
        and only `ack` may assert the second one; a heartbeat that also advanced the sequence
        would authorise pruning ops the client has not actually applied.
        """
        self.ack(workspace, actor, 0)

    def forget(self, workspace: str, actor: str) -> bool:
        """Drop a replica entirely. Called when membership is revoked.

        A revoked member will never acknowledge anything again, and leaving their row in place
        would freeze the watermark until the horizon expires. Revocation is a decision that the
        replica is gone; the registry should agree immediately.
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM replica_acks WHERE workspace = ? AND actor = ?", (workspace, actor)
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------ reading
    def replicas(self, workspace: str) -> list[ReplicaAck]:
        """Every replica the workspace has heard from, evicted ones included and flagged."""
        cutoff = self._now() - self.horizon_seconds
        with self._lock:
            rows = self._conn.execute(
                "SELECT workspace, actor, acked_seq, last_seen, seen_at FROM replica_acks"
                " WHERE workspace = ? ORDER BY actor",
                (workspace,),
            ).fetchall()
        return [
            ReplicaAck(
                workspace=row["workspace"],
                actor=row["actor"],
                acked_seq=int(row["acked_seq"]),
                last_seen=float(row["last_seen"]),
                seen_at=row["seen_at"],
                evicted=float(row["last_seen"]) < cutoff,
            )
            for row in rows
        ]

    def watermark(self, workspace: str) -> int:
        """The highest sequence number every live replica has acknowledged.

        ZERO WHEN THERE ARE NO LIVE REPLICAS, which means "compact nothing". The tempting
        alternative — treating an empty set as "everyone has seen everything" and returning the
        head — is the single most dangerous line this module could contain: a workspace whose
        replicas are all merely quiet would have its entire log collapsed to the winners, and the
        first replica to reconnect with a stale checkpoint would be told there is nothing new
        while holding ops nobody else has. An empty registry is ignorance, not consensus.

        It is also self-limiting in the only case that matters: ops only enter the log by way of
        a replica, and a replica that appends is a replica that acks, so a growing log always has
        someone holding a position in it.
        """
        live = [r for r in self.replicas(workspace) if not r.evicted]
        if not live:
            return 0
        return min(r.acked_seq for r in live)

    def describe(self, workspace: str) -> dict[str, object]:
        """Diagnostics for the health endpoint: the watermark and who is holding it there."""
        replicas = self.replicas(workspace)
        live = [r for r in replicas if not r.evicted]
        floor = min((r.acked_seq for r in live), default=0)
        return {
            "watermark": floor if live else 0,
            "replicas": [
                {
                    "actor": r.actor,
                    "acked_seq": r.acked_seq,
                    "seen_at": r.seen_at,
                    "evicted": r.evicted,
                    # The name a human wants when compaction is not progressing.
                    "holding_watermark": (not r.evicted) and r.acked_seq == floor,
                }
                for r in replicas
            ],
            "horizon_seconds": self.horizon_seconds,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _iso(stamp: float) -> str:
    """The human-readable half of `last_seen`.

    Both columns exist on purpose: the float is what eviction arithmetic uses, and the string is
    what somebody reading the table at 3am needs. Deriving one from the other at query time
    would put a strftime in the path that decides whether ops get deleted.
    """
    return datetime.fromtimestamp(stamp, UTC).isoformat()
