"""What an agent IS, as data rather than as code.

THIS IS THE FILE THAT TURNS A DEMO INTO A PRODUCT. Until now "Liv, who works at Rainmaker and
may say these eleven things about it" lived in constants in `calls/session.py`: the agent was
compiled, and shipping a second one meant a release. Rainmaker sells the agent to other
businesses, each of whom points it at their own buyers, so the agent has to be a row someone
edits — persona, knowledge, prices, voice, face — and Liv has to be the first row rather than a
special case. If our own demo runs down a different path from the one customers get, it drifts,
and we end up demonstrating something we do not sell.

WHAT A TENANT MAY CHANGE, AND WHAT THEY MAY NOT, is the load-bearing distinction here.

    theirs      the name, the persona, the objective, the voice, the face, what the agent
                knows, what it charges, how each step of the call is framed.
    ours        that it discloses it is an AI. That it hands over when asked for a person.
                That it may only state what its knowledge contains. That it cannot reach a
                tool it was not granted.

Those four are not withheld to be paternalistic — they are the difference between selling
software and inheriting somebody else's liability. When a customer's buyer is told something
untrue by an agent, the sentence that matters in the complaint is "the vendor's AI said it".
Making disclosure structurally impossible to switch off is also the answer to every customer's
compliance review, which makes it a feature rather than a restriction.

VERSIONS ARE IMMUTABLE AND CALLS PIN ONE. Publishing a change while somebody is mid-call must
not change the agent underneath them — an agent that alters its prices between two sentences is
worse than one with stale prices. So a spec is written once, never edited, and a live call holds
the version it started with.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

#: Ceilings, not preferences. Every one of these is either something that has to fit in a
#: prompt alongside the call rules, or something a caller would sit through.
MAX_KNOWLEDGE_ITEMS = 60
MAX_KNOWLEDGE_CHARS = 600
MAX_TIERS = 6

#: The voices the platform offers. A tenant naming a voice that does not exist should be told
#: at publish time, not discovered when their agent opens its mouth in front of a customer.
VOICES = ("liv", "female-warm", "female-clear", "male-warm", "male-us")


#: Phrases that actually tell someone they are not talking to a person. Deliberately about
#: MACHINE-NESS rather than about being an assistant: a human can be an assistant, so "I'm your
#: assistant here at Acme" must not pass.
_DISCLOSING = (
    "ai ", "ai,", "ai.", "an ai", "a.i.", "artificial",
    "automated", "machine", "bot", "robot", "synthetic", "computer",
    "not a human", "not human", "not a person", "not a real person",
)


class SpecError(ValueError):
    """A configuration that would produce a bad call. Raised at publish, never at call time."""


@dataclass(frozen=True, slots=True)
class Fact:
    """One thing the agent is allowed to say, and where it came from.

    THE SOURCE IS NOT DECORATION. An agent may only state what its knowledge contains, so the
    knowledge is the whole surface on which a customer can be wrong about their own product.
    Attaching a source to every claim means a disputed sentence can be traced to the person who
    wrote it rather than argued about.
    """

    text: str
    source: str = ""
    #: Grouping for retrieval and for the editor: "pricing", "security", "integrations".
    topic: str = ""

    def validate(self) -> None:
        if not self.text.strip():
            raise SpecError("a fact with no text is not a fact")
        if len(self.text) > MAX_KNOWLEDGE_CHARS:
            raise SpecError(
                f"fact is {len(self.text)} characters; keep them under {MAX_KNOWLEDGE_CHARS} "
                "so several fit in a prompt beside the call rules"
            )


@dataclass(frozen=True, slots=True)
class Tier:
    """A price the agent may put on screen. It is never spoken — see `agenda._price`."""

    name: str
    price: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Guardrails:
    """The rules the platform enforces on every agent, whoever configured it.

    The WORDING of the disclosure is a tenant's to change — their brand, their jurisdiction,
    their lawyers. Its EXISTENCE is not, and neither is the handoff. `CallPipeline` already
    refuses to run a turn before the disclosure has been delivered; this is where a tenant is
    told they cannot ask for that in the first place.
    """

    disclosure: str = (
        "Hi, before we start — I'm an AI assistant, not a human. "
        "I can walk you through the product and answer questions, "
        "and I can bring in a person any time you'd like."
    )
    handoff_line: str = (
        "Of course — let me bring someone in. I'll pass along everything we covered so you "
        "don't have to repeat yourself."
    )
    #: Never quote a figure out loud. On screen a number is a reference; spoken on a sales call
    #: it is a commitment, and it would be a small model's guess.
    speak_prices: bool = False
    #: How many sentences a turn may run to. Enforced in `pipeline.cap_sentences`.
    max_sentences: int = 2

    def validate(self) -> None:
        if not self.disclosure.strip():
            raise SpecError(
                "the disclosure cannot be empty. Its wording is yours; its existence is not — "
                "an AI on a sales call that does not say so is the mistake that ends a company"
            )
        # A disclosure that does not disclose is worse than none: it looks like compliance.
        #
        # THE LIST HAS TO BE WIDE ENOUGH TO NOT REJECT HONEST WORDING. It started as five terms
        # and immediately refused "I'm an automated assistant, not a person" from a dental
        # practice — a perfectly good disclosure that happened to avoid the word "AI". A guard
        # that rejects honest rewordings is a guard people work around, and the way they work
        # around this one is by writing something that passes the check without disclosing.
        lowered = self.disclosure.lower()
        if not any(word in lowered for word in _DISCLOSING):
            raise SpecError(
                "the disclosure must actually say the agent is an AI; "
                f"{self.disclosure!r} does not"
            )
        if not self.handoff_line.strip():
            raise SpecError("the handoff line cannot be empty")
        if self.speak_prices:
            raise SpecError(
                "prices are shown, never spoken. A number on screen is a reference; the same "
                "number said aloud on a sales call is a quote, and this one would be a "
                "1.5B model's guess"
            )
        if not 1 <= self.max_sentences <= 4:
            raise SpecError("max_sentences must be between 1 and 4; a phone call is not an essay")


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """One configured agent, at one version.

    Frozen on purpose. A live call holds this object for its whole duration, and a mutable spec
    would let a publish change an agent's prices between two of its own sentences.
    """

    tenant: str
    agent_id: str
    version: int = 1

    # ── identity ────────────────────────────────────────────────────────
    name: str = "Liv"
    company: str = "Rainmaker"
    persona: str = "a direct, well-prepared account executive who does not oversell"
    objective: str = (
        "Understand what the prospect is trying to fix, and find out whether it is worth "
        "putting a person on the next call."
    )
    voice: str = "liv"
    portrait: str = "/agent/liv.jpg"

    # ── what it may say ─────────────────────────────────────────────────
    knowledge: tuple[Fact, ...] = ()
    pricing: tuple[Tier, ...] = ()
    pricing_note: str = "Exact numbers come from a person."

    # ── what it may reach ───────────────────────────────────────────────
    #: An allow-list, not a deny-list. An agent reaching a tool nobody granted it is the failure
    #: mode worth designing against, and a default of "everything" makes that the normal case.
    tools: tuple[str, ...] = ("calendar", "crm", "research")

    # ── how each step is framed ─────────────────────────────────────────
    #: Per-step objective overrides, keyed by `agenda.Step`. The STEPS themselves stay in code:
    #: a tenant editing the shape of a call is a config language, a debugger and a support
    #: burden, and the shape is not what differs between businesses. What they say at each step
    #: is.
    step_objectives: tuple[tuple[str, str], ...] = ()

    guardrails: Guardrails = field(default_factory=Guardrails)

    # ── lifecycle ───────────────────────────────────────────────────────
    published: bool = False
    #: What the embed presents. Public by design — it identifies an agent, it does not authorise
    #: anything, and it will sit in the page source of the customer's website.
    public_key: str = ""

    # ── validation ──────────────────────────────────────────────────────
    def validate(self) -> None:
        """Everything that would make a bad call, caught before anyone is on one."""
        if not _SLUG.match(self.tenant):
            raise SpecError(f"tenant must be a slug, got {self.tenant!r}")
        if not _SLUG.match(self.agent_id):
            raise SpecError(f"agent_id must be a slug, got {self.agent_id!r}")
        if not self.name.strip():
            raise SpecError("an agent needs a name; it says it on every call")
        if not self.company.strip():
            raise SpecError("an agent needs a company; it is in the first sentence")
        if self.voice not in VOICES:
            raise SpecError(f"unknown voice {self.voice!r}; choose from {', '.join(VOICES)}")
        if len(self.knowledge) > MAX_KNOWLEDGE_ITEMS:
            raise SpecError(
                f"{len(self.knowledge)} facts is more than fits in a prompt beside the call "
                f"rules; keep it under {MAX_KNOWLEDGE_ITEMS}"
            )
        for fact in self.knowledge:
            fact.validate()
        if len(self.pricing) > MAX_TIERS:
            raise SpecError(f"{len(self.pricing)} tiers is more than a caller can hold")
        for tool in self.tools:
            if "." in tool:
                raise SpecError(
                    f"grant a server, not a tool: {tool!r}. Naming individual tools means an "
                    "agent breaks when the server adds one"
                )
        self.guardrails.validate()

    # ── use ─────────────────────────────────────────────────────────────
    def objective_for(self, step: str, default: str) -> str:
        for name, objective in self.step_objectives:
            if name == step and objective.strip():
                return objective
        return default

    def may_use(self, qualified_tool: str) -> bool:
        """Whether this agent is allowed to call `server.tool`.

        Checked at call time as well as at publish, because the grant can be revoked while a
        call is running and the answer must be current, not the one from when it started.
        """
        return qualified_tool.split(".", 1)[0] in self.tools

    def knowledge_text(self) -> str:
        """The agent's knowledge as the prompt sees it.

        Grouped by topic, because a model given a flat list of sixty sentences answers from the
        first three. Sources are included: the agent is told it may only state what is here, so
        the list is also the audit trail for anything it said.
        """
        if not self.knowledge:
            return ""
        by_topic: dict[str, list[Fact]] = {}
        for fact in self.knowledge:
            by_topic.setdefault(fact.topic or "general", []).append(fact)

        lines: list[str] = []
        for topic in sorted(by_topic):
            if topic != "general":
                lines.append(f"\n{topic.upper()}")
            for fact in by_topic[topic]:
                suffix = f"  [{fact.source}]" if fact.source else ""
                lines.append(f"- {fact.text}{suffix}")
        return "\n".join(lines).strip()

    def bump(self, **changes: Any) -> AgentSpec:
        """The next version of this agent. Never mutates; validates before it returns."""
        nxt = replace(self, version=self.version + 1, published=False, **changes)
        nxt.validate()
        return nxt

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "agent_id": self.agent_id,
            "version": self.version,
            "name": self.name,
            "company": self.company,
            "persona": self.persona,
            "objective": self.objective,
            "voice": self.voice,
            "portrait": self.portrait,
            "knowledge": [
                {"text": f.text, "source": f.source, "topic": f.topic} for f in self.knowledge
            ],
            "pricing": [
                {"name": t.name, "price": t.price, "detail": t.detail} for t in self.pricing
            ],
            "pricing_note": self.pricing_note,
            "tools": list(self.tools),
            "step_objectives": [list(pair) for pair in self.step_objectives],
            "guardrails": {
                "disclosure": self.guardrails.disclosure,
                "handoff_line": self.guardrails.handoff_line,
                "speak_prices": self.guardrails.speak_prices,
                "max_sentences": self.guardrails.max_sentences,
            },
            "published": self.published,
            "public_key": self.public_key,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AgentSpec:
        rails = raw.get("guardrails") or {}
        return cls(
            tenant=raw["tenant"],
            agent_id=raw["agent_id"],
            version=int(raw.get("version", 1)),
            name=raw.get("name", "Liv"),
            company=raw.get("company", "Rainmaker"),
            persona=raw.get("persona", AgentSpec.persona),
            objective=raw.get("objective", AgentSpec.objective),
            voice=raw.get("voice", "liv"),
            portrait=raw.get("portrait", "/agent/liv.jpg"),
            knowledge=tuple(
                Fact(text=f["text"], source=f.get("source", ""), topic=f.get("topic", ""))
                for f in raw.get("knowledge", [])
            ),
            pricing=tuple(
                Tier(name=t["name"], price=t["price"], detail=t.get("detail", ""))
                for t in raw.get("pricing", [])
            ),
            pricing_note=raw.get("pricing_note", ""),
            tools=tuple(raw.get("tools", ())),
            step_objectives=tuple(
                (pair[0], pair[1]) for pair in raw.get("step_objectives", []) if len(pair) == 2
            ),
            guardrails=Guardrails(
                disclosure=rails.get("disclosure", Guardrails.disclosure),
                handoff_line=rails.get("handoff_line", Guardrails.handoff_line),
                speak_prices=bool(rails.get("speak_prices", False)),
                max_sentences=int(rails.get("max_sentences", 2)),
            ),
            published=bool(raw.get("published", False)),
            public_key=raw.get("public_key", ""),
        )


_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")
