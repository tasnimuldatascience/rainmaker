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
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .spec import (
    AgentSpec,
    Competitor,
    Fact,
    Guardrails,
    Need,
    SpecError,
    Tier,
    TourStop,
)

log = logging.getLogger("rainmaker.agents")

DATA_DIR = Path(os.environ.get("RAINMAKER_DATA", "data"))
DB_PATH = Path(os.environ.get("RAINMAKER_AGENTS_DB", DATA_DIR / "agents.sqlite3"))

#: Where the console is served from, for the pages the tour drives to.
#:
#: OVERRIDABLE BECAUSE 5173 IS NOT A PROMISE. Vite takes the next free port when its default is
#: busy, and a developer who already has something on 5173 gets 5174 without being asked — at
#: which point a hardcoded tour navigates a live demo to a page that is not there. The rest of
#: this repository already parameterises the same host (`CORS_ORIGINS`,
#: `RAINMAKER_CHECKOUT_BASE`); this was the one place it did not.
CONSOLE = os.environ.get("RAINMAKER_CONSOLE", "http://localhost:5173").rstrip("/")


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
#: THIS IS THE POINT OF THE WHOLE FILE. Nadia used to be constants in `calls/session.py`. She is
#: now a row loaded through exactly the path a customer's agent takes, so the demo cannot drift
#: away from the product: if configuration breaks, our own front page breaks with it.
NADIA_TENANT = "rainmaker"
NADIA_AGENT = "nadia"


def nadia_spec() -> AgentSpec:
    """Rainmaker's own agent, configured the way a customer configures theirs.

    WHAT SHE SELLS IS THE AGENT ITSELF, which makes her the product demonstrating itself: a
    buyer asking Nadia how Rainmaker works is watching Rainmaker work. The knowledge below is
    written as a salesperson would say it, not as documentation — the earlier version led with
    "the console is offline-first", which is a true sentence about a CRDT and not a reason
    anybody buys anything.
    """
    return AgentSpec(
        tenant=NADIA_TENANT,
        agent_id=NADIA_AGENT,
        version=1,
        name="Nadia",
        company="Rainmaker",
        persona="a direct, well-prepared account executive who does not oversell",
        objective=(
            "Work out whether an AI agent would actually help their sales team, show them how "
            "it works on their own business, and either get them started or get them a time "
            "with someone."
        ),
        # A different voice from the demo tenant's, so two agents on one machine are told apart
        # by ear. Both are top-graded; see `providers.KokoroTextToSpeech.VOICES`.
        voice="female-clear",
        portrait="/agent/nadia.jpg",
        knowledge=(
            Fact(
                "Rainmaker is an AI sales agent that talks to your buyers the moment they land "
                "on your site, at any hour, with no waiting for a rep to be free.",
                source="positioning",
            ),
            Fact(
                "It reads the buyer's company from their work email before it says a word, so "
                "the conversation is about their business rather than a script.",
                source="positioning",
            ),
            Fact(
                "It runs the whole first call: understands what they need, walks them through "
                "the product on screen, compares against what else they are looking at, quotes "
                "them, and takes payment.",
                source="positioning",
            ),
            Fact(
                "When a deal genuinely needs a person, it books one from a real calendar rather "
                "than promising a callback.",
                source="positioning",
            ),
            Fact(
                "Most inbound buyers wait days for a first call. The ones who do not wait go "
                "and talk to somebody else, which is the revenue this recovers.",
                source="the problem",
                topic="why",
            ),
            Fact(
                "You configure your agent once: what it knows, what it may claim, what it "
                "charges, and where it takes people on the demo. No engineering work.",
                source="setup",
                topic="setup",
            ),
            Fact(
                "It connects to your own calendar, CRM and payment provider over MCP, so your "
                "existing systems are a configuration line rather than a project.",
                source="integrations",
                topic="integrations",
            ),
            Fact(
                "The agent says it is an AI before anything else and that cannot be switched "
                "off, by us or by you. Ask it for a human and it stops selling immediately.",
                source="guardrails",
                topic="security",
            ),
            Fact(
                "It can only state facts you gave it. It is not allowed to invent a price, a "
                "capability or a customer name, and every claim traces back to who wrote it.",
                source="guardrails",
                topic="security",
            ),
            Fact(
                "The language model and the voice run on your own hardware if you want them "
                "to, so recorded calls and pipeline data never leave your infrastructure.",
                source="architecture",
                topic="security",
            ),
        ),
        # WHY ANYBODY BUYS AN AI SALES AGENT, and how to spot each reason in a website. The
        # agent reads a prospect's site and has to do something with what it read; without this
        # it reads the findings out, which is not selling.
        needs=(
            Need(
                signals=(
                    "book a demo", "request a demo", "contact sales", "talk to sales",
                    "get a quote", "free trial", "sign up", "pricing",
                ),
                means=(
                    "they take inbound demand through a form, so every buyer who arrives out "
                    "of hours waits for somebody to get back to them"
                ),
                opener=(
                    "it looks like interested buyers reach you through a form, which means the "
                    "ones who turn up out of hours wait"
                ),
                ask="who picks up a demo request that lands at eleven at night?",
            ),
            Need(
                signals=(
                    "sales", "account executive", "sdr", "business development", "revenue",
                    "go-to-market", "hiring",
                ),
                means=(
                    "they are adding sales headcount, which is the expensive way to answer more "
                    "buyers faster"
                ),
                opener=(
                    "it looks like you are growing the sales team, which is the expensive way "
                    "to get back to more buyers faster"
                ),
                ask="how long does a new rep take to get productive with you?",
            ),
            Need(
                signals=("support", "help centre", "help center", "chat", "live chat", "faq"),
                means="they already answer questions in a widget that cannot sell anything",
                opener=(
                    "it looks like you already answer questions in a chat widget, and that "
                    "widget cannot sell anybody anything"
                ),
                ask="does that chat ever turn into a real sales conversation?",
            ),
        ),
        tour=(
            TourStop(
                url=f"{CONSOLE}/demo/tessera.html",
                label="an agent on a customer's site",
                shows=(
                    "a real customer's website with their own agent in the corner - their name, "
                    "their voice, their prices, one script tag"
                ),
                scroll_to="Per GPU-hour",
                answers=("how it works", "embed", "website", "install", "set up", "look like"),
            ),
            TourStop(
                url=f"{CONSOLE}/",
                label="the pipeline the calls write into",
                shows=(
                    "the deals board a rep works from, with the outcome and transcript of every "
                    "agent call already on it"
                ),
                answers=("crm", "pipeline", "reps", "after the call", "handover", "team"),
            ),
        ),
        competitors=(
            Competitor(
                name="a chat widget",
                positioning="cheap, instant to install, and fine for answering a shipping question",
                against=(
                    ("what it does", "hold a real sales conversation rather than route a ticket"),
                    ("preparation", "read the buyer's company before the first sentence"),
                    ("outcome", "quote, take payment, or book a person"),
                ),
            ),
            Competitor(
                name="hiring another SDR",
                positioning="judgement, relationships, and the ability to handle anything",
                against=(
                    ("availability", "answer at 2am and on a Sunday, with no queue"),
                    ("ramp", "be live the day you configure it"),
                    ("cost", "not scale with headcount"),
                ),
            ),
        ),
        pricing=(
            Tier(
                "Team",
                "$40 / seat / month",
                "up to 25 reps, hosted by us",
                unit_amount=4000,
                min_seats=1,
            ),
            Tier(
                "Business",
                "$75 / seat / month",
                "SSO, your own environment",
                unit_amount=7500,
                min_seats=20,
            ),
            Tier("Enterprise", "quoted", "self-hosted, custom retention"),
        ),
        pricing_note="Sized from what she found about your business. Annual billing saves 15%.",
        pricing_period="month",
        currency="usd",
        annual_discount_pct=15,
        tools=("calendar", "crm", "research", "email", "payments"),
        guardrails=Guardrails(),
    )


def seed(store: AgentStore) -> AgentSpec:
    """Make sure tenant zero exists and is published, and matches the code.

    IDEMPOTENT, BUT NOT INERT, and the difference cost a whole debugging session. Nadia is defined
    in `nadia_spec()` rather than in a builder, so a change to her — a new tour stop, a competitor,
    a price with an amount on it — is a code change. A seed that returned early whenever any
    version was live meant the running agent was whatever had been seeded first: the tour was
    empty and the comparison step fell through to the model, silently, on a database nobody
    thought to look at.

    A tenant's own agent is the opposite case and is left alone: their versions are theirs, and
    nothing here writes one.
    """
    existing = store.live(NADIA_TENANT, NADIA_AGENT)
    wanted = nadia_spec()
    if existing is not None and _same_agent(existing, wanted):
        return existing

    # A new version rather than an edit of the live one: publishing is a pointer move, so an
    # upgrade cannot alter an agent underneath somebody who is mid-conversation with it.
    versions = store.versions(NADIA_TENANT, NADIA_AGENT)
    saved = store.save(replace(wanted, version=max(versions) + 1 if versions else wanted.version))
    return store.publish(saved.tenant, saved.agent_id, saved.version)


def _same_agent(live: AgentSpec, wanted: AgentSpec) -> bool:
    """Whether two specs say the same thing, ignoring what versioning adds."""
    ignore = {"version", "public_key", "published", "created_at", "updated_at"}
    return {k: v for k, v in live.as_dict().items() if k not in ignore} == {
        k: v for k, v in wanted.as_dict().items() if k not in ignore
    }
