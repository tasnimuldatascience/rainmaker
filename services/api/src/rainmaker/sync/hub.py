"""Live relay: fan ops out to every connected replica in a workspace.

THE CONTRACT WITH THE CLIENT. A client connects, states the last sequence number it has, and
receives (a) everything it missed, then (b) everything new, until it disconnects. That is the
entire protocol. It is deliberately not a subscription language, a diff protocol, or a
presence system — a bigger protocol is a bigger surface for the two sides to disagree about
state, and disagreement here means a rep's pipeline is silently wrong.

WHY A SEQUENCE NUMBER AND NOT A VECTOR CLOCK. The CRDT already handles concurrency; the
server only needs to answer "what have I not seen". A monotonic per-workspace counter answers
that in one integer and one indexed range scan. A vector clock would grow with the number of
devices and would have to be reconciled on every reconnect, buying nothing the CRDT does not
already provide.

BACKPRESSURE IS REAL AND IS HANDLED. A client on a bad connection cannot keep up with a busy
workspace. Its queue is bounded; when it overflows the connection is closed with a resync
signal rather than buffered indefinitely. Unbounded per-client buffers are how one slow phone
takes down a server, and reconnect-and-replay is cheap here precisely because the log is
sequenced.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .oplog import OpLog, StoredOp

log = logging.getLogger("rainmaker.sync.hub")

# How many pending messages a single client may fall behind before it is disconnected.
# Sized so a client that stalls for a few seconds in a busy workspace survives, but one that
# has effectively gone away does not accumulate memory forever.
MAX_QUEUE = 512


@dataclass
class Subscriber:
    """One connected replica."""

    id: str
    workspace: str
    actor: str
    queue: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=MAX_QUEUE)
    )
    overflowed: bool = False

    def offer(self, message: dict[str, Any]) -> bool:
        """Non-blocking send. False means this subscriber is too far behind to keep."""
        try:
            self.queue.put_nowait(message)
            return True
        except asyncio.QueueFull:
            self.overflowed = True
            return False


class SyncHub:
    """Owns the live subscriber set and the append→broadcast path."""

    def __init__(self, oplog: OpLog):
        self.oplog = oplog
        self._subs: dict[str, dict[str, Subscriber]] = defaultdict(dict)
        self._lock = asyncio.Lock()

    async def subscribe(self, workspace: str, actor: str, sub_id: str) -> Subscriber:
        sub = Subscriber(id=sub_id, workspace=workspace, actor=actor)
        async with self._lock:
            self._subs[workspace][sub_id] = sub
        log.info("subscribe ws=%s actor=%s id=%s (%d live)",
                 workspace, actor, sub_id, len(self._subs[workspace]))
        return sub

    async def unsubscribe(self, workspace: str, sub_id: str) -> None:
        async with self._lock:
            self._subs[workspace].pop(sub_id, None)
            if not self._subs[workspace]:
                del self._subs[workspace]

    async def publish(
        self, workspace: str, ops: list[dict[str, Any]], origin: str | None = None
    ) -> list[StoredOp]:
        """Durably append, then fan out. In that order, always.

        Broadcasting before the append would let a client observe an op that a crash then
        loses — the one inconsistency a log-backed system must never produce. Appending first
        costs one fsync of latency and makes "acknowledged" mean "durable".
        """
        stored = await asyncio.to_thread(self.oplog.append, workspace, ops)
        if not stored:
            return []

        message = {
            "type": "ops",
            "workspace": workspace,
            "ops": [s.to_wire() for s in stored],
            "head": stored[-1].seq,
        }
        dropped: list[str] = []
        async with self._lock:
            for sub_id, sub in self._subs.get(workspace, {}).items():
                # The originating connection is skipped: it applied these ops locally before
                # sending them, and echoing them back is pure waste on the exact connection
                # most likely to be constrained.
                if origin is not None and sub_id == origin:
                    continue
                if not sub.offer(message):
                    dropped.append(sub_id)

            for sub_id in dropped:
                sub = self._subs[workspace].pop(sub_id, None)
                log.warning(
                    "dropping subscriber %s: queue full at %d messages. It will reconnect "
                    "and replay from its last sequence.", sub_id, MAX_QUEUE,
                )
                if sub is not None:
                    # Best-effort resync hint. If even this cannot be queued the socket is
                    # already gone and the client's own reconnect logic takes over.
                    try:
                        sub.queue.put_nowait({"type": "resync", "reason": "queue_overflow"})
                    except asyncio.QueueFull:
                        pass
        return stored

    async def catch_up(self, workspace: str, since: int, limit: int = 5000) -> dict[str, Any]:
        """Everything the client missed, plus the current head."""
        ops = await asyncio.to_thread(self.oplog.since, workspace, since, limit)
        head = await asyncio.to_thread(self.oplog.head, workspace)
        return {
            "type": "catchup",
            "workspace": workspace,
            "ops": [o.to_wire() for o in ops],
            "head": head,
            # The client keeps requesting while this is true. Chunking rather than streaming
            # the whole history keeps a first sync on a large workspace from blocking the
            # event loop in one enormous serialisation.
            "more": bool(ops) and ops[-1].seq < head,
        }

    def live_count(self, workspace: str) -> int:
        return len(self._subs.get(workspace, {}))

    def presence(self, workspace: str) -> list[str]:
        """Distinct actors currently connected. Used for the console's presence row."""
        return sorted({s.actor for s in self._subs.get(workspace, {}).values()})
