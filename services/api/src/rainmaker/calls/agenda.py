"""The call itself: what Liv is trying to do right now, and what she is allowed to do about it.

THIS IS THE FILE THAT MAKES IT A PRODUCT RATHER THAN A CHATBOT. A chatbot answers whatever is in
front of it. A demo call has somewhere to get to — understand the business, show the thing
against their own situation, get a meeting in the diary, quote a price — and something in the
system has to hold that shape while the conversation wanders.

WHO DECIDES WHAT, and it is worth being blunt because most systems built like this put it the
other way round:

    the graph decides   which step the call is on, which tools fire, what goes on screen,
                        and every sentence where being wrong costs something real — the
                        disclosure, the slot offer, the booking confirmation, the handoff.
    the model decides   the wording of everything else: the greeting, the discovery question,
                        the narration over a page, the pitch.

Qwen2.5-1.5B is a good enough writer for the second column and nowhere near reliable enough for
the first. A model that phrases a question awkwardly costs a moment. A model that decides on its
own to confirm a meeting books nothing and promises everything.

HOW A STEP ENDS. Three signals, cheapest first: an explicit intent in what the prospect said
("how much is it", "let's book something"), the model volunteering a marker, and a turn budget
that moves things on regardless. The budget is not a fallback nobody hits — small models are
happy to discover forever, and a demo that never reaches the price is a demo that failed.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from ..agents.quoting import build_quote, seats_from_conversation, unit_words
from .intake import Contact
from .pipeline import Finished, LatencyBudget, Spoke, TurnEvent, TurnResult, _wants_human
from .session import CallSession, Prospect, clean_company_name, facts_from_enrichment
from .session import _say as speak_line

log = logging.getLogger("rainmaker.calls.agenda")


class _Granted:
    """The tool layer, narrowed to what THIS agent was granted.

    AN ALLOW-LIST CHECKED AT CALL TIME, not only at publish. A grant can be revoked while a call
    is running — a tenant discovering their agent is booking meetings it should not — and the
    answer has to be the current one rather than the one from when the socket opened.

    It refuses rather than filtering silently: an agenda that asks for a tool it cannot have has
    a bug, and swallowing that produces a call where a step quietly does nothing.
    """

    def __init__(self, tools: Tools, spec: Any):
        self._tools = tools
        self._spec = spec

    def has(self, qualified: str) -> bool:
        if self._spec is not None and not self._spec.may_use(qualified):
            return False
        return self._tools.has(qualified)

    async def call(self, qualified: str, arguments: dict[str, Any] | None = None, **kw: Any):
        if self._spec is not None and not self._spec.may_use(qualified):
            raise PermissionError(
                f"{self._spec.tenant}/{self._spec.agent_id} was not granted "
                f"{qualified.split('.', 1)[0]!r}"
            )
        return await self._tools.call(qualified, arguments, **kw)


class Tools(Protocol):
    """What the agenda needs from the tool layer.

    A protocol rather than `ToolBroker` so the agenda can be tested without spawning four
    subprocesses, and so a deployment that routes tools somewhere else — a hosted gateway, a
    queue — does not have to subclass anything.
    """

    def has(self, qualified: str) -> bool: ...

    async def call(self, qualified: str, arguments: dict[str, Any] | None = None) -> Any: ...


class Step(StrEnum):
    """Where the call is. Order is the happy path; jumps are allowed and normal.

    THIS IS A CLOSING FUNNEL, NOT A LEAD-GEN ONE, and that is a recent correction. The steps
    used to end at "book a meeting", which optimises for calendar slots. The agent exists so a
    buyer can talk at any hour without waiting for a rep, and a buyer who is ready at eleven at
    night should be able to finish — understand it, compare it, see their number, and pay.
    Booking a human is the ESCALATION now, not the goal.
    """

    RESEARCHING = "researching"
    OPENING = "opening"
    DISCOVERY = "discovery"
    GUIDE = "guide"
    COMPARE = "compare"
    QUOTE = "quote"
    CLOSE = "close"
    PAY = "pay"
    BOOKING = "booking"
    WRAP = "wrap"


# ───────────────────────────────────────────────────────────── what the console shows
@dataclass(slots=True)
class Panel:
    """Something for the stage: a page, a set of facts, slots, a price, a draft.

    The stage is not decoration. The product's claim is that an agent does the reading a rep
    would otherwise do, and the only way to make that claim visible in ninety seconds is to
    show the reading happening.
    """

    kind: str
    data: dict[str, Any]


@dataclass(slots=True)
class Phase:
    """The call moved to a new step. Drives the progress rail in the console."""

    step: Step
    detail: str = ""


AgendaEvent = TurnEvent | Panel | Phase


# ───────────────────────────────────────────────────────────── intents
#: Things a prospect says that should move the call whatever step it is on. Matched before the
#: model is consulted, because these are the moments where guessing is expensive.
_INTENTS: tuple[tuple[Step, re.Pattern[str]], ...] = (
    (
        Step.BOOKING,
        re.compile(
            r"\b(?:talk|speak) to (?:a |an )?(?:human|person|real|someone|sales)"
            r"|\b(?:book|schedule|set up|arrange)\b.{0,20}"
            r"\b(?:call|meeting|demo|time|chat|something|slot|session)\b"
            r"|\bwhen can (?:we|i) (?:speak|talk|meet)\b",
            re.I,
        ),
    ),
    (
        Step.PAY,
        re.compile(
            r"\b(?:i'?ll take it|sign me up|sign up|let'?s do it|i'?m in|take my money"
            r"|how do i (?:buy|pay|start)|can i (?:buy|pay|start)|get started|check ?out)\b",
            re.I,
        ),
    ),
    (
        Step.COMPARE,
        re.compile(
            r"\b(?:compare[ds]?|comparison|versus|vs\.?|instead of|better than|difference"
            r"|what makes you|why (?:you|not|switch)|alternativ\w*)\b",
            re.I,
        ),
    ),
    (
        Step.QUOTE,
        re.compile(
            r"\b(?:how much|what.{0,12}cost|price|pricing|budget|per seat|quote|afford)\b",
            re.I,
        ),
    ),
    (
        Step.GUIDE,
        re.compile(
            r"\b(?:show me|can i see|what does it look like|demo|walk me through"
            r"|how does it work|how do(?:es)? (?:it|you) work)\b",
            re.I,
        ),
    ),
)

#: The model may end a reply with one of these to say "this step is done". A hint, not an
#: instruction — see the module docstring.
_MARKER = re.compile(r"\[\[(?P<step>[a-z_]+)\]\]", re.IGNORECASE)


def read_marker(text: str) -> tuple[str, Step | None]:
    """Split the spoken reply from a transition marker.

    Stripping it here is what stops the synthesiser saying "double bracket booking double
    bracket" out loud, which is the kind of bug that only exists once there is audio.
    """
    match = _MARKER.search(text)
    if not match:
        return text.strip(), None
    cleaned = _MARKER.sub("", text).strip()
    try:
        return cleaned, Step(match.group("step").lower())
    except ValueError:
        return cleaned, None


def detect_intent(text: str) -> Step | None:
    for step, pattern in _INTENTS:
        if pattern.search(text):
            return step
    return None


# ───────────────────────────────────────────────────────────── the steps
@dataclass(slots=True)
class StepPlan:
    """What a step is for, and when it is over."""

    objective: str
    #: Turns to spend here before moving on regardless. Small models discover forever.
    max_turns: int
    next_step: Step | None
    #: Markers the model may use from here. Offering every step invites nonsense.
    transitions: tuple[Step, ...] = ()


PLAN: dict[Step, StepPlan] = {
    Step.OPENING: StepPlan(
        objective=(
            "Greet them by name if you know it, name one specific thing you found on their "
            "site, and ask what prompted them to look at this. Do not pitch yet."
        ),
        max_turns=1,
        next_step=Step.DISCOVERY,
    ),
    Step.DISCOVERY: StepPlan(
        objective=(
            "Find out what they are trying to fix and how big their team is. One question at a "
            "time. Do not describe the product yet."
        ),
        max_turns=3,
        next_step=Step.GUIDE,
        transitions=(Step.GUIDE, Step.QUOTE, Step.COMPARE),
    ),
    Step.GUIDE: StepPlan(
        objective=(
            "You are showing them the product on screen. Say what is on this page and what it "
            "would do for the problem they described. Concrete, not a feature list."
        ),
        max_turns=4,
        next_step=Step.QUOTE,
        transitions=(Step.COMPARE, Step.QUOTE, Step.CLOSE),
    ),
    Step.COMPARE: StepPlan(
        objective=(
            "The comparison is on screen. Be fair about what the alternative is good at, then "
            "say plainly where you differ. Never rubbish a competitor."
        ),
        max_turns=2,
        next_step=Step.QUOTE,
        transitions=(Step.QUOTE, Step.CLOSE, Step.GUIDE),
    ),
    Step.QUOTE: StepPlan(
        objective=(
            "Their number is on screen and you have just said it. Do not repeat the figure. "
            "Ask whether it works for them."
        ),
        max_turns=2,
        next_step=Step.CLOSE,
        transitions=(Step.CLOSE, Step.COMPARE, Step.BOOKING),
    ),
    Step.CLOSE: StepPlan(
        objective=(
            "Ask for the business, once and plainly. If they raise an objection, answer it in "
            "one sentence and ask again. If they need someone internally, offer a time with a "
            "person instead."
        ),
        max_turns=3,
        next_step=Step.WRAP,
        transitions=(Step.PAY, Step.BOOKING, Step.COMPARE),
    ),
    Step.PAY: StepPlan(
        objective=(
            "Checkout is on screen. Tell them it is there and that they can finish it now. Do "
            "not state the amount again and never ask for card details."
        ),
        max_turns=2,
        next_step=Step.WRAP,
        transitions=(Step.WRAP, Step.BOOKING),
    ),
    Step.BOOKING: StepPlan(
        objective="Get one of the offered times agreed. Do not invent times.",
        max_turns=3,
        next_step=Step.WRAP,
        transitions=(Step.WRAP,),
    ),
    Step.WRAP: StepPlan(
        objective="Close warmly in one sentence and say what happens next.",
        max_turns=1,
        next_step=None,
    ),
}


# ───────────────────────────────────────────────────────────── the agenda
class Agenda:
    """Runs one call from the email address to the follow-up draft.

    Wraps `CallSession`, which owns the pipeline and the prompt. The agenda owns the plot.
    """

    def __init__(self, session: CallSession, tools: Tools, contact: Contact):
        self.session = session
        self.tools = _Granted(tools, session.spec)
        self.contact = contact
        self.step: Step = Step.RESEARCHING
        self.turns_in_step = 0
        self.deal_id = f"d-{contact.domain.split('.')[0]}" if contact.domain else "d-inbound"

        #: Pages found on their site that are worth putting on screen, and which have been.
        self.shown: set[str] = set()
        #: The slots most recently offered, so "the first one" means something.
        self.offered: list[dict[str, str]] = []
        self.booking: dict[str, Any] | None = None
        self.enrichment: dict[str, Any] = {}
        self.transcript_lines: list[str] = []
        #: Seats the buyer stated, which beats anything research guessed. "We've got forty reps"
        #: is not small talk, it is the number on the quote.
        self.said_seats: int | None = None
        self.quote: Any = None
        self.checkout: dict[str, Any] | None = None
        #: True once they have asked for a person, so the booking step says the handoff line.
        self.wants_human = False
        #: True once the call has been written down. See `end`.
        self.closed = False

        #: True once the disclosure has actually been delivered.
        self.opened = False
        #: True once the WHOLE opening sequence is done: disclosure, research, greeting.
        self.ready = False
        #: Things said before that happened, held rather than dropped. See `defer`.
        self._pending: list[str] = []

    def defer(self, text: str) -> bool:
        """Hold something the prospect said before the disclosure finished. True if held.

        THE WHOLE OPENING IS UNINTERRUPTIBLE, not just the disclosure. Barge-in cancels the turn
        in flight, which is right for a sentence about pricing and wrong for `begin()`, because
        `begin()` is not a sentence — it is the disclosure, then fifteen seconds of reading their
        website, then a greeting built from what it found.

        Gating on "the disclosure has been said" was the first attempt and looked correct. It
        left a window: the disclosure lands at two seconds and the research runs until fifteen,
        so anyone typing in between cancelled the research. The observed result was a jump
        straight to SHOWING with nothing found, and a model inventing a page it had never
        opened — a worse failure than the one it replaced, because it produced fluent nonsense
        instead of an error.

        (The first version of the bug was cruder: cancelling `session.open()` half way meant the
        disclosure was never delivered and `CallPipeline` correctly refused every turn for the
        rest of the call — `DisclosureError: turn() before open()`.)

        Someone typing over the introduction is being keen, not rude, so their words are held
        here and answered the moment she is ready. Nothing is lost and nothing is said out of
        order.
        """
        if self.ready:
            return False
        self._pending.append(text)
        return True

    # ── the opening move ────────────────────────────────────────────────
    async def begin(self) -> AsyncIterator[AgendaEvent]:
        """Research them, then open the call.

        THE RESEARCH HAPPENS BEFORE THE GREETING and the prospect watches it. That ordering is
        the product: a rep who has read your site before calling is the thing being sold, so
        the demo has to do it in front of you rather than claim it afterwards.
        """
        yield Phase(Step.RESEARCHING, f"reading {self.contact.domain}")

        # THE DISCLOSURE IS THE FIRST THING SHE SAYS, before the holding line and before the
        # research. It used to come after both, so her first words were "give me a moment, I'm
        # having a look at your site" — a sentence spoken by something that had not yet said it
        # was a machine. The rule this project enforces everywhere else is "before anything
        # else", and "anything" includes being helpful.
        async for event in self.session.open():
            yield event
        self.opened = True

        if self.contact.researchable:
            async for event in self._research():
                yield event
        else:
            # A personal address is not a dead end — she just cannot pretend to have looked
            # them up. Researching gmail.com would have her describe Google's marketing site.
            yield Panel(
                "note",
                {
                    "text": "Personal address — nothing to research, so she'll ask instead.",
                    "domain": self.contact.domain,
                },
            )

        async for event in self._enter(Step.OPENING):
            yield event

        # Anything they typed over the introduction, now that it is safe to answer. The flag is
        # set with no `await` between it and the loop's final check, so nothing can be appended
        # into the gap and stranded.
        while self._pending:
            async for event in self.respond(self._pending.pop(0)):
                yield event
        self.ready = True

    async def _research(self) -> AsyncIterator[AgendaEvent]:
        """Enrichment first, then the pages worth showing. Both are optional.

        SHE SAYS SOMETHING BEFORE SHE WAITS. Reading a real website is six page loads over
        someone else's network and takes ten seconds or more; ten seconds of silence after
        someone types their email is the point at which they close the tab. The holding line is
        not filler — it is the only part of this step the prospect can hear.
        """
        async for event in self._say(
            "Give me a moment — I'm having a look at your site now."
        ):
            yield event

        # THE BROWSING IS THE DEMO, SO IT HAS TO BE VISIBLE WHILE IT HAPPENS. Enrichment is one
        # tool call that reads half a dozen pages and returns at the end, so for the fifteen
        # seconds that takes the stage showed nothing at all — the prospect was told she was
        # looking at their site and given an empty box to look at. Their homepage goes up first,
        # in two or three seconds, so "she is on your website" is something you can see rather
        # than something you are asked to believe.
        home = f"https://{self.contact.domain}/"
        yield Panel("browser", {"state": "opening", "url": home, "label": "home"})
        try:
            looked = await self.tools.call(
                "research.browse", {"url": home, "screenshot": True}
            )
            self.shown.add(looked.get("url", home))
            yield Panel(
                "browser",
                {
                    "state": "open",
                    "url": looked.get("url", home),
                    "title": looked.get("title", ""),
                    "label": "reading",
                    "frame": looked.get("frame_jpeg_base64", ""),
                },
            )
        except Exception as exc:  # noqa: BLE001 — a slow homepage is not a failed call
            log.info("could not open %s: %s", home, exc)

        try:
            self.enrichment = await self.tools.call(
                "research.research_company", {"domain": self.contact.domain, "max_pages": 6}
            )
        except Exception as exc:  # noqa: BLE001 — a site that will not load is not a crash
            log.info("research failed for %s: %s", self.contact.domain, exc)
            yield Panel("note", {"text": f"Could not read {self.contact.domain}."})
            return

        facts = facts_from_enrichment(self.enrichment)
        company = clean_company_name(
            (self.enrichment.get("name") or {}).get("value") or "",
            fallback=self.contact.company_guess,
        )
        self.session.prospect = Prospect(
            company=company, domain=self.contact.domain, facts=facts
        )
        yield Panel(
            "facts",
            {
                "company": company,
                "domain": self.contact.domain,
                "facts": facts,
                "pages_read": self.enrichment.get("pages_fetched", []),
                "score": self.enrichment.get("score"),
            },
        )

    # ── a turn ──────────────────────────────────────────────────────────
    async def respond(
        self, text: str, *, budget_hints: dict[str, float] | None = None
    ) -> AsyncIterator[AgendaEvent]:
        """One thing the prospect said, and everything that follows from it.

        `budget_hints` carries the two latency stages only the browser can see, and is threaded
        through rather than dropped: without it, every turn on a call WITH an agenda silently
        loses its transcription timing, and the strip would have shown a faster call than the
        prospect experienced.
        """
        text = text.strip()
        if not text:
            return
        self.transcript_lines.append(f"Prospect: {text}")

        # NOBODY IS IN THE ROOM, so asking for a person cannot mean a transfer. It means a time
        # with one. The fixed line is still said first and the model is still never consulted
        # about whether to agree; what follows it is a diary rather than a promise.
        if _wants_human(text):
            self.wants_human = True
            async for event in self._enter(Step.BOOKING, said=text):
                yield event
            return

        # Anything they said about team size feeds the quote later. Read on every turn, because
        # it is usually mentioned in passing during discovery rather than in answer to a
        # question about seats.
        spec = self.session.spec
        stated = seats_from_conversation(text, unit_words(spec) if spec else ())
        if stated is not None:
            self.said_seats = stated

        # An explicit ask outranks the plan. Someone who asks the price during discovery is
        # telling you what the call is about now.
        #
        # GUIDE RE-ENTERS ITSELF, and the others do not. "Show me another" while already on the
        # tour means the NEXT stop, and a step that only runs on arrival would have answered it
        # by talking about the page already on screen. Asking the price twice, by contrast,
        # should not rebuild the same quote — the model answers that.
        wanted = detect_intent(text) or self._tenant_intent(text)
        if wanted and (wanted is not self.step or wanted is Step.GUIDE):
            async for event in self._enter(wanted, said=text):
                yield event
            return

        self.turns_in_step += 1
        reply = ""
        async for event in self.session.respond(text, budget_hints=budget_hints):
            if isinstance(event, Finished):
                reply = event.result.response
            yield event
        if reply:
            self.transcript_lines.append(f"Liv: {reply}")

        _, marker = read_marker(reply)
        plan = PLAN.get(self.step)
        if plan is None:
            return

        # Cheapest signal first: the model volunteered a marker, or the step ran out of budget.
        if marker in plan.transitions:
            async for event in self._enter(marker):
                yield event
        elif self.turns_in_step >= plan.max_turns and plan.next_step:
            async for event in self._enter(plan.next_step):
                yield event

    # ── moving between steps ────────────────────────────────────────────
    async def _enter(self, step: Step, *, said: str = "") -> AsyncIterator[AgendaEvent]:
        """Arrive at a step: run its tools, put something on screen, then speak."""
        self.step = step
        self.turns_in_step = 0
        plan = PLAN.get(step)
        yield Phase(step, plan.objective[:80] if plan else "")

        if step is Step.OPENING:
            async for event in self._open():
                yield event
            return
        if step is Step.GUIDE:
            async for event in self._guide(said):
                yield event
            return
        if step is Step.COMPARE:
            async for event in self._compare(said):
                yield event
            return
        if step is Step.QUOTE:
            async for event in self._quote(said):
                yield event
            return
        if step is Step.CLOSE:
            async for event in self._close(said):
                yield event
            return
        if step is Step.PAY:
            async for event in self._pay(said):
                yield event
            return
        if step is Step.BOOKING:
            async for event in self._offer_times(said):
                yield event
            return
        if step is Step.WRAP:
            async for event in self._wrap(said):
                yield event
            return

        # Discovery is pure conversation: retarget the model and let it talk.
        async for event in self._speak(said or "(continue)"):
            yield event

    def _retarget(self, step: Step) -> None:
        """Point the prompt at the current step's objective.

        The persona and the grounding rules are constant; only the goal moves. Rebuilding the
        whole prompt per step would let the rules drift between steps, which is exactly where a
        model starts inventing prices.

        A tenant may override the wording of any step's objective — that is what differs between
        selling payments infrastructure and selling dental appointments. The STEPS are not
        theirs to change: a configurable call graph is a config language, a debugger and a
        support burden, and the shape of a first sales call is not what varies between
        businesses.
        """
        plan = PLAN.get(step)
        if not plan:
            return
        spec = self.session.spec
        self.session.profile.objective = (
            spec.objective_for(str(step), plan.objective) if spec else plan.objective
        )

    async def _speak(self, prompt: str) -> AsyncIterator[AgendaEvent]:
        """Let the model say something for the current step."""
        self._retarget(self.step)
        reply = ""
        async for event in self.session.respond(prompt, internal=True):
            if isinstance(event, Finished):
                reply = event.result.response
            yield event
        if reply:
            self.transcript_lines.append(f"Liv: {reply}")

    async def _say(self, line: str) -> AsyncIterator[AgendaEvent]:
        """Say a fixed line — no model involved.

        Used wherever the words carry a commitment: a time, a confirmation, a disclosure.
        """
        self.transcript_lines.append(f"Liv: {line}")
        self.session.record("agent", line)
        self.session.history.append({"role": "assistant", "content": line})
        budget = LatencyBudget()
        first = True
        async for clip in speak_line(self.session.pipeline.tts, line):
            if first:
                first = False
                budget.mark("tts")
            yield Spoke(clip)
        yield Finished(TurnResult(transcript="", response=line, budget=budget))

    def _tenant_intent(self, text: str) -> Step | None:
        """The step a question implies, in THIS tenant's vocabulary.

        `detect_intent` knows how buyers ask in general — "show me", "how much". It cannot know
        that "what have you got available right now" is a request to see the capacity page,
        because "available" is a word about GPUs and this agent could be selling anything.

        THE TENANT ALREADY WROTE THE VOCABULARY DOWN. Every tour stop declares what it answers
        and every competitor has a name, and both were being used only to pick between steps
        once a step had been chosen. Consulted here, they choose the step. Found by driving a
        real call and watching the guide never fire on the one question the agent exists to
        answer.
        """
        spec = self.session.spec
        if spec is None:
            return None
        lowered = text.lower()
        if any(
            topic and topic.lower() in lowered for stop in spec.tour for topic in stop.answers
        ):
            return Step.GUIDE
        if any(rival.name.lower() in lowered for rival in spec.competitors):
            return Step.COMPARE
        return None

    # ── the steps that do something ─────────────────────────────────────
    async def _open(self) -> AsyncIterator[AgendaEvent]:
        """A greeting that proves she read their site. The disclosure already happened."""
        who = self.contact.first_name or "them"
        if not self.session.prospect.facts:
            # The form gave her a name and a company; research found nothing to add. Better to
            # greet with what she has than to imply she looked them up.
            async for event in self._speak(
                f"(Greet {who} by name and ask what prompted them to look at this. You could "
                f"not read anything about {self.contact.company_guess or 'their company'}, so "
                f"do not pretend you did.)"
            ):
                yield event
            return

        # THE MOST SPECIFIC FACT IS HANDED TO HER, not left to be chosen from a list. Given
        # nine facts a 1.5B model greets with "Hello! Nice to meet you." — generically, which
        # wastes the one moment where knowing something specific is worth anything.
        facts = self.session.prospect.facts
        hook = next(
            (f for f in facts if f.startswith(("Technology", "Currently hiring", "Buying signal"))),
            next((f for f in facts if f.startswith("What they do")), ""),
        )
        async for event in self._speak(
            f"(Greet {who} by name, mention this specific thing you found — {hook or 'their site'}"
            " — and ask what prompted them to look at this.)"
        ):
            yield event

    def _next_stop(self, said: str) -> Any:
        """Where to take them next on the product tour.

        Goes where the QUESTION went. A stop declares what it answers, so "does it work
        offline" lands on the page that shows that rather than on stop three of five. A tour
        that always runs in order is a slide deck with extra steps.
        """
        spec = self.session.spec
        stops = [s for s in (spec.tour if spec else ()) if s.url not in self.shown]
        if not stops:
            return None
        lowered = said.lower()
        wanted = [
            s for s in stops if any(topic.lower() in lowered for topic in s.answers if topic)
        ]
        return (wanted or stops)[0]

    async def _guide(self, said: str) -> AsyncIterator[AgendaEvent]:
        """Drive the SELLER's product on screen and talk over it.

        THE OPPOSITE DIRECTION FROM RESEARCH, and the distinction is the whole demo. Research
        opens the BUYER's website to work out who they are. This opens OUR product to show them
        what they would be buying. For a while this code did the first and called it the second,
        so the demo consisted of narrating the prospect's own homepage back at them.
        """
        stop = self._next_stop(said)
        if stop is None:
            # No tour configured, or every stop has been shown. Talk from knowledge rather than
            # opening a page nobody chose.
            async for event in self._speak(
                said or "(Describe what the product would do for the problem they described.)"
            ):
                yield event
            return

        self.shown.add(stop.url)
        yield Panel("browser", {"state": "opening", "url": stop.url, "label": stop.label})
        try:
            looked = await self.tools.call(
                "research.browse",
                {"url": stop.url, "screenshot": True, "scroll_to": stop.scroll_to},
            )
        except Exception as exc:  # noqa: BLE001 - a page that will not load is not a dead call
            log.info("could not open %s: %s", stop.url, exc)
            async for event in self._speak(said or "(continue)"):
                yield event
            return

        yield Panel(
            "browser",
            {
                "state": "open",
                "url": looked.get("url", stop.url),
                "title": looked.get("title", ""),
                "label": stop.label,
                "frame": looked.get("frame_jpeg_base64", ""),
                "scrolled_to": looked.get("scrolled_to", ""),
                # The console scrolls to this in front of the prospect rather than cutting to
                # a still of the destination. A browser being driven looks like a browser being
                # driven; a screenshot of the right part of a page looks like a screenshot.
                "full_page": looked.get("full_page", False),
                "scroll_ratio": looked.get("scroll_ratio", 0),
                "viewport_ratio": looked.get("viewport_ratio", 1),
            },
        )
        excerpt = (looked.get("text") or "")[:700]
        ours = self.session.spec.company if self.session.spec else "your company"
        theirs = self.session.prospect.company or self.contact.company_guess or "their company"
        async for event in self._speak(
            f"(You are screen-sharing {ours}'s OWN product with them: {stop.label}. "
            f"It shows: {stop.shows}. The page reads: {excerpt} "
            f"This page belongs to {ours}, not to {theirs} — it is what they would be buying. "
            f"Say what is on the screen and what it would do about the problem they described. "
            f"Do not read the page out.)"
        ):
            yield event

    async def _compare(self, said: str) -> AsyncIterator[AgendaEvent]:
        """Put the comparison on screen, then be fair about it.

        EVERY LINE IS THE TENANT'S, QUOTED VERBATIM. A comparison table a language model wrote
        about a named competitor is a defamation risk with a grid layout, so the model is given
        the table and told to talk around it rather than asked to produce one.
        """
        spec = self.session.spec
        rivals = list(spec.competitors) if spec else []
        if not rivals:
            async for event in self._speak(
                said or "(They asked how you compare. You have no comparison configured, so "
                "answer from what the product does and offer to have someone follow up.)"
            ):
                yield event
            return

        lowered = said.lower()
        named = [r for r in rivals if r.name.lower() in lowered]
        shown = named or rivals

        yield Panel(
            "comparison",
            {
                "company": spec.company,
                "rivals": [
                    {
                        "name": r.name,
                        "positioning": r.positioning,
                        "against": [{"dimension": d, "ours": o} for d, o in r.against],
                    }
                    for r in shown
                ],
            },
        )
        table = " | ".join(
            f"{r.name}: they are good at {r.positioning}; "
            + "; ".join(f"on {d} we {o}" for d, o in r.against)
            for r in shown
        )
        async for event in self._speak(
            f"(The comparison is on their screen: {table} "
            f"Be fair about what they are good at first, then say plainly where you differ. "
            f"Never rubbish them, and never claim anything not in that table.)"
        ):
            yield event

    async def _quote(self, said: str) -> AsyncIterator[AgendaEvent]:
        """Their number, computed here and spoken by the platform.

        THE FIGURE IS NOT THE MODEL'S. Seats, rate, discount and total are arithmetic over the
        tenant's published pricing, and the sentence that states them is written by `Quote`, the
        way the calendar writes its own times. The model is then told the number is already said
        and asked to do the only part it is good at: ask whether it works.

        `Guardrails.speak_prices` stays false and still means what it meant — the MODEL may not
        state a figure. The platform saying its own computed total is a different act, and it is
        the one that lets a call end in a sale instead of a follow-up.
        """
        spec = self.session.spec
        if spec is None:
            return

        band = ((self.enrichment.get("size") or {}).get("value") or "")
        quote = build_quote(
            spec,
            said_seats=self.said_seats,
            size_band=str(band),
            company=self.session.prospect.company or self.contact.company_guess,
        )
        if quote is None:
            # Tiers with no amounts are legitimate: "Enterprise, quoted". There is nothing to
            # compute, so nothing is put on screen and nobody is given a number.
            yield Panel(
                "pricing",
                {
                    "company": self.session.prospect.company,
                    "tiers": [
                        {"name": t.name, "per_seat": t.price, "for": t.detail}
                        for t in spec.pricing
                    ],
                    "note": spec.pricing_note,
                },
            )
            async for event in self._speak(
                said or "(Pricing is on screen. Say how it works, never a figure, and offer to "
                "have someone quote it properly.)"
            ):
                yield event
            return

        self.quote = quote
        yield Panel("quote", quote.as_dict())
        async for event in self._say(quote.spoken()):
            yield event
        async for event in self._speak(
            "(Their quote is on screen and you have just read it out. Do NOT repeat the figure. "
            "Ask in one sentence whether that works for them.)"
        ):
            yield event

    async def _close(self, said: str) -> AsyncIterator[AgendaEvent]:
        """Ask for the business.

        The one step with no tool and no panel. Everything before it was information; this is
        the question, and an agent that shows a quote and then waits is a brochure.
        """
        async for event in self._speak(
            said or "(You have shown them the product and their number. Ask for the business, "
            "once, plainly, in one sentence.)"
        ):
            yield event

    async def _pay(self, said: str) -> AsyncIterator[AgendaEvent]:
        """Put a checkout in front of them.

        THE AGENT NEVER SEES A CARD. It asks the payment server for a hosted checkout built from
        the quote object and shows the link; the card is entered on the processor's page. That
        keeps card data out of this product entirely — no PCI scope, and no language model
        within reach of a card number.

        THE AMOUNT COMES FROM THE QUOTE, NEVER FROM THE CONVERSATION. An agent that invents a
        booking wastes a slot. An agent that invents an amount takes somebody's money.
        """
        if self.quote is None:
            # Nothing agreed yet. Quote first — a checkout for an amount nobody has seen is how
            # a sale becomes a chargeback.
            async for event in self._enter(Step.QUOTE, said=said):
                yield event
            return

        try:
            checkout = await self.tools.call(
                "payments.create_checkout",
                {
                    "amount": self.quote.total,
                    "currency": self.quote.currency,
                    "description": f"{self.quote.tier}, {self.quote.seats} seats",
                    "email": self.contact.email,
                    "company": self.quote.company,
                    "period": self.quote.period,
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.info("checkout failed: %s", exc)
            async for event in self._say(
                "I can't open the checkout just now — let me get a time with someone instead."
            ):
                yield event
            async for event in self._enter(Step.BOOKING):
                yield event
            return

        self.checkout = checkout
        yield Panel("checkout", {**checkout, "quote": self.quote.as_dict()})
        async for event in self._say(
            "I've put the checkout on your screen — you can finish it there whenever you're "
            "ready, and I'll never ask you for card details myself."
        ):
            yield event

    async def _offer_times(self, said: str) -> AsyncIterator[AgendaEvent]:
        """Offer real slots with a real person.

        THIS IS THE ESCALATION, NOT THE GOAL. The whole point of the agent is that a buyer does
        not wait for a rep, so a booking means the deal genuinely needs a human — procurement, a
        technical question nobody configured, or somebody who simply wants one.

        THE TIMES ARE NOT WRITTEN BY THE MODEL. They come from the tool already phrased for
        speech and are read verbatim. A model that invents "how about Thursday?" has promised a
        slot nobody holds.
        """
        if self.wants_human:
            # The fixed line first, before any tool can fail. Somebody who asked for a person
            # should hear the answer to that, not a calendar error.
            async for event in self._say(self.session.handoff_line):
                yield event

        try:
            found = await self.tools.call(
                "calendar.list_availability", {"limit": 3, "duration_minutes": 30}
            )
        except Exception as exc:  # noqa: BLE001
            log.info("no availability: %s", exc)
            async for event in self._say(
                "My diary isn't loading - let me have someone send you times instead."
            ):
                yield event
            return

        self.offered = found.get("slots", [])
        if not self.offered:
            async for event in self._say(
                "I don't have anything free this week - someone will send you times."
            ):
                yield event
            return

        yield Panel("slots", {"slots": self.offered, "with_person": True})
        spoken = " or ".join(slot["spoken"] for slot in self.offered[:2])
        async for event in self._say(f"I could do {spoken}. Would either of those work?"):
            yield event

    async def confirm_slot(self, index: int) -> AsyncIterator[AgendaEvent]:
        """Book one of the offered slots. Called when the prospect picks one."""
        if not 0 <= index < len(self.offered):
            return
        slot = self.offered[index]
        try:
            result = await self.tools.call(
                "calendar.book_meeting",
                {
                    "starts_at": slot["starts_at"],
                    "attendee_email": self.contact.email,
                    "attendee_name": self.contact.first_name,
                    "company": self.session.prospect.company,
                    # WHAT A REP READS BEFORE WALKING IN, not the tape. This used to paste the
                    # last six lines of dialogue verbatim, so the diary showed "Prospect: how
                    # much does it cost? Liv: Pricing depends on seats and..." — the raw
                    # transcript, mid-sentence, as the meeting's title text. The full transcript
                    # is already on the deal; what belongs here is why the meeting exists.
                    "notes": self._booking_note(),
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.info("booking failed: %s", exc)
            async for event in self._say(
                "That didn't go through - I'll have someone confirm it with you."
            ):
                yield event
            return

        if not result.get("confirmed"):
            # The tool wrote the sentence, because it knows whether the slot was taken or the
            # time had passed, and those are different apologies.
            yield Panel("slots", {"slots": self.offered, "failed": slot["starts_at"]})
            async for event in self._say(result.get("spoken", "That time has gone.")):
                yield event
            return

        self.booking = result
        yield Panel("booking", result)
        async for event in self._say(
            f"Done - {result['spoken']}. I've sent it to {self.contact.email}."
        ):
            yield event

    async def _wrap(self, said: str) -> AsyncIterator[AgendaEvent]:
        async for event in self._speak(said or "(Close the call.)"):
            yield event
        async for event in self.end():
            yield event

    async def end(self) -> AsyncIterator[AgendaEvent]:
        """Finish the call, however it ended.

        MOST CALLS DO NOT END AT `WRAP`. They end because somebody closed the tab, and a call
        that only writes itself down when it reaches the last step of the plan loses every call
        that mattered enough to be interrupted. Idempotent, because the socket teardown and a
        natural wrap can both arrive.
        """
        if self.closed:
            return
        self.closed = True
        async for event in self._close_out(self._outcome()):
            yield event

    def _outcome(self) -> str:
        """What this call actually achieved, best first.

        A paid checkout beats a booked meeting beats a conversation. Ordering them here rather
        than at each call site keeps the pipeline honest about which calls were worth having.
        """
        if self.checkout is not None:
            return "checkout_sent"
        if self.booking is not None:
            return "meeting_booked"
        if self.wants_human:
            return "handed_off"
        return "no_decision"

    # ── the end ─────────────────────────────────────────────────────────
    async def _close_out(self, outcome: str) -> AsyncIterator[AgendaEvent]:
        """Write the call down and draft the follow-up.

        After the talking, deliberately. Writing to the CRM mid-call would put a database round
        trip inside the latency budget for no benefit — nobody is reading the pipeline while the
        call is still happening.
        """
        summary = self._summary(outcome)
        for tool, arguments in (
            (
                "crm.record_call_outcome",
                {
                    "deal_id": self.deal_id, "outcome": outcome, "summary": summary,
                    "company": self.session.prospect.company or self.contact.company_guess,
                    "contact_email": self.contact.email,
                    "stage": "demo" if outcome == "meeting_booked" else "",
                },
            ),
            (
                "crm.log_call",
                {
                    "deal_id": self.deal_id, "summary": summary, "outcome": outcome,
                    "transcript": "\n".join(self.transcript_lines)[:30_000],
                    "contact_email": self.contact.email,
                },
            ),
        ):
            try:
                await self.tools.call(tool, arguments)
            except Exception as exc:  # noqa: BLE001 — the call already happened
                log.warning("could not write %s: %s", tool, exc)

        try:
            draft = await self.tools.call(
                "email.draft_recap",
                {
                    "contact_name": self.contact.first_name,
                    "company": self.session.prospect.company or self.contact.company_guess,
                    "summary": summary,
                    "meeting_spoken": (self.booking or {}).get("spoken", ""),
                    "next_step": "I'll send over the security documentation before we speak.",
                },
            )
            yield Panel("draft", draft)
        except Exception as exc:  # noqa: BLE001 — disabled without SMTP, which is the default
            log.debug("no recap drafted: %s", exc)

        yield Phase(Step.WRAP, outcome)

    def _booking_note(self) -> str:
        """One or two sentences of context for whoever takes the meeting.

        Assembled from what the call established rather than quoted from it: what they said they
        needed, what she showed them, and where the money got to. A rep opening their diary
        wants the reason, not the recording.
        """
        company = self.session.prospect.company or self.contact.company_guess
        parts = [f"{company} — booked by Liv." if company else "Booked by Liv."]

        asked = next(
            (line[len("Prospect: ") :] for line in self.transcript_lines if line.startswith("Prospect: ")),
            "",
        )
        if asked:
            parts.append(f'They opened with: "{asked[:160].strip()}"')
        if self.quote is not None:
            parts.append(f"Quoted {self.quote.money(self.quote.total)} per {self.quote.period}.")
        if self.wants_human:
            parts.append("They asked for a person.")
        if self.shown:
            parts.append(f"Shown {len(self.shown)} page(s) of the product.")
        return " ".join(parts)[:400]

    def _summary(self, outcome: str) -> str:
        company = self.session.prospect.company or self.contact.company_guess or "the prospect"
        if outcome == "meeting_booked" and self.booking:
            return f"{company}: demo booked for {self.booking['spoken']}."
        if outcome == "checkout_sent" and self.quote is not None:
            return f"{company}: quoted {self.quote.money(self.quote.total)} and sent checkout."
        if outcome == "handed_off":
            return f"{company}: asked for a person mid-call."
        return f"{company}: spoke to Liv, no meeting booked."
