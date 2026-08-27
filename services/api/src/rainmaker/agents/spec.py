"""What an agent IS, as data rather than as code.

THIS IS THE FILE THAT TURNS A DEMO INTO A PRODUCT. Until now "Nadia, who works at Rainmaker and
may say these eleven things about it" lived in constants in `calls/session.py`: the agent was
compiled, and shipping a second one meant a release. Rainmaker sells the agent to other
businesses, each of whom points it at their own buyers, so the agent has to be a row someone
edits — persona, knowledge, prices, voice, face — and Nadia has to be the first row rather than a
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

#: Every voice the platform offers, and the Kokoro voice behind each.
#:
#: ONE TABLE, BECAUSE TWO OF THEM DRIFTED. The list a tenant is validated against lived here and
#: the list the synthesiser could actually produce lived in `calls/providers.py`, and they
#: stopped agreeing: `female-british` and `male-british` existed in the engine and were rejected
#: at publish time, while `male-us` validated and was an alias nothing documented. A tenant
#: hitting either of those learns about it in front of a customer.
#:
#: CHOSEN BY GRADE, NOT BY NAME. Kokoro publishes a quality grade per voice that tracks how much
#: training data it had, and it predicts how stable each one sounds. There are fifty-four in the
#: pack; these are the ones worth putting on a sales call.
#:
#: The demo agent was on `bf_isabella`, which is graded C — and measurably the worst of the set
#: on the thing that matters here: it peaks at 1.14 on our own quote sentence, above full scale.
#: That is digital clipping, it is a hard distortion, and it is one of the things people mean
#: when they say a voice sounds synthetic. `af_heart` (grade A) reads the same sentence at 0.45.
#:
#: `af_nicole` is graded B- and is excluded on measurement rather than grade: the same line takes
#: 19.6 seconds against 12 for the others, because it is a breathy ASMR voice. On a sales call
#: that is not a voice, it is a mood.
VOICE_CATALOGUE: dict[str, tuple[str, str]] = {
    # name              kokoro        language    grade
    "female-warm": ("af_heart", "en-us"),  # A
    "female-clear": ("af_bella", "en-us"),  # A-
    "female-british": ("bf_emma", "en-gb"),  # B-
    "male-warm": ("am_michael", "en-us"),  # C+
    "male-british": ("bm_george", "en-gb"),  # C
}

#: The names a tenant may write, for the publish-time check and its error message. A tenant
#: naming a voice that does not exist should be told at publish time, not discovered when their
#: agent opens its mouth in front of a customer.
VOICES = tuple(VOICE_CATALOGUE)

#: What an agent gets when nobody chose. The highest-graded voice in the pack.
DEFAULT_VOICE = "female-warm"


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
    """One row of the tenant's pricing.

    TWO REPRESENTATIONS, AND BOTH ARE NEEDED. `price` is what a human wrote and what goes on
    screen: "£120-£180", "quoted", "$40 / seat / month". `unit_amount` is minor units for
    arithmetic, and is what a quote and a payment are built from. A tier with a display price
    and no amount is legitimate — "Enterprise, quoted" cannot be multiplied by a seat count and
    should not pretend it can.
    """

    name: str
    price: str
    detail: str = ""
    #: Minor units: cents, pence. Zero means "not quotable", which is not the same as free.
    unit_amount: int = 0
    #: The smallest quantity this tier applies to, so a big buyer lands on the right row rather
    #: than being quoted the starter rate two hundred times.
    min_seats: int = 1
    #: WHAT IS BEING COUNTED, AND IT IS NOT ALWAYS A SEAT. A GPU cloud sells hours, a roaster
    #: sells kilos, a charging company sells chargers. The word was hard-coded into the quote
    #: until a second tenant needed a different one — the usual way a platform finds out it has
    #: been shaped around its first customer.
    unit_name: str = "seat"
    #: The plural, for the words where adding an "s" is wrong. Empty means "seat" -> "seats".
    unit_plural: str = ""

    def units(self, count: int) -> str:
        """`40` -> "40 GPU-hours"; `1` -> "one GPU-hour"."""
        plural = self.unit_plural or f"{self.unit_name}s"
        return f"one {self.unit_name}" if count == 1 else f"{count:,} {plural}"


@dataclass(frozen=True, slots=True)
class Intake:
    """What the form in front of the call asks for.

    Three fields, two of which are optional to ASK for and one of which is not: an agent that
    does not know who it is talking to cannot write the call down, follow it up, or take a
    payment that anyone could reconcile.
    """

    #: Ask which company they are with. Off for anything selling to consumers.
    ask_company: bool = True
    #: Refuse gmail and friends. Only meaningful alongside research, which is the thing the
    #: domain feeds.
    require_work_email: bool = True

    def fields(self) -> tuple[str, ...]:
        """The fields to render, in order. The client asks; it does not decide."""
        return ("name", "email", "company") if self.ask_company else ("name", "email")

    def as_dict(self) -> dict[str, Any]:
        return {
            "ask_company": self.ask_company,
            "require_work_email": self.require_work_email,
            "fields": list(self.fields()),
        }


@dataclass(frozen=True, slots=True)
class Need:
    """A reason somebody buys this, and how to recognise it in what research found.

    THIS IS THE MISSING LINK BETWEEN READING A SITE AND SELLING SOMETHING. Research produces
    facts about the BUYER; the agent sells a product. Nothing connected the two, so the agent
    did the only thing left: it read the facts out. On a real call that came out as "I noticed
    you're currently hiring four positions: Production Associates, Product Manager, Research and
    Development Engineer, and Sales and Marketing Specialist" — from an agent that rents GPUs.
    Four job titles, read back to the person who wrote them, none of them about GPUs.

    A seller does not recite what they found. They form a hypothesis: *this suggests you are
    doing X, which means you need Y — am I right?* The hypothesis is the product of a signal and
    a reason to buy, and the reason to buy is not something a language model should invent about
    a tenant's business. So the tenant writes it down.

        Need(
            signals=("machine learning", "training", "model"),
            means="they are training models and will be short of capacity",
            opener="it looks like you're training models already, and capacity is the constraint",
            ask="what are you training at the moment, and what are you training it on?",
        )

    WHY `means` AND `opener` BOTH EXIST, when they say the same thing. One is read by a language
    model and one is read out loud, and they are written in different persons because of it.
    `means` is third person: it goes into the system prompt as a belief the agent holds ABOUT
    the buyer, where "you" already refers to the agent and a second-person sentence would invert
    who it is about. `opener` is second person: it is said TO the buyer, and no model touches it.
    This repository already splits a price the same way — `total_display` for the eye and
    `spoken` for the ear — for the same reason: two senses, two representations, and converging
    them breaks one of them.

    `signals` are matched against the facts research returned. `ask` is the one question that
    checks the hypothesis, written by the tenant because a wrong question is worse than no
    question — and spoken exactly as written, for the same reason.
    """

    #: Words that suggest this need, matched case-insensitively against research facts.
    signals: tuple[str, ...]
    #: What the agent may say those signals imply, in the THIRD person. For the model's context.
    means: str
    #: The same belief in the SECOND person, said to the buyer verbatim. Lower case, no full
    #: stop: it is dropped into the middle of the opening sentence.
    opener: str
    #: The single question that checks the hypothesis. Spoken exactly as written.
    ask: str

    def validate(self) -> None:
        """A half-written need is worse than none: it is spoken.

        `opener` and `ask` go to a prospect verbatim, so a blank one is not a missing feature,
        it is a sentence with a hole in it — "I had a quick look at acme.dev. , ." Caught at
        publish time, which is the only moment anybody is looking.
        """
        if not self.signals or not any(s.strip() for s in self.signals):
            raise SpecError("a need with no signals can never match anything")
        for field_name in ("means", "opener", "ask"):
            if not getattr(self, field_name).strip():
                raise SpecError(
                    f"need {self.signals[0]!r} has no {field_name}; it is said out loud, so it "
                    f"cannot be blank"
                )

    def score(self, text: str) -> int:
        """How strongly this need shows up in a piece of text."""
        lowered = text.lower()
        return sum(1 for signal in self.signals if signal and signal.lower() in lowered)

    def narrow(self, fact: str) -> str:
        """The smallest part of a fact that still carries this need.

        TELLING A SMALL MODEL "DO NOT LIST IT" WHILE SHOWING IT A LIST DOES NOT WORK. Research
        facts arrive as `label: item, item, item` — eight technologies, five job titles — and a
        1.5B model handed that list plus a prohibition reads the list out anyway. It is not
        disobeying; the list is the most concrete thing in its context.

        So the list never reaches the prompt. This keeps only the items that carry the signal,
        at most two of them, which is also what a person would have noticed:

            "Currently hiring (4 open roles): Production Associate, Product Manager,
             Research and Development Engineer, Sales and Marketing Specialist"
                -> "Research and Development Engineer"

            "Technology on their site: express, rails, aws, azure, snowflake, react, java"
                -> "aws and azure"
        """
        _, separator, body = fact.partition(": ")
        body = body if separator else fact
        items = [part.strip() for part in re.split(r"[,;]", body) if part.strip()]
        if len(items) <= 1:
            return body.strip()
        # Fall back to the whole list only when nothing in it matched — which cannot happen on
        # the path that calls this, but a `Need` is tenant data and this must not raise.
        hits = [item for item in items if self.score(item)] or items
        return hits[0] if len(hits) == 1 else " and ".join(hits[:2])


@dataclass(frozen=True, slots=True)
class TourStop:
    """One place the agent takes a buyer during the demo.

    THE GUIDED TOUR IS THE DEMO, and it points the opposite way from research. The research
    browser reads the BUYER's website to understand them; this one drives the SELLER's product
    to show it. Conflating the two is how a demo ends up narrating the prospect's own homepage
    back at them, which is what this codebase did before someone pointed it out.
    """

    url: str
    label: str
    #: What this page demonstrates. Handed to the model as narration context so it talks about
    #: what is on the screen rather than about what it imagines is on the screen.
    shows: str
    #: A phrase to scroll into view first, so the screen shows the thing being said.
    scroll_to: str = ""
    #: Buyer concerns this stop answers, so the agent can go where the question went.
    answers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Competitor:
    """Somebody the buyer is also weighing up, and what is honestly true about the difference.

    POSITIONING IS THE TENANT'S CLAIM, NEVER THE MODEL'S. A comparison table generated by a
    language model is a defamation risk with a grid layout. Every line here was written by the
    seller and is quoted verbatim, and `positioning` — something fair about the competitor — is
    required, because an agent that cannot say what a rival is good at is not believed about
    the rest of the table either.
    """

    name: str
    positioning: str
    #: (dimension, what is true of us), shown beside `positioning`.
    against: tuple[tuple[str, str], ...] = ()


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
    name: str = "Nadia"
    company: str = "Rainmaker"
    persona: str = "a direct, well-prepared account executive who does not oversell"
    objective: str = (
        "Understand what the prospect is trying to fix, and find out whether it is worth "
        "putting a person on the next call."
    )
    voice: str = DEFAULT_VOICE
    portrait: str = "/agent/nadia.jpg"

    # ── what it may say ─────────────────────────────────────────────────
    knowledge: tuple[Fact, ...] = ()
    pricing: tuple[Tier, ...] = ()
    pricing_note: str = "Exact numbers come from a person."
    #: What a unit costs per, and in what currency. Shown and spoken; never invented.
    pricing_period: str = "month"
    currency: str = "usd"
    #: Off by default. A discount is something a seller decides, not a default a platform
    #: applies on their behalf.
    annual_discount_pct: int = 0

    #: What the front door asks for before the call starts.
    #:
    #: A B2B AGENT AND A DENTIST DO NOT ASK THE SAME QUESTIONS. Nadia needs a work address —
    #: the domain is where the research browser points, and it is the qualifying question.
    #: A dental practice asking a patient for their work email and their company is asking a
    #: person in pain to identify their employer, and the defaults must not make that the
    #: normal case just because the first tenant is a software company.
    intake: Intake = field(default_factory=lambda: Intake())

    #: Why people buy this, and how to spot each reason in what research found. Without these
    #: the agent can read a website and cannot do anything with what it read.
    needs: tuple[Need, ...] = ()

    #: Where the agent takes a buyer to show the product.
    tour: tuple[TourStop, ...] = ()
    #: Who else they are looking at, and what is fairly true about the difference.
    competitors: tuple[Competitor, ...] = ()

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
        for need in self.needs:
            need.validate()
        if len(self.pricing) > MAX_TIERS:
            raise SpecError(f"{len(self.pricing)} tiers is more than a caller can hold")
        for tier in self.pricing:
            if tier.unit_amount < 0:
                raise SpecError(f"{tier.name} has a negative amount")
            if tier.min_seats < 1:
                raise SpecError(f"{tier.name} has a minimum below one seat")
        if not 0 <= self.annual_discount_pct <= 90:
            raise SpecError("an annual discount outside 0-90% is a typo, not a discount")
        for stop in self.tour:
            if not stop.url.startswith(("http://", "https://")):
                raise SpecError(f"tour stop {stop.label!r} needs an http(s) URL")
            if not stop.shows.strip():
                raise SpecError(
                    f"tour stop {stop.label!r} does not say what it shows; the agent would be "
                    "narrating a page it cannot see"
                )
        for rival in self.competitors:
            if not rival.positioning.strip():
                raise SpecError(
                    f"{rival.name} has no positioning line. An agent that cannot say what a "
                    "competitor is good at is not believed about the rest of the table"
                )
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
                {
                    "name": t.name, "price": t.price, "detail": t.detail,
                    "unit_amount": t.unit_amount, "min_seats": t.min_seats,
                    "unit_name": t.unit_name, "unit_plural": t.unit_plural,
                }
                for t in self.pricing
            ],
            "pricing_period": self.pricing_period,
            "currency": self.currency,
            "annual_discount_pct": self.annual_discount_pct,
            "tour": [
                {
                    "url": t.url, "label": t.label, "shows": t.shows,
                    "scroll_to": t.scroll_to, "answers": list(t.answers),
                }
                for t in self.tour
            ],
            "competitors": [
                {
                    "name": c.name, "positioning": c.positioning,
                    "against": [list(pair) for pair in c.against],
                }
                for c in self.competitors
            ],
            "pricing_note": self.pricing_note,
            "needs": [
                {
                    "signals": list(n.signals),
                    "means": n.means,
                    "opener": n.opener,
                    "ask": n.ask,
                }
                for n in self.needs
            ],
            "intake": self.intake.as_dict(),
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
            name=raw.get("name", "Nadia"),
            company=raw.get("company", "Rainmaker"),
            persona=raw.get("persona", AgentSpec.persona),
            objective=raw.get("objective", AgentSpec.objective),
            voice=raw.get("voice", DEFAULT_VOICE),
            portrait=raw.get("portrait", "/agent/nadia.jpg"),
            knowledge=tuple(
                Fact(text=f["text"], source=f.get("source", ""), topic=f.get("topic", ""))
                for f in raw.get("knowledge", [])
            ),
            pricing=tuple(
                Tier(
                    name=t["name"], price=t["price"], detail=t.get("detail", ""),
                    unit_amount=int(t.get("unit_amount", 0)),
                    min_seats=int(t.get("min_seats", 1)),
                    unit_name=t.get("unit_name", "seat"),
                    unit_plural=t.get("unit_plural", ""),
                )
                for t in raw.get("pricing", [])
            ),
            pricing_period=raw.get("pricing_period", "month"),
            currency=raw.get("currency", "usd"),
            annual_discount_pct=int(raw.get("annual_discount_pct", 0)),
            tour=tuple(
                TourStop(
                    url=t["url"], label=t.get("label", ""), shows=t.get("shows", ""),
                    scroll_to=t.get("scroll_to", ""), answers=tuple(t.get("answers", ())),
                )
                for t in raw.get("tour", [])
            ),
            competitors=tuple(
                Competitor(
                    name=c["name"], positioning=c.get("positioning", ""),
                    against=tuple((p[0], p[1]) for p in c.get("against", []) if len(p) == 2),
                )
                for c in raw.get("competitors", [])
            ),
            pricing_note=raw.get("pricing_note", ""),
            needs=tuple(
                Need(
                    signals=tuple(n.get("signals", ())),
                    means=n.get("means", ""),
                    opener=n.get("opener", ""),
                    ask=n.get("ask", ""),
                )
                for n in raw.get("needs", [])
            ),
            intake=Intake(
                ask_company=bool((raw.get("intake") or {}).get("ask_company", True)),
                require_work_email=bool(
                    (raw.get("intake") or {}).get("require_work_email", True)
                ),
            ),
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
