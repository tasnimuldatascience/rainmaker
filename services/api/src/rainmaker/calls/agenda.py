"""The call itself: what Nadia is trying to do right now, and what she is allowed to do about it.

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
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from ..agents.quoting import (
    build_quote,
    money_words,
    duration_from_conversation,
    rate_period,
    seats_from_conversation,
    unit_words,
)
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
        # FIRST, BECAUSE IT IS TERMINAL. A buyer winding the call up often says why in the same
        # breath — "thanks, that's all for now" — and if BOOKING or QUOTE matched first the
        # call would carry on selling to somebody who has already said they are done.
        #
        # THE ONE INTENT THAT DID NOT EXIST. `Step.WRAP`, `_wrap` and `end` were all written and
        # nothing routed to them, so "thanks, that's all for now" matched nothing, fell through
        # to an ordinary turn, and got "Great! Let me know if you need anything else." The call
        # never closed, never wrote itself down, and sat on whatever step it had reached.
        #
        # Deliberately not "thanks" on its own: "thanks, can you book something?" is the middle
        # of a call, not the end of one. It takes an actual closing phrase.
        Step.WRAP,
        re.compile(
            r"\b(?:that'?s (?:all|it|everything|great,? thanks)"
            r"|(?:i'?m|we'?re) (?:all )?(?:done|good|set|sorted)"
            r"|nothing else|no(?:thing)? more"
            r"|thanks? (?:for|so much for) (?:your |the )?(?:time|help)"
            r"|(?:i|we) (?:have|need|gotta|got to|have to) (?:to )?(?:go|run|jump off|drop)"
            r"|good ?bye|bye for now|speak soon|talk (?:to you )?(?:soon|later)"
            r"|let'?s (?:leave|wrap) it (?:there|up)|end the call|hang up)\b",
            re.I,
        ),
    ),
    (
        Step.BOOKING,
        re.compile(
            # HOW PEOPLE ACTUALLY ASK FOR A PERSON: by their job. "Talk to an engineer" is the
            # request a GPU cloud gets and it matched nothing, because the list held "human",
            # "person", "someone" and "sales" — the words a software company uses about itself.
            r"\b(?:talk|speak|chat|connect me)\b[^.?!]{0,16}?\b(?:to|with)\b\s+"
            r"(?:an?\s+|the\s+|your\s+)?(?:human|person|someone|somebody|rep|engineer|"
            r"specialist|expert|advisor|adviser|consultant|manager|sales(?:person|\s+team)?|"
            r"account manager|technical)\b"
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

#: Somebody pushing back rather than asking a question.
#:
#: "SO WHAT" IS THE COMMONEST FIRST OBJECTION AND NOTHING HANDLED IT. It matches no intent, so
#: it fell through to an ordinary discovery turn — and a 1.5B model handed "find out what they
#: are trying to fix" and a challenge does the one thing that ends a call: it mirrors. Observed,
#: verbatim, on a real call:
#:
#:     prospect  so what
#:     agent     That looks interesting. Could you tell me why you think these roles are
#:               important?
#:
#: The agent asked the buyer to justify the buyer's own job adverts. A challenge is answered, in
#: one sentence, with a reason — never with a question back.
#: An opening word that makes a sentence a request rather than a statement.
#:
#: Buyers type without punctuation, so a question mark cannot be the only test — "do you have
#: h100s" arrives exactly like that. These are the words that start one.
_ASKING = re.compile(
    r"^\s*(?:do|does|did|can|could|will|would|should|is|are|was|were|have|has|any|"
    r"what|which|where|when|why|how|who|show|tell|give|walk|take|let'?s see|got)\b",
    re.IGNORECASE,
)


#: `pick_slot` heard a time, and more than one of the offered slots answers to it.
AMBIGUOUS = -1

#: Picking one of two offered times, by position rather than by clock.
_ORDINAL_SLOT = re.compile(
    r"\b(?:the\s+)?(?:(first|1st|earlier|early|sooner)|(second|2nd|later|latter|last))\b"
    r"(?:\s+(?:one|slot|time|option))?",
    re.IGNORECASE,
)

#: A plain yes. Only ever consulted when a time is actually on the table.
_ACCEPTS = re.compile(
    r"\b(?:yes|yeah|yep|yup|sure|ok(?:ay)?|sounds? (?:good|great|fine|perfect)"
    r"|that works|works for me|perfect|great|lovely|book it|let'?s do (?:it|that)"
    r"|i'?ll take (?:it|that)|go ahead)\b",
    re.IGNORECASE,
)

#: Hours as a buyer says them back. "noon" and "midday" are twelve; "half past" is a minute cue.
_HOUR_WORDS_BY_VALUE = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight",
    9: "nine", 10: "ten", 11: "eleven", 12: "twelve",
}

_HOUR_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "noon": 12, "midday": 12,
}


def pick_slot(text: str, offered: list[dict[str, Any]]) -> int | None:
    """Which offered time they just accepted.

    Returns the index, `AMBIGUOUS` when they named a time that fits more than one of them, or
    `None` when they were not accepting anything at all. Three answers because they need three
    different responses: book it, ask which, or carry on with the call.

    THE CONSOLE COULD BOOK AND A VOICE CALL COULD NOT. `confirm_slot` was reachable only by
    clicking a slot in the browser, so on a call the agent offered two real times, the buyer
    said "Thursday at twelve works for me", and nothing happened — the model improvised "our
    team will confirm the booking for you", which is a promise nobody wrote down and no diary
    holds. The whole escalation path ended in a sentence.

    Deliberately conservative, in the same way the seat detector is. It fires on an ordinal
    ("the first one"), on a clock reference that matches an offered time, or on a bare yes when
    a time is on the table. It stays quiet on anything else, because booking the wrong half of
    somebody's Thursday is worse than asking again.
    """
    if not offered:
        return None

    ordinal = _ORDINAL_SLOT.search(text)
    if ordinal:
        return 0 if ordinal.group(1) else min(1, len(offered) - 1)

    lowered = text.lower()
    wants_half = bool(re.search(r"\b(?:thirty|half past|:30)\b", lowered))

    scored: list[tuple[int, int]] = []
    for index, slot in enumerate(offered):
        when = datetime.fromisoformat(slot["starts_at"])
        score = 0
        if when.strftime("%A").lower() in lowered:
            score += 2
        hour12 = when.hour % 12 or 12
        said_hour = any(
            word in lowered for word, value in _HOUR_WORDS.items() if value == hour12
        ) or re.search(rf"\b{hour12}\b", lowered) is not None
        if said_hour:
            score += 2
            # "twelve" against a 12:00 and a 12:30 is ambiguous until you notice which one they
            # did NOT say. The half-hour cue is the only thing separating them.
            score += 1 if wants_half == (when.minute == 30) else 0
        if score:
            scored.append((score, index))

    if scored:
        best = max(scored)
        # A TIE IS A QUESTION, NOT A DECISION. Two slots half an hour apart are both "Thursday",
        # so "Thursday works for me" narrows it to the day and no further. Booking the wrong
        # half of somebody's afternoon is the one failure a diary cannot quietly absorb, and
        # saying nothing is barely better: the model fills the silence with a promise. Asking
        # which is what a person does, and it takes one sentence.
        if sum(1 for score, _ in scored if score == best[0]) == 1:
            return best[1]
        return AMBIGUOUS

    # No clock reference at all. A bare "yes" is only an answer to the question just asked, and
    # only when there is exactly one thing it could mean.
    if len(offered) == 1 and _ACCEPTS.search(text):
        return 0
    return None


#: Enough stemming to make buy/buying and box/boxes the same word, and no more. A real stemmer
#: would be a dependency and a wrong answer here is a comparison, not a search result.
_STEM_SUFFIXES = ("ing", "es", "s")


def _stem(word: str) -> str:
    for suffix in _STEM_SUFFIXES:
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _is_asking(text: str) -> bool:
    """Whether this reads as a request rather than as a statement of their own situation.

    Not a parser and not trying to be. It separates "do you have H100s" from "we need 32 H100s",
    which is the distinction a tour trigger word cannot make on its own.
    """
    stripped = text.strip()
    return stripped.endswith("?") or bool(_ASKING.match(stripped))


def _opens(text: str) -> str:
    """`text` as the start of a sentence, with the rest of it left alone.

    `str.capitalize` is the wrong tool and was the first thing tried: it lower-cases everything
    after the first character, so "Research and Development Engineer" came back as "Research and
    development engineer" and an acronym came back ruined.
    """
    text = text.strip()
    return text[:1].upper() + text[1:] if text else ""


#: EVERY ALTERNATIVE IS ANCHORED AT BOTH ENDS, and that is the whole correction. Written as a
#: prefix match with a bare `and\??` in it, this fired on any sentence beginning with "And" —
#: so "And what would that cost us?", the question the entire call exists to reach, was
#: answered as a pushback. The buyer asked for a price and got a reason to care. Observed on a
#: recorded call, at the moment the quote should have appeared.
#:
#: A challenge is a SHORT, COMPLETE utterance. "And?" on its own is one; "And what would that
#: cost us?" is a question that happens to start with the same word. The difference is whether
#: anything follows, so the pattern now requires that nothing does.
_CHALLENGE = re.compile(
    r"^\s*(?:"
    r"so what|and|ok(?:ay)?|meh|who cares|what'?s the point|not interested"
    r"|we'?re (?:fine|all set|good)|(?:we )?don'?t need (?:it|this|that)?"
    r"|why (?:should|would|do) (?:i|we)(?: care)?|why does that matter"
    r"|and\s+that\s+matters\s+because"
    r")\s*[.?!]*\s*$",
    re.IGNORECASE,
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
        # THE MODEL NEVER SPEAKS IN THIS STEP ANY MORE — `_open` says the whole opening from
        # the tenant's own words. The objective is kept because a spec may override it and
        # because the step still frames the prompt if a tenant re-enables a model greeting.
        objective=(
            "Greet them by name if you know it, say what you think their situation is, and ask "
            "the one question that checks it. Do not pitch yet."
        ),
        max_turns=1,
        next_step=Step.DISCOVERY,
    ),
    Step.DISCOVERY: StepPlan(
        objective=(
            "Find out how much of what you sell they would actually need, and what is getting "
            "in the way today. One question at a time. Never ask them to explain their own "
            "business back to you. Do not describe the product yet."
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
        #: How long they said they needed it for, for a rate unit. See `_quote`.
        self.said_duration: tuple[float, str] | None = None
        self.quote: Any = None
        self.checkout: dict[str, Any] | None = None
        #: True once they have asked for a person, so the booking step says the handoff line.
        self.wants_human = False
        #: True once the call has been written down. See `end`.
        self.closed = False
        #: What research suggests they need, once it has been worked out. See `_diagnose`.
        self.need: Any = None

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

        # AND STRAIGHT INTO DISCOVERY, WITHOUT SAYING ANYTHING ELSE. The opening now ENDS with
        # the tenant's discovery question, so the buyer's next sentence is a discovery answer.
        # Left in OPENING, that answer was handled under the opening's objective and then the
        # step ran out of budget, and arriving at DISCOVERY spoke again — so one sentence from
        # the buyer produced a reaction and then a fresh question, which is how she ended up
        # saying "that's quite a lot!" and then asking what they were training. Two turns, one
        # of them filler, on the most important answer of the call.
        self._hand_over(Step.DISCOVERY)
        yield Phase(Step.DISCOVERY, "listening")

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

        # ACCEPTING A TIME BEATS EVERYTHING ELSE ON THE TABLE. Checked before the intents
        # because "Thursday at twelve works for me" carries no intent of its own and would
        # otherwise fall through to an ordinary turn — which is exactly what happened, and the
        # model answered it with a promise instead of a booking.
        if self.offered and self.booking is None:
            picked = pick_slot(text, self.offered)
            if picked == AMBIGUOUS:
                # Read back only the part that differs, which is how a person disambiguates a
                # time: "twelve, or twelve thirty?" rather than the whole phrase twice.
                async for event in self._say(self._which_slot()):
                    yield event
                return
            if picked is not None:
                async for event in self.confirm_slot(picked):
                    yield event
                return

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

        # AND HOW LONG FOR, WHEN THE UNIT IS A RATE. "32 H100s for a month" states the quantity
        # and the duration in one breath, and a GPU-hour is only a number once both are known.
        # Kept on the same every-turn footing as the count, and for the same reason: it arrives
        # in passing during discovery, not in answer to a question about it.
        period = rate_period(spec.pricing[0].unit_name) if spec and spec.pricing else ""
        if period:
            heard = duration_from_conversation(text, period)
            if heard is not None:
                self.said_duration = heard

        # An explicit ask outranks the plan. Someone who asks the price during discovery is
        # telling you what the call is about now.
        #
        # GUIDE RE-ENTERS ITSELF, and the others do not. "Show me another" while already on the
        # tour means the NEXT stop, and a step that only runs on arrival would have answered it
        # by talking about the page already on screen. Asking the price twice, by contrast,
        # should not rebuild the same quote — the model answers that.
        # A CHALLENGE IS ANSWERED, NOT REFLECTED. Checked before the intents, because "so what"
        # carries no intent and the fall-through is an ordinary turn — which is where a small
        # model asks the buyer to justify the buyer's own business.
        if _CHALLENGE.match(text) and self.step in (Step.OPENING, Step.DISCOVERY, Step.GUIDE):
            async for event in self._answer_challenge(text):
                yield event
            return

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
            self.transcript_lines.append(f"Nadia: {reply}")

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

    def _hand_over(self, step: Step) -> None:
        """Move to a step without speaking on arrival.

        `_enter` always says something, which is right when a step has something to announce —
        a page, a price, a set of times. It is wrong when the previous step has already asked
        the question this one exists to hear the answer to.
        """
        self.step = step
        self.turns_in_step = 0
        self._retarget(step)

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

    def _needs_line(self) -> str:
        """What this call is trying to establish, in the tenant's own words."""
        spec = self.session.spec
        unit = next((t.unit_name for t in (spec.pricing if spec else ()) if t.unit_amount), "")
        if self.need is not None:
            return f" You believe {self.need.means}." + (
                f" You are trying to find out how many {unit}s they need." if unit else ""
            )
        return f" You are trying to find out how many {unit}s they need." if unit else ""

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
        # THE NEED TRAVELS WITH THE OBJECTIVE. Working out what they need in the opening and
        # then not telling the model about it again is how a call drifts back into small talk
        # by the second question.
        self.session.profile.objective = (
            spec.objective_for(str(step), plan.objective) if spec else plan.objective
        ) + self._needs_line()

    async def _speak(self, prompt: str) -> AsyncIterator[AgendaEvent]:
        """Let the model say something for the current step."""
        self._retarget(self.step)
        reply = ""
        async for event in self.session.respond(prompt, internal=True):
            if isinstance(event, Finished):
                reply = event.result.response
            yield event
        if reply:
            self.transcript_lines.append(f"Nadia: {reply}")

    async def _say(self, line: str) -> AsyncIterator[AgendaEvent]:
        """Say a fixed line — no model involved.

        Used wherever the words carry a commitment: a time, a confirmation, a disclosure.
        """
        self.transcript_lines.append(f"Nadia: {line}")
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

        # ONLY WHEN THEY ARE ASKING. A tour stop's trigger words are nouns from the tenant's own
        # product, and a buyer says those nouns for two completely different reasons:
        #
        #   "do you have H100s?"                    -> show me the capacity page
        #   "we're training a 70B model, about      -> this is my requirement; you asked
        #    32 H100s for a month"
        #
        # Matched as a substring, the second one drove the call straight to the tour: the buyer
        # answered the discovery question and was shown a web page instead of being heard. The
        # answer still counts — the quantity and duration are read off it further up — but it
        # does not move the call.
        #
        # `detect_intent` is unaffected: its patterns are phrases like "show me" and "how much",
        # which are requests whatever mood they are in.
        if _is_asking(text) and any(
            topic and topic.lower() in lowered for stop in spec.tour for topic in stop.answers
        ):
            return Step.GUIDE
        if any(rival.name.lower() in lowered for rival in spec.competitors):
            return Step.COMPARE
        return None

    # ── the steps that do something ─────────────────────────────────────
    async def _answer_challenge(self, said: str) -> AsyncIterator[AgendaEvent]:
        """They pushed back. Give a reason, not a question.

        "So what" is the commonest first objection and nothing handled it: it matches no intent,
        so it fell through to an ordinary discovery turn, and a 1.5B model handed "find out what
        they are trying to fix" plus a challenge does the one thing that ends a call — it
        mirrors. Observed, verbatim:

            prospect  so what
            agent     That looks interesting. Could you tell me why you think these roles are
                      important?

        It asked the buyer to justify the buyer's own job adverts. What a seller does with a
        challenge is answer it in one sentence with a concrete reason, and only then ask.
        """
        spec = self.session.spec
        sells = spec.knowledge[0].text if spec and spec.knowledge else "what you sell"
        why = next(
            (fact.text for fact in (spec.knowledge if spec else ()) if fact.topic == "why"),
            "",
        )
        reason = f" The reason people move is: {why}" if why else ""
        async for event in self._speak(
            f"(They pushed back with \"{said.strip()}\". Do NOT ask them a question about their "
            f"own business and do NOT ask why something matters to them. In ONE sentence, say "
            f"concretely what {sells} would change for someone in their position.{reason} Then "
            f"ask one short question about their own setup.)"
        ):
            yield event

    def _diagnose(self) -> tuple[str, Any]:
        """What research found, and what it suggests they need. Either may be empty.

        THIS IS THE STEP THAT WAS MISSING. Research produced facts about the BUYER and the agent
        sold a PRODUCT, and nothing joined them — so the agent read the facts out. The tenant
        writes down what each signal means for what they sell (`spec.needs`), and this picks the
        strongest match across everything research returned.

        Scored over the WHOLE fact list rather than per fact, because the evidence for a need is
        usually spread: a careers page mentioning research engineers and a stack listing PyTorch
        are one hypothesis, not two.
        """
        spec = self.session.spec
        facts = self.session.prospect.facts
        if not spec or not spec.needs or not facts:
            return "", None

        corpus = " ".join(facts)
        # A need with nothing to say is skipped rather than spoken. `validate` refuses these at
        # publish time; this is the second line, because a spec can also arrive from a database
        # written before that check existed, and the failure mode is a sentence with a hole in
        # it read out loud to a customer.
        usable = [n for n in spec.needs if n.opener.strip() and n.ask.strip()]
        if not usable:
            return "", None
        ranked = sorted(usable, key=lambda need: need.score(corpus), reverse=True)
        best = ranked[0]
        if not best.score(corpus):
            return "", None

        # The fact that carries the most of that need's signal, narrowed to the part of it that
        # did the carrying — see `Need.narrow`. Handing over the whole fact means handing over a
        # list, and a list in the context is a list in the greeting.
        source = max(facts, key=best.score)
        if not best.score(source):
            return "", best

        # ONLY A LIST MAKES A QUOTABLE NOUN PHRASE, and this is decided here rather than in
        # `narrow` because it is a question about the sentence it lands in, not about the fact.
        # Research facts come in two shapes. "Technology on their site: express, rails, aws" and
        # "Currently hiring (4 open roles): ..." are enumerations, and one item out of one is a
        # thing a person can say they noticed. "How they charge: sales assisted" is a field with
        # a value, and reading it into the same slot produced "sales assisted stood out" —
        # grammatical, and not English anybody speaks.
        #
        # So a non-list fact still selects the need; it just does not get quoted. She says what
        # she thinks rather than what she read, which was the point of the whole mechanism.
        _, separator, body = source.partition(": ")
        if not separator or "," not in body:
            return "", best
        return best.narrow(source), best

    async def _open(self) -> AsyncIterator[AgendaEvent]:
        """A greeting that proves she read their site, and says what it means.

        A SELLER DOES NOT RECITE WHAT THEY FOUND. Told to "mention one specific thing you found",
        a 1.5B model reads the fact back including its label and its list of four items. What a
        seller does with a signal is say what it might MEAN for the thing they sell, and check.
        """
        who = self.contact.first_name or "them"
        spec = self.session.spec
        sells = spec.knowledge[0].text if spec and spec.knowledge else "what you sell"

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

        evidence, need = self._diagnose()
        if need is None:
            # Read their site and found nothing that bears on what we sell. Say hello and ask
            # about the problem rather than filling the silence with the rest of the research.
            async for event in self._speak(
                f"(Greet {who} by name. Nothing you read about "
                f"{self.contact.company_guess or 'their company'} relates to {sells}, so do "
                f"not mention what you read. Ask one question about whether they have that "
                f"problem today.)"
            ):
                yield event
            return

        self.need = need
        # AND THE OBJECTIVE LEARNS ABOUT IT NOW. `_retarget` runs at the top of `_speak`, and
        # the opening no longer speaks through the model — so without this the need would not
        # reach the system prompt until the next model turn happened to rebuild it. It would
        # get there eventually; "eventually" is not an invariant.
        self._retarget(self.step)

        # THE OPENING SENTENCE IS NOT IMPROVISED. Every version that let the model write it went
        # wrong in a different way and none of them were fixable from the prompt:
        #
        #   "mention one specific thing you found"      -> it read four job titles out loud
        #   "say what it means, then ask exactly this"  -> it diagnosed and never asked
        #   "...and do NOT ask a question"              -> it asked one anyway, so the tenant's
        #                                                  question landed second and the buyer
        #                                                  got two questions in a row
        #
        # The third one is the reason this is now fixed text rather than a better prompt. Clips
        # stream as they are generated, so by the time a reply can be inspected it has already
        # been heard — there is no post-filter, and a rule that cannot be enforced is a hope.
        #
        # This is the first sentence of a sales call. It is the highest-stakes line on it, every
        # word of it is tenant-written, and it now costs no model time at all. The model still
        # runs everything from discovery onward, which is where improvising is the point.
        # The evidence is spoken because it is the proof she actually read the site, and
        # `Need.narrow` has already cut it down to the noun phrase that carried the signal — so
        # it drops into a sentence without dragging a list in behind it.
        greeting = f"Hi {who} — " if self.contact.first_name else "Hi — "
        looked = f"I had a quick look at {self.contact.domain} before we spoke"
        # `opener` and `ask` are written lower case because they are fragments a tenant drops
        # into a sentence, and `evidence` is however the site spelled it. Each one starts a
        # sentence here, so each one is capitalised here — a full stop followed by "how long
        # does a new rep" is a thing a reader notices immediately and a caption shows plainly.
        middle = f"{_opens(evidence)} stood out, and {need.opener}." if evidence else (
            f"{_opens(need.opener)}."
        )
        async for event in self._say(
            f"{greeting}{looked}. {middle} {_opens(need.ask)}"
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
        # WHAT IS ON THE SCREEN IS THE TENANT'S SENTENCE. WHY IT MATTERS IS THE MODEL'S.
        #
        # Describing a screen share is the one narration job a 1.5B model cannot be trusted
        # with, and two prompts proved it in opposite directions. Handed 700 characters of the
        # demo customer's page it described the page: "pricing for a compute capacity service
        # called Tessera, offered by Rainmaker" — Tessera is the example customer, not the
        # product — and read that site's "~90s node ready" back as renting GPUs "for up to
        # ninety seconds". Told instead not to describe the page, it let go of the screen
        # altogether and described the PROSPECT from the research dossier: "Stripe is a flexible
        # solutions provider for businesses..." while our own product sat on screen behind it.
        #
        # Both failures are the same failure: the sentence that has to be factually right about
        # a picture was being generated from context rather than stated. `TourStop.shows` is
        # that sentence, the tenant wrote it, and it is right by construction — so it is said,
        # not paraphrased. The model then does the part it is good at and cannot get factually
        # wrong: tying it back to the problem this particular buyer just described.
        ours = self.session.spec.company if self.session.spec else "your company"
        theirs = self.session.prospect.company or self.contact.company_guess or "their company"

        async for event in self._say(f"What you're looking at is {stop.shows}."):
            yield event

        # NO PAGE TEXT IN THIS PROMPT AT ALL. It was cut from 700 characters to 240 and the
        # model still read numbers off it and got them wrong — "you're currently getting 392
        # GPUs for free right now, with a reservation period of a week", from a page that says
        # 392 GPUs are AVAILABLE and says nothing about a week. It cannot misread a page it has
        # not been shown, and it does not need one: what is on screen has already been said, in
        # the tenant's words, in the sentence immediately before this.
        #
        # What is left is the only thing the model is being asked for and the only thing it
        # cannot get factually wrong — the connection between a product and a problem the buyer
        # described in their own words a moment ago.
        problem = self.need.means if self.need is not None else ""
        because = f" They are dealing with this: {problem}." if problem else ""
        async for event in self._speak(
            f"(You are screen-sharing {ours}'s own product with {theirs}: {stop.label}. You have "
            f"just said what is on the screen, so do NOT describe it again, do NOT describe "
            f"{theirs}, and do NOT quote any number.{because} In ONE sentence, say what this "
            f"would change for them. Address them as \"you\".)"
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

        # WHICH RIVAL THEY MEANT, NOT WHICH ONE IS FIRST. Substring matching required the buyer
        # to say the tenant's exact phrase: "why not just buy our own boxes?" missed
        # "buying your own boxes" on a single letter, fell through to the default, and got a
        # fair-and-honest comparison against something they had not mentioned.
        #
        # Overlap on content words instead, stemmed just enough that buy/buying and box/boxes
        # are the same word. No match at all still falls back to the tenant's first rival, which
        # is the one they chose to lead with.
        lowered = said.lower()
        heard = {_stem(word) for word in re.findall(r"[a-z]{3,}", lowered)}
        scored = [
            (len(heard & {_stem(w) for w in re.findall(r"[a-z]{3,}", r.name.lower())}), i, r)
            for i, r in enumerate(rivals)
        ]
        best = max(scored)
        shown = [best[2]] if best[0] else list(rivals)

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
        # SPOKEN, NOT SUMMARISED. Handed the table and told to "be fair, then say where you
        # differ", the model answered "how does this compare to AWS?" with "it seems you're
        # already well equipped for what you need" — a sentence that compares nothing, concedes
        # the deal, and is not in the table anywhere.
        #
        # That is the expensive failure mode here twice over. Every clause about a named
        # competitor is a factual claim the tenant is answerable for, which is why this
        # docstring already calls a model-written comparison a defamation risk with a grid
        # layout — and a model that will not actually compare loses the deal on the one question
        # where the buyer has said out loud what they need convincing of.
        #
        # The tenant wrote both halves: what the competitor is genuinely good at, and where the
        # difference is. Said in that order, it IS the answer.
        # "The honest case for X" rather than "X is/are good at": a rival's name is whatever the
        # tenant typed — "a hyperscaler", "buying your own boxes", "a chat widget" — and no verb
        # agrees with all of them. The first attempt said "A chat widget are genuinely good at".
        # A phrasing with no verb to agree cannot be wrong about one.
        rival = shown[0]
        fair = f"The honest case for {rival.name}: {rival.positioning}."
        differences = " ".join(f"On {d}, we {o}." for d, o in rival.against)
        async for event in self._say(f"{fair} Where we differ: {differences}"):
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
            said_duration=self.said_duration,
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
        # ASKED FOR ONE PREDICTABLE SENTENCE, IT ANSWERED AS THE BUYER. Told "ask whether that
        # works for them", the model produced "That works perfectly, thank you." — accepting its
        # own quote, on the seller's behalf, out loud. There is exactly one sentence that
        # belongs after a number, it never varies, and it is the last one before the buyer
        # either objects or moves; nothing about it wants improvising.
        async for event in self._say("How does that sit against what you had in mind?"):
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

    def _which_slot(self) -> str:
        """"Twelve, or twelve thirty?" — the smallest question that separates the offers.

        Built from the times rather than asked of the model, for the same reason the offer was:
        a model that re-words a slot has offered a slot nobody holds.
        """
        times = []
        for slot in self.offered[:2]:
            when = datetime.fromisoformat(slot["starts_at"])
            hour = _HOUR_WORDS_BY_VALUE[when.hour % 12 or 12]
            times.append(f"{hour} thirty" if when.minute == 30 else hour)
        if len(times) < 2 or times[0] == times[1]:
            return "Which of those works better for you?"
        return f"{_opens(times[0])}, or {times[1]}?"

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
                    # much does it cost? Nadia: Pricing depends on seats and..." — the raw
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
        """Say what was agreed, say goodbye, and stop.

        THE LAST SENTENCE OF A CALL IS A SUMMARY OF ITS COMMITMENTS, so it is not the model's.
        Asked to "(Close the call.)" it produced "Great! Let me know if you need anything else."
        — which is not a close, it is an invitation to keep going, and it left a buyer who had
        just said they were done with nothing to confirm and no idea the call had ended.

        A person winding up a sales call recites the outcome: the time that is now in the diary,
        the number that is now on the screen, where the follow-up is going. Every one of those
        is a fact this object already holds, and none of them is safe to paraphrase.
        """
        agreed: list[str] = []
        if self.booking is not None and self.booking.get("spoken"):
            agreed.append(f"You're booked in for {self.booking['spoken']}")
        if self.checkout is not None:
            agreed.append("the checkout link is on your screen whenever you're ready")
        elif self.quote is not None:
            # `money_words`, NOT `money`. `money` is the screen form and it starts with a "$",
            # which a synthesiser reads as the word "dollar" placed BEFORE the digits. The quote
            # sentence has said its own numbers in words since that was found; this line is
            # spoken too and had quietly reintroduced it.
            spoken_total = money_words(self.quote.total, self.quote.currency)
            agreed.append(f"your numbers are on screen — {spoken_total}")
        if self.wants_human and self.booking is None:
            agreed.append("I'll get somebody to pick this up with you")

        who = self.contact.first_name
        closing = f"Thanks for your time{', ' + who if who else ''}."
        if agreed:
            # Joined with a full stop rather than a comma: each of these is a separate
            # commitment and running them together is how one of them gets missed.
            body = ". ".join(_opens(part) for part in agreed)
            line = f"{body}. {closing}"
        else:
            # Nothing was agreed, and saying so is better than implying something was. The
            # follow-up still goes out; `_close_out` drafts it either way.
            line = f"{closing} I'll send you a short note with what we covered."

        async for event in self._say(line):
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
        parts = [f"{company} — booked by Nadia." if company else "Booked by Nadia."]

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
        return f"{company}: spoke to Nadia, no meeting booked."
