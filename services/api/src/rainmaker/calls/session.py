"""One live call: who the agent is, what it may claim, and what it does when asked for a human.

THE PROMPT IS THE PRODUCT SURFACE. Everything the prospect experiences that is not latency comes
from this file — the persona, the rules the model cannot talk its way out of, and the facts it is
allowed to state. A sales agent that invents a price is not a charming bug; it is a quote the
company has to honour or explain away, so the rules below are ordered with that first.

WHAT IS GROUNDED AND WHAT IS NOT. The agent is given two bodies of fact: what Rainmaker is (a
constant in this file, because the product's own claims should not be inventable) and what the
research agent actually read on the prospect's website. Nothing else is admissible. The model is
told explicitly that anything absent from both is something to offer to check rather than to
answer — the failure this prevents is the agent confidently reciting a competitor's pricing page
back at the person who works there.

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

#: What the agent sells. A CONSTANT RATHER THAN A PROMPT INPUT, because the product's own claims
#: are the ones most likely to be embellished and least excusable when they are. Every line here
#: is true of this repository.
PRODUCT_FACTS = """
Rainmaker is a sales platform with three parts: a research agent that reads a company's public
website, an AI account executive that runs the first call, and a pipeline console for the reps.

What is unusual about it: the console is offline-first. Every edit is saved on the rep's own
device immediately and synced afterwards, so it keeps working with no internet at all — on a
train, in a basement, on a bad hotel connection. Two reps editing the same deal while both
offline end up in agreement automatically, because the data is a CRDT rather than a database row.

Everything runs locally and is open source. The language model, the voice and the research agent
all run on the customer's own hardware, so recorded calls and pipeline data never leave it. There
is no per-call cost and no third-party AI vendor in the contract.

Pricing is not published. It depends on seats and on whether it runs in the customer's own
environment, and it is quoted by a person.
""".strip()

#: The one line the agent says when someone asks for a human, verbatim, every time.
HANDOFF_LINE = (
    "Of course — let me bring someone in. I'll pass along everything we covered so you don't "
    "have to repeat yourself."
)


@dataclass(slots=True)
class AgentProfile:
    """Who is on the call from the seller's side."""

    name: str = "Liv"
    company: str = "Rainmaker"
    persona: str = "a direct, well-prepared account executive who does not oversell"
    objective: str = (
        "Understand what the prospect is trying to fix, and find out whether it is worth "
        "putting a person on the next call."
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
    sourced("published_price_usd", "Their published price, in dollars")

    tech = [t.get("name") for t in enrichment.get("tech") or [] if t.get("name")]
    if tech:
        out.append("Technology on their site: " + ", ".join(tech[:8]))

    roles = [h.get("title") for h in enrichment.get("hiring") or [] if h.get("title")]
    if roles:
        out.append(f"Currently hiring ({len(roles)} open roles): " + ", ".join(roles[:5]))

    for signal in (enrichment.get("signals") or [])[:4]:
        detail = signal.get("detail")
        text = detail.get("value") if isinstance(detail, dict) else None
        kind = str(signal.get("kind", "")).replace("_", " ")
        if text or kind:
            out.append(f"Buying signal ({kind}): {text}" if text else f"Buying signal: {kind}")

    return out[:limit]


def build_system_prompt(profile: AgentProfile, prospect: Prospect) -> str:
    """Assemble the system message.

    Order is deliberate: identity, then the rules, then what may be claimed, then the goal. The
    call rules sit ABOVE everything configurable because an operator prompt that says "be
    thorough" would otherwise produce an agent that reads a paragraph down the line, and no
    amount of configuration should be able to do that.
    """
    parts = [
        f"You are {profile.name}, {profile.persona}, at {profile.company}. "
        f"You are an AI, and you have already said so at the start of this call.",
        CALL_RULES,
        "About " + profile.company + ", the only claims you may make about it:\n\n" + PRODUCT_FACTS,
    ]
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
    ) -> None:
        self.pipeline = pipeline
        self.stt = stt
        self.profile = profile or AgentProfile()
        self.prospect = prospect or Prospect()
        self.history: list[dict[str, str]] = []
        self.transcript: list[dict[str, Any]] = []
        self.handed_off = False

    # ── prompt ──────────────────────────────────────────────────────────
    @property
    def system_prompt(self) -> str:
        return build_system_prompt(self.profile, self.prospect)

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
        self, text: str, *, budget_hints: dict[str, float] | None = None
    ) -> AsyncIterator[TurnEvent]:
        """Answer one thing the prospect said, whether they typed it or spoke it.

        TYPED AND SPOKEN INPUT TAKE THE SAME PATH ON PURPOSE. The only difference upstream is
        who produced the string — the browser's recogniser or a keyboard — and collapsing them
        here means the mode that is easier to test is the same one that runs live.
        """
        text = text.strip()
        if not text:
            return

        if _wants_human(text):
            async for event in self._hand_off(text):
                yield event
            return

        self.stt.offer(text, final=True)
        result = None
        async for event in self.pipeline.stream_turn(_no_audio(), self.context()):
            if isinstance(event, Finished):
                result = event.result
                for stage, ms in (budget_hints or {}).items():
                    result.budget.adopt(stage, ms)
            yield event

        if result is not None:
            reply = result.response.strip()
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
        self.handed_off = True
        self.record("prospect", text)
        self.record("agent", HANDOFF_LINE)
        self.history.append({"role": "user", "content": text})
        self.history.append({"role": "assistant", "content": HANDOFF_LINE})
        log.info("handoff requested: %r", text)

        first = True
        async for clip in _say(self.pipeline.tts, HANDOFF_LINE):
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
                response=HANDOFF_LINE,
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
