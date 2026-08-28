"""Who may write to a workspace, enforced where it is enforceable.

THE CLIENT CANNOT BE ASKED. Actor identity was a string in a query parameter — `?actor=dana`
— which is not a claim about who you are, it is a claim you would like to make. Anyone who
could open a socket could open it as anybody, into any workspace, and the op log would attribute
their writes accordingly. A client that decides whether it is allowed to write has decided
nothing.

There is exactly one place this can be checked, and it is the relay: every op crosses it, and
it is the only component both replicas trust. That is why `oplog.py` has always listed
AUTHORISATION among the four things the server is responsible for. This module is the part that
was missing under that heading.

WHAT IS ENFORCED, precisely, because "auth" is a word that hides a lot:

    identity     a token binds one actor to one workspace and is signed. Editing the actor in
                 the query string now invalidates the signature rather than changing who you
                 are.
    membership   the (workspace, actor) pair must exist in `members`. A valid token for
                 workspace A is not a token for workspace B.
    attribution  every op's `actor` must match the token's. A member cannot write ops as a
                 colleague, which matters because the CRDT's tie-break is by actor id: forging
                 an actor is forging the outcome of a concurrent edit.
    revocation   membership is a row. Deleting it takes effect on the next connect and the next
                 append, without waiting for a token to expire.

WHAT IS NOT, equally precisely. Enrolment is open in this deployment: `POST /api/sync/token`
issues a token to whoever asks and enrols them. That is a POLICY choice for a demo that has no
sign-in screen, and it is the one line to change for a real one — the ENFORCEMENT path above is
real either way, and it is the part that is hard to add later. An open door with a working lock
is a different thing from a doorway.

HMAC RATHER THAN A SESSION TABLE. The relay is the hot path and a signature check is a hash,
not a query. The secret is per-deployment and generated on first use when unset, which means a
restart invalidates outstanding tokens on a dev machine and does not on a configured one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("rainmaker.sync.membership")

#: How long a token is good for. Short enough that a leaked one is not permanent, long enough
#: that a console left open overnight does not lose its socket at 3am.
TOKEN_TTL_SECONDS = int(os.environ.get("RAINMAKER_TOKEN_TTL", str(30 * 24 * 3600)))

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    workspace  TEXT NOT NULL,
    actor      TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT 'member',
    added_at   TEXT NOT NULL,
    PRIMARY KEY (workspace, actor)
);
"""


class AuthError(Exception):
    """A token that does not entitle its bearer to what they asked for.

    ONE EXCEPTION FOR EVERY FAILURE, and the caller turns it into one status code with one
    message. A missing token, a bad signature, an expired token and a revoked membership are
    the same answer to a stranger, for the same reason the agent store returns None for an
    unknown key and an unpublished agent: distinguishing them is an oracle.
    """


@dataclass(frozen=True)
class Grant:
    """A verified claim: this actor, in this workspace."""

    workspace: str
    actor: str
    role: str = "member"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Members:
    """The membership table, and the tokens that assert a row in it."""

    def __init__(self, path: Path | str = ":memory:", secret: str | None = None):
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        # A GENERATED SECRET IS NOT A CONFIGURED ONE, and the difference is visible: with
        # RAINMAKER_SYNC_SECRET unset every restart mints a new one and outstanding tokens stop
        # verifying. That is the correct failure for a machine nobody configured -- it fails
        # closed, loudly, on the next connect -- and the log line says which case you are in.
        configured = secret or os.environ.get("RAINMAKER_SYNC_SECRET", "")
        if not configured:
            configured = secrets.token_urlsafe(32)
            log.warning(
                "RAINMAKER_SYNC_SECRET is unset; generated an ephemeral one. "
                "Tokens will not survive a restart."
            )
        self._secret = configured.encode()

    # ───────────────────────────────────────────────────────────── membership
    def add(self, workspace: str, actor: str, role: str = "member") -> None:
        from datetime import UTC, datetime

        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO members (workspace, actor, role, added_at)"
                " VALUES (?, ?, ?, ?)",
                (workspace, actor, role, datetime.now(UTC).isoformat()),
            )
            self._conn.commit()

    def remove(self, workspace: str, actor: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM members WHERE workspace = ? AND actor = ?", (workspace, actor)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def role_of(self, workspace: str, actor: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT role FROM members WHERE workspace = ? AND actor = ?",
                (workspace, actor),
            ).fetchone()
        return row["role"] if row else None

    def members(self, workspace: str) -> list[dict[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT actor, role, added_at FROM members WHERE workspace = ? ORDER BY actor",
                (workspace,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ───────────────────────────────────────────────────────────── tokens
    def issue(self, workspace: str, actor: str, role: str = "member") -> str:
        """Enrol if new, and return a signed token for the pair."""
        if not workspace or not actor:
            raise AuthError("workspace and actor are required")
        self.add(workspace, actor, role)
        body = json.dumps(
            {"w": workspace, "a": actor, "exp": int(time.time()) + TOKEN_TTL_SECONDS},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        mac = hmac.new(self._secret, body, hashlib.sha256).digest()
        return f"{_b64(body)}.{_b64(mac)}"

    def verify(self, token: str, workspace: str) -> Grant:
        """The token, checked against the signature, the clock, and the table.

        ORDER MATTERS. The signature is checked before anything is read out of the body,
        because an unverified body is attacker-controlled text and using it to look up a row is
        how a signature check gets skipped by accident.
        """
        if not token:
            raise AuthError("no token")
        try:
            body_b64, mac_b64 = token.split(".", 1)
            body, mac = _unb64(body_b64), _unb64(mac_b64)
        except (ValueError, TypeError) as exc:
            raise AuthError("malformed token") from exc

        expected = hmac.new(self._secret, body, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            raise AuthError("bad signature")

        try:
            claims = json.loads(body)
        except json.JSONDecodeError as exc:
            raise AuthError("malformed token") from exc

        if int(claims.get("exp", 0)) < time.time():
            raise AuthError("expired")
        if claims.get("w") != workspace:
            raise AuthError("token is for another workspace")

        actor = str(claims.get("a", ""))
        # THE TABLE IS CHECKED EVERY TIME, not just at issue. A token is a claim that a row
        # existed; revocation is that row going away, and a check that trusted the signature
        # alone would keep an ex-member writing until their token expired.
        role = self.role_of(workspace, actor)
        if role is None:
            raise AuthError("not a member of this workspace")
        return Grant(workspace=workspace, actor=actor, role=role)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def authorise_ops(ops: list[dict], grant: Grant) -> None:
    """Every op must be attributed to the actor that the token names.

    FORGING AN ACTOR IS FORGING A MERGE. The CRDT breaks hybrid-logical-clock ties by actor id,
    so an op wearing somebody else's actor does not merely misattribute an edit — it changes
    which of two concurrent edits wins. That makes this check part of the merge's correctness,
    not only of its audit trail.
    """
    for op in ops:
        who = op.get("actor")
        if who is not None and who != grant.actor:
            raise AuthError("op actor does not match the authenticated actor")
