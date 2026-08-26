"""Where agents live: versioned, published, and resolved by the key in a customer's page.

THREE OPERATIONS, AND THE MIDDLE ONE IS THE PRODUCT. `save` writes a draft. `publish` makes a
version live. `resolve` turns the public key sitting in a customer's website into the agent that
answers their buyer. Everything a tenant does in a builder is the first two; everything their
buyers experience is the third.

PUBLISHING IS A POINTER MOVE, NOT AN EDIT. Versions are immutable rows and `published_version`
names one of them. That gives rollback for free — a customer whose agent starts saying something
wrong at four in the afternoon needs the previous version back in seconds, not a support ticket
— and it means a call already running is unaffected, because it holds the spec object it started
with rather than a row it re-reads.

THE PUBLIC KEY IS PUBLIC AND AUTHORISES NOTHING. It goes in a script tag on a customer's
marketing site, so it will be read by everyone who views source. It identifies which agent
should answer; it does not grant the ability to change one. Editing goes through the tenant's
own authentication, which this repository does not implement and says so rather than pretending
a key is a credential.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .spec import AgentSpec, Fact, Guardrails, SpecError, Tier

log = logging.getLogger("rainmaker.agents")

DATA_DIR = Path(os.environ.get("RAINMAKER_DATA", "data"))
DB_PATH = Path(os.environ.get("RAINMAKER_AGENTS_DB", DATA_DIR / "agents.sqlite3"))


class AgentStore:
    """Agent specs, versioned. One instance per process."""

    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._conn = self._connect()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    tenant            TEXT NOT NULL,
                    agent_id          TEXT NOT NULL,
                    public_key        TEXT NOT NULL UNIQUE,
                    published_version INTEGER,
                    created_at        TEXT NOT NULL,
                    PRIMARY KEY (tenant, agent_id)
                );

                CREATE TABLE IF NOT EXISTS agent_versions (
                    tenant     TEXT NOT NULL,
                    agent_id   TEXT NOT NULL,
                    version    INTEGER NOT NULL,
                    spec       TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (tenant, agent_id, version),
                    FOREIGN KEY (tenant, agent_id) REFERENCES agents (tenant, agent_id)
                );

                CREATE INDEX IF NOT EXISTS agents_by_key ON agents (public_key);
                """
            )
            self._conn.commit()

    # ── writing ─────────────────────────────────────────────────────────
    def save(self, spec: AgentSpec) -> AgentSpec:
        """Write a draft version. Validates first: a bad spec never reaches the database.

        Validation at write rather than at read is the whole point of having a publish step.
        A misconfigured agent should fail in a builder in front of the person who broke it, not
        halfway through a call in front of their customer.
        """
        spec.validate()
        now = datetime.now(UTC).isoformat()

        with self._lock:
            row = self._conn.execute(
                "SELECT public_key FROM agents WHERE tenant = ? AND agent_id = ?",
                (spec.tenant, spec.agent_id),
            ).fetchone()
            public_key = row["public_key"] if row else (spec.public_key or _mint_key())
            if row is None:
                self._conn.execute(
                    "INSERT INTO agents (tenant, agent_id, public_key, published_version,"
                    " created_at) VALUES (?,?,?,NULL,?)",
                    (spec.tenant, spec.agent_id, public_key, now),
                )

            stored = AgentSpec.from_dict({**spec.as_dict(), "public_key": public_key})
            self._conn.execute(
                "INSERT OR REPLACE INTO agent_versions (tenant, agent_id, version, spec,"
                " created_at) VALUES (?,?,?,?,?)",
                (
                    spec.tenant,
                    spec.agent_id,
                    spec.version,
                    json.dumps(stored.as_dict(), separators=(",", ":")),
                    now,
                ),
            )
            self._conn.commit()
        return stored

    def publish(self, tenant: str, agent_id: str, version: int) -> AgentSpec:
        """Make a version the one that answers calls. A pointer move, so rollback is the same call."""
        spec = self.version(tenant, agent_id, version)
        if spec is None:
            raise SpecError(f"no version {version} of {tenant}/{agent_id} to publish")
        spec.validate()

        with self._lock:
            self._conn.execute(
                "UPDATE agents SET published_version = ? WHERE tenant = ? AND agent_id = ?",
                (version, tenant, agent_id),
            )
            self._conn.commit()
        log.info("published %s/%s v%d", tenant, agent_id, version)
        return AgentSpec.from_dict({**spec.as_dict(), "published": True})

    # ── reading ─────────────────────────────────────────────────────────
    def version(self, tenant: str, agent_id: str, version: int) -> AgentSpec | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT spec FROM agent_versions WHERE tenant = ? AND agent_id = ? AND version = ?",
                (tenant, agent_id, version),
            ).fetchone()
        return AgentSpec.from_dict(json.loads(row["spec"])) if row else None

    def live(self, tenant: str, agent_id: str) -> AgentSpec | None:
        """The published version, or nothing. A draft never answers a call."""
        with self._lock:
            row = self._conn.execute(
                "SELECT published_version FROM agents WHERE tenant = ? AND agent_id = ?",
                (tenant, agent_id),
            ).fetchone()
        if row is None or row["published_version"] is None:
            return None
        spec = self.version(tenant, agent_id, int(row["published_version"]))
        return AgentSpec.from_dict({**spec.as_dict(), "published": True}) if spec else None

    def resolve(self, public_key: str) -> AgentSpec | None:
        """The agent a customer's website is asking for.

        THE HOT PATH, and the only one an anonymous visitor can reach. Returns the published
        version or nothing — an unpublished agent, a revoked key and a typo are the same answer
        on purpose, because distinguishing them for a stranger is an enumeration oracle.
        """
        if not public_key:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT tenant, agent_id, published_version FROM agents WHERE public_key = ?",
                (public_key,),
            ).fetchone()
        if row is None or row["published_version"] is None:
            return None
        return self.live(row["tenant"], row["agent_id"])

    def versions(self, tenant: str, agent_id: str) -> list[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT version FROM agent_versions WHERE tenant = ? AND agent_id = ?"
                " ORDER BY version",
                (tenant, agent_id),
            ).fetchall()
        return [int(row["version"]) for row in rows]

    def list_agents(self, tenant: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT tenant, agent_id, public_key, published_version FROM agents"
        params: tuple[Any, ...] = ()
        if tenant:
            sql += " WHERE tenant = ?"
            params = (tenant,)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY tenant, agent_id", params).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _mint_key() -> str:
    """A public agent key. Long enough that guessing one is not a strategy."""
    return f"rk_{secrets.token_urlsafe(24)}"


# ───────────────────────────────────────────────────────────── tenant zero
#: Rainmaker's own agent, expressed the same way a customer's would be.
#:
#: THIS IS THE POINT OF THE WHOLE FILE. Liv used to be constants in `calls/session.py`. She is
#: now a row loaded through exactly the path a customer's agent takes, so the demo cannot drift
#: away from the product: if configuration breaks, our own front page breaks with it.
LIV_TENANT = "rainmaker"
LIV_AGENT = "liv"


def liv_spec() -> AgentSpec:
    return AgentSpec(
        tenant=LIV_TENANT,
        agent_id=LIV_AGENT,
        version=1,
        name="Liv",
        company="Rainmaker",
        persona="a direct, well-prepared account executive who does not oversell",
        objective=(
            "Understand what the prospect is trying to fix, and find out whether it is worth "
            "putting a person on the next call."
        ),
        voice="liv",
        portrait="/agent/liv.jpg",
        knowledge=(
            Fact(
                text=(
                    "Rainmaker is a sales platform with three parts: a research agent that reads "
                    "a company's public website, an AI account executive that runs the first "
                    "call, and a pipeline console for the reps."
                ),
                source="product overview",
            ),
            Fact(
                text=(
                    "The console is offline-first. Every edit is saved on the rep's own device "
                    "immediately and synced afterwards, so it keeps working with no internet at "
                    "all — on a train, in a basement, on a bad hotel connection."
                ),
                source="product overview",
                topic="offline",
            ),
            Fact(
                text=(
                    "Two reps editing the same deal while both offline end up in agreement "
                    "automatically, because the data is a CRDT rather than a database row."
                ),
                source="architecture",
                topic="offline",
            ),
            Fact(
                text=(
                    "The language model, the voice and the research agent all run on the "
                    "customer's own hardware, so recorded calls and pipeline data never leave "
                    "it. There is no per-call cost and no third-party AI vendor in the contract."
                ),
                source="architecture",
                topic="security",
            ),
            Fact(
                text=(
                    "The agent says it is an AI before anything else, and that cannot be "
                    "switched off — not by us and not by the customer configuring it."
                ),
                source="guardrails",
                topic="security",
            ),
            Fact(
                text=(
                    "Ask for a human and the agent stops selling immediately and hands over. "
                    "The decision is made in code, not by the language model."
                ),
                source="guardrails",
                topic="security",
            ),
            Fact(
                text=(
                    "Pricing depends on seats and on whether it runs in the customer's own "
                    "environment. It is quoted by a person."
                ),
                source="pricing",
                topic="pricing",
            ),
            Fact(
                text=(
                    "The agent connects to a customer's own calendar and CRM over MCP, so their "
                    "existing systems are a configuration line rather than a Rainmaker release."
                ),
                source="integrations",
                topic="integrations",
            ),
        ),
        pricing=(
            Tier("Team", "$40 / seat / month", "up to 25 reps, hosted by us"),
            Tier("Business", "$75 / seat / month", "SSO, your own environment"),
            Tier("Enterprise", "quoted", "self-hosted, custom retention"),
        ),
        pricing_note=(
            "Sized from what her research found. Exact numbers come from a person — she is not "
            "allowed to quote one."
        ),
        tools=("calendar", "crm", "research", "email"),
        guardrails=Guardrails(),
    )


def seed(store: AgentStore) -> AgentSpec:
    """Make sure tenant zero exists and is published. Idempotent."""
    existing = store.live(LIV_TENANT, LIV_AGENT)
    if existing is not None:
        return existing
    saved = store.save(liv_spec())
    return store.publish(saved.tenant, saved.agent_id, saved.version)
