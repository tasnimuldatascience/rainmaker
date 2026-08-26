"""One live call: who the agent is, what it may claim, and what it does when asked for a human.

THE PROMPT IS THE PRODUCT SURFACE. Everything the prospect experiences that is not latency comes
from this file — the persona, the rules the model cannot talk its way out of, and the facts it is
allowed to state. A sales agent that invents a price is not a charming bug; it is a quote the
company has to honour or explain away, so the rules below are ordered with that first.

WHAT IS GROUNDED AND WHAT IS NOT. The agent is given two bodies of fact: what its own
`AgentSpec` says it may claim, and what the research agent actually read on the prospect's
website. Nothing else is admissible. The model is told explicitly that anything absent from both
is something to offer to check rather than to answer.

THE FIRST OF THOSE USED TO BE A CONSTANT IN THIS FILE, and moving it was the change that turned
a demo into a product. Rainmaker sells this agent to other businesses; each of them points it at
their own buyers with their own claims, so what an agent may say has to be a row someone edits
rather than a string someone deploys. Liv is now simply the first row — see `agents/store.py`.

Which raises the stake on grounding rather than lowering it. When our own agent invented a fact
it embarrassed us; when a customer's agent invents their refund policy, the sentence in the
complaint is "the vendor's AI said it".

HANDOFF IS NOT LEFT TO THE MODEL. `_wants_human` runs before generation, and when it fires the
agent says one fixed line and stops. Asking a 1.5B model to reliably abandon its objective is a
bet, and the thing being bet is the single worst failure this product has: talking over someone
who has explicitly asked for a person.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ..agents.spec import AgentSpec, Guardrails
from .naming import clean_company_name  # noqa: F401 — re-exported for callers
from .pipeline import (
    CallPipeline,
    Clip,
    Finished,
    Spoke,
    TextToSpeech,
    TurnEvent,
    _wants_human,
)
from .providers import ClientSpeechToText

log = logging.getLogger("rainmaker.calls.session")

#: How many previous turns travel with the prompt. Enough that the agent remembers what was just
#: discussed; short enough that prefill stays under the latency budget. Each turn is roughly 60
#: tokens, so eight is ~500 tokens of history on top of ~600 of rules and facts.
HISTORY_TURNS = 8

#: Rules every agent gets regardless of configuration. These are not style preferences: each one
#: is a failure mode that makes an agent unusable on a live call.
CALL_RULES = """
You are on a LIVE VOICE CALL. Follow these rules absolutely:
- One or two sentences. Never more. The other person is listening, not reading, and cannot skim.
- Never use bullet points, numbered lists, markdown, headings or emoji. They cannot be heard.
- Write numbers the way they are spoken: "forty dollars a seat", not "$40/seat".
- Never invent a price, a customer name, a date, a certification or a capability. If it is not
  in the information below, say you will find out and offer to have it sent.
- Quote every price and figure EXACTLY as it is written below. Never average a range, never
  round, never say "about" a price. A quoted price is a commitment.
- Ask one question at a time, and only when it moves the conversation forward.
- Answer the question that was actually asked before you say anything else about the product.
- When the notes below contain something specific about their company, refer to it. A reply
  that would fit any company on earth is a reply that persuades nobody.
- Do not repeat back what the other person just said. Answer it.
- Never claim to be human, and never claim to have used the product yourself.
- Anything below marked "a model's reading" is not confirmed. You may raise it as a question
  ("am I right that..."), never as a statement of fact about their company.
""".strip()

#: Kept as the default for a session built without a spec — the tests, and the plain `start`
#: call that exercises the voice path. A real call always carries one.
HANDOFF_LINE = Guardrails().handoff_line


@dataclass(slots=True)
class AgentProfile:
    """Who is on the call from the seller's side.

    A thin view over `AgentSpec`, kept because the agenda retargets `objective` per step and a
    frozen spec cannot be retargeted. Everything else here is copied from the spec at the start
    of the call and never re-read, so publishing mid-call cannot change the agent underneath the
    person on it.
    """

    name: str = "Liv"
    company: str = "Rainmaker"
    persona: str = "a direct, well-prepared account executive who does not oversell"
    objective: str = (
        "Understand what the prospect is trying to fix, and find out whether it is worth "
        "putting a person on the next call."
    )

    @classmethod
    def of(cls, spec: AgentSpec) -> AgentProfile:
        return cls(
            name=spec.name,
            company=spec.company,
            persona=spec.persona,
            objective=spec.objective,
        )


@dataclass(slots=True)
class Prospect:
    """Who is on the other side, and what the research agent actually read about them.

    `facts` is the enrichment rendered as plain lines. Deliberately plain: passing the model a
    JSON blob invites it to quote field names out loud, and "your pricing_model is seat_based"
    is not a sentence a person says.
    """

    company: str = ""
    domain: str = ""
    facts: list[str] = field(default_factory=list)

    def render(self) -> str:
        if not self.facts:
            return ""
        head = f"What we know about {self.company or self.domain}, from their own website:"
        return head + "\n" + "\n".join(f"- {fact}" for fact in self.facts)


#: A published price outside this range is an extraction artifact, not a price. Stripe's pricing
#: page yielded "0.01" — the cents half of "2.9% + $0.30" — and the agent was handed it as
#: "their published price, in dollars: 0.01". Reading that back to someone who works at Stripe
#: is worse than knowing nothing about their pricing, so it is dropped rather than hedged.
PLAUSIBLE_PRICE = (1.0, 100_000.0)

#: Longer than this and it is a paragraph that happened to sit near a careers link, not a job
#: title. Observed: "AI is replatforming the global economy, Products and pricing Pricing Atlas
#: Authorizatio" arriving as an open role.
MAX_JOB_TITLE_CHARS = 48


def _sourced_price(enrichment: dict[str, Any], out: list[str]) -> None:
    node = enrichment.get("published_price_usd")
    if not isinstance(node, dict):
        return
    try:
        price = float(node.get("value"))
    except (TypeError, ValueError):
        return
    low, high = PLAUSIBLE_PRICE
    if low <= price <= high:
        out.append(f"Their published price, in dollars: {price:g}")


def _job_title(hiring: Any) -> str:
    """One open role, or nothing if what was scraped is not a job title.

    The research agent takes titles from link text on a careers page, and careers pages are full
    of link text that is not a job title. A wrong fact spoken confidently to the person who works
    there is the most expensive kind of wrong this product has.
    """
    if not isinstance(hiring, dict):
        return ""
    title = str(hiring.get("title") or "").strip()
    if not (2 <= len(title) <= MAX_JOB_TITLE_CHARS):
        return ""
    # A title is a noun phrase. Sentence punctuation means a sentence came along with it.
    if any(mark in title for mark in (". ", ", ", ";", "!", "?")):
        return ""
    return title


#: Values the research agent uses to mean "we could not tell". Passing them to the model as
#: facts produces an agent that says "your company size is unknown" out loud.
_EMPTY_VALUES = {"unknown", "none", "n/a", ""}


def facts_from_enrichment(enrichment: dict[str, Any], *, limit: int = 12) -> list[str]:
    """Flatten a research result into lines the model can quote.

    PROVENANCE SURVIVES THE FLATTENING, and that is the point of the function. The research
    agent is careful to distinguish what a page said from what a model concluded, and dropping
    that distinction here would hand the agent a list in which both look identical — which is
    precisely the failure the provenance tracking exists to prevent. Inferred values are marked
    as such, and `CALL_RULES` tells the agent to hedge them rather than assert them.

    Unknowns are dropped entirely rather than passed through: `size = unknown` is not a fact
    about the prospect, and an agent given it will eventually say it.
    """
    out: list[str] = []

    def sourced(key: str, label: str) -> None:
        node = enrichment.get(key)
        if not isinstance(node, dict):
            return
        value = node.get("value")
        if value is None or str(value).strip().lower() in _EMPTY_VALUES:
            return
        pretty = str(value).replace("_", " ")
        hedge = " (a model's reading, not stated outright)" if node.get(
            "provenance"
        ) == "inferred" else ""
        out.append(f"{label}: {pretty}{hedge}")

    sourced("name", "Company name")
    sourced("description", "What they do")
    sourced("industry", "Industry")
    sourced("size", "Company size")
    sourced("pricing_model", "How they charge")
    _sourced_price(enrichment, out)

    tech = [t.get("name") for t in enrichment.get("tech") or [] if t.get("name")]
    if tech:
        out.append("Technology on their site: " + ", ".join(tech[:8]))

    roles = [r for r in (_job_title(h) for h in enrichment.get("hiring") or []) if r]
    if roles:
        out.append(f"Currently hiring ({len(roles)} open roles): " + ", ".join(roles[:5]))

    for signal in (enrichment.get("signals") or [])[:4]:
        detail = signal.get("detail")
        text = detail.get("value") if isinstance(detail, dict) else None
        kind = str(signal.get("kind", "")).replace("_", " ")
        if text or kind:
            out.append(f"Buying signal ({kind}): {text}" if text else f"Buying signal: {kind}")

    return out[:limit]


def build_system_prompt(
    profile: AgentProfile, prospect: Prospect, spec: AgentSpec | None = None
) -> str:
    """Assemble the system message.

    Order is deliberate: identity, then the rules, then what may be claimed, then the goal. The
    call rules sit ABOVE everything configurable because an operator prompt that says "be
    thorough" would otherwise produce an agent that reads a paragraph down the line, and no
    amount of configuration should be able to do that.
    """
    claims = spec.knowledge_text() if spec else ""
    parts = [
        f"You are {profile.name}, {profile.persona}, at {profile.company}. "
        f"You are an AI, and you have already said so at the start of this call.",
        CALL_RULES,
    ]
    if claims:
        # WHAT THEY SELL, IN ONE LINE, BEFORE THE DETAIL. A list of facts leaves a small model to
        # infer the product from them, and what it infers is "a platform" — the generic noun that
        # fits anything and sells nothing. The first fact is the positioning line, so it is
        # promoted to a headline rather than left as item one of nine.
        headline = spec.knowledge[0].text if spec and spec.knowledge else ""
        sells = f"What {profile.company} sells, in one line: {headline}\n\n" if headline else ""
        parts.append(
            f"{sells}About {profile.company}, the ONLY claims you may make about it. If "
            f"something is not below, say you will find out:\n\n{claims}"
        )
    else:
        # An agent with no knowledge is not a broken agent, it is a new one. It may still
        # discover, book and hand over; it simply may not describe a product.
        parts.append(
            f"You have no product information for {profile.company} on this call. Do not "
            "describe the product. Ask questions, and offer to have a colleague follow up."
        )
    prospect_facts = prospect.render()
    if prospect_facts:
        parts.append(
            prospect_facts
            + "\n\nUse these to be specific about THEIR situation. Do not state anything about "
            "them that is not listed above."
        )
    parts.append(f"Your goal on this call: {profile.objective}")
    return "\n\n".join(parts)


class CallSession:
    """A single call: the pipeline, the history, and the prospect it is about.

    One instance per WebSocket. Not shared and not reused — the history is the call, and a
    session that outlived its socket would greet the next prospect mid-conversation.
    """

    def __init__(
        self,
        pipeline: CallPipeline,
        stt: ClientSpeechToText,
        *,
        profile: AgentProfile | None = None,
        prospect: Prospect | None = None,
        spec: AgentSpec | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.stt = stt
        #: THE SPEC IS HELD, NOT RE-READ. A publish while this call is running must not change
        #: the agent underneath the person on it — an agent whose prices move between two of its
        #: own sentences is worse than one with stale prices.
        self.spec = spec
        self.profile = profile or (AgentProfile.of(spec) if spec else AgentProfile())
        self.prospect = prospect or Prospect()
        self.history: list[dict[str, str]] = []
        self.transcript: list[dict[str, Any]] = []
        self.handed_off = False

    # ── prompt ──────────────────────────────────────────────────────────
    @property
    def system_prompt(self) -> str:
        return build_system_prompt(self.profile, self.prospect, self.spec)

    @property
    def handoff_line(self) -> str:
        """What she says when someone asks for a person. The tenant's wording, our rule."""
        return self.spec.guardrails.handoff_line if self.spec else HANDOFF_LINE

    def context(self) -> dict[str, Any]:
        return {
            "system": self.system_prompt,
            "history": self.history[-HISTORY_TURNS * 2 :],
            "company": self.prospect.company,
        }

    # ── the call ────────────────────────────────────────────────────────
    async def open(self) -> AsyncIterator[TurnEvent]:
        """Deliver the disclosure. Nothing else may happen before this.

        The line is spoken, not merely logged: a disclosure the prospect cannot hear is not a
        disclosure. `CallPipeline.open` refuses to let a turn run until this has happened.

        IT ENDS WITH `Finished` LIKE ANY OTHER TURN, and that is not symmetry for its own sake.
        Without a terminator the console cannot tell when the greeting has finished arriving,
        so it either enables the microphone too early — and the agent hears itself — or guesses
        with a timer. The opening is a turn the agent takes; it ends the way turns end.
        """
        from .pipeline import LatencyBudget, Stage, TurnResult

        budget = LatencyBudget()
        spoken = await self.pipeline.open()
        self.record("agent", spoken)
        self.history.append({"role": "assistant", "content": spoken})

        first = True
        async for clip in _say(self.pipeline.tts, spoken):
            if first:
                first = False
                budget.mark(Stage.TTS)
            yield Spoke(clip)

        yield Finished(
            TurnResult(transcript="", response=spoken, budget=budget, handoff_requested=False)
        )

    async def respond(
        self,
        text: str,
        *,
        budget_hints: dict[str, float] | None = None,
        internal: bool = False,
    ) -> AsyncIterator[TurnEvent]:
        """Answer one thing the prospect said, whether they typed it or spoke it.

        TYPED AND SPOKEN INPUT TAKE THE SAME PATH ON PURPOSE. The only difference upstream is
        who produced the string — the browser's recogniser or a keyboard — and collapsing them
        here means the mode that is easier to test is the same one that runs live.
        """
        text = text.strip()
        if not text:
            return

        # A stage direction is never a request for a human, and running the check on one means
        # a direction containing the word "person" hands the call over.
        if not internal and _wants_human(text):
            async for event in self._hand_off(text):
                yield event
            return

        self.stt.offer(text, final=True)
        result = None
        async for event in self.pipeline.stream_turn(
            _no_audio(), self.context(), announce=not internal
        ):
            if isinstance(event, Finished):
                result = event.result
                for stage, ms in (budget_hints or {}).items():
                    result.budget.adopt(stage, ms)
            yield event

        if result is not None:
            reply = result.response.strip()
            # The transcript is what was SAID. A stage direction was not said, so it is kept out
            # of it — but it stays in `history`, because the model's next turn needs the reply
            # it just gave to have something to follow.
            if not internal:
                self.record("prospect", text)
            self.record("agent", reply, budget=result.budget.report())
            self.history.append({"role": "user", "content": text})
            if reply:
                self.history.append({"role": "assistant", "content": reply})

    async def _hand_off(self, text: str) -> AsyncIterator[TurnEvent]:
        """Someone asked for a person. Say one line and stop selling.

        Not routed through the model. See the module docstring: this is the one behaviour that
        must not depend on a small model choosing to comply.
        """
        from .pipeline import LatencyBudget, Stage, TurnResult

        budget = LatencyBudget()
        line = self.handoff_line
        self.handed_off = True
        self.record("prospect", text)
        self.record("agent", line)
        self.history.append({"role": "user", "content": text})
        self.history.append({"role": "assistant", "content": line})
        log.info("handoff requested: %r", text)

        first = True
        async for clip in _say(self.pipeline.tts, line):
            if first:
                first = False
                # The fixed line is still synthesised, so it still has a first-audio latency.
                # Reporting zero for it would make the one turn that skips the model look like
                # the fastest turn in the call.
                budget.mark(Stage.TTS)
            yield Spoke(clip)
        yield Finished(
            TurnResult(
                transcript=text,
                response=line,
                budget=budget,
                handoff_requested=True,
            )
        )

    def record(self, who: str, text: str, **extra: Any) -> None:
        if text:
            self.transcript.append({"who": who, "text": text, **extra})


async def _no_audio() -> AsyncIterator[bytes]:
    """The audio stream the pipeline expects, empty because transcription already happened.

    `ClientSpeechToText` reads from a queue the socket fills, so there are no bytes to carry.
    The parameter stays on the interface for the day an engine runs server-side.
    """
    return
    yield b""  # pragma: no cover — makes this an async generator


async def _say(tts: TextToSpeech, text: str) -> AsyncIterator[Clip]:
    """Synthesise a fixed line — the disclosure, the handoff.

    Goes through `clips` like everything else rather than a separate path, so the fixed lines
    are chunked, timed and animated exactly the way a generated reply is.
    """

    async def once() -> AsyncIterator[str]:
        yield text

    async for clip in tts.clips(once()):
        yield clip
