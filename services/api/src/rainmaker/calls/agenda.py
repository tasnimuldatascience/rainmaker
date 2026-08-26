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
    """Where the call is. Order is the happy path; jumps are allowed and normal."""

    RESEARCHING = "researching"
    OPENING = "opening"
    DISCOVERY = "discovery"
    SHOWING = "showing"
    PROPOSING = "proposing"
    BOOKING = "booking"
    PRICING = "pricing"
    WRAP = "wrap"
    HANDOFF = "handoff"


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
        Step.PRICING,
        re.compile(r"\b(how much|what.{0,12}cost|price|pricing|budget|per seat|quote)\b", re.I),
    ),
    (
        Step.BOOKING,
        re.compile(
            r"\b(book|schedule|set (?:up|something)|calendar|meeting|call next|when can we)\b",
            re.I,
        ),
    ),
    (
        Step.SHOWING,
        re.compile(r"\b(show me|can I see|what does it look like|demo)\b", re.I),
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
            "Find out how their inbound sales calls are handled today and what is painful "
            "about it. One question at a time. Do not describe the product yet."
        ),
        max_turns=3,
        next_step=Step.SHOWING,
        transitions=(Step.SHOWING, Step.PRICING, Step.BOOKING),
    ),
    Step.SHOWING: StepPlan(
        objective=(
            "You are looking at one of their own pages. Say what you see on it and what an "
            "agent would do with it on a real inbound call. Be concrete about THEIR page."
        ),
        max_turns=3,
        next_step=Step.PROPOSING,
        transitions=(Step.PROPOSING, Step.PRICING, Step.BOOKING),
    ),
    Step.PROPOSING: StepPlan(
        objective=(
            "Make the case in two sentences: what this replaces for them specifically, and "
            "what it costs them to keep doing it by hand. Then offer to put time in the diary."
        ),
        max_turns=2,
        next_step=Step.BOOKING,
        transitions=(Step.BOOKING, Step.PRICING),
    ),
    Step.BOOKING: StepPlan(
        objective="Get one of the offered times agreed. Do not invent times.",
        max_turns=3,
        next_step=Step.PRICING,
        transitions=(Step.PRICING,),
    ),
    Step.PRICING: StepPlan(
        objective=(
            "Talk about price honestly: it depends on seats and where it runs, and a person "
            "quotes it. Never state a figure."
        ),
        max_turns=2,
        next_step=Step.WRAP,
        transitions=(Step.WRAP, Step.BOOKING),
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
        self.pages: list[dict[str, str]] = []
        self.shown: set[str] = set()
        #: The slots most recently offered, so "the first one" means something.
        self.offered: list[dict[str, str]] = []
        self.booking: dict[str, Any] | None = None
        self.enrichment: dict[str, Any] = {}
        self.transcript_lines: list[str] = []

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

        try:
            found = await self.tools.call(
                "research.pages_worth_showing", {"domain": self.contact.domain}
            )
            self.pages = found.get("pages", [])
        except Exception as exc:  # noqa: BLE001
            log.info("page discovery failed: %s", exc)

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

        if _wants_human(text):
            self.step = Step.HANDOFF
            yield Phase(Step.HANDOFF, "asked for a person")
            async for event in self.session.respond(text, budget_hints=budget_hints):
                yield event
            async for event in self._close_out("handed_off"):
                yield event
            return

        # An explicit ask outranks the plan. Someone who asks the price during discovery is
        # telling you what the call is about now.
        wanted = detect_intent(text)
        if wanted and wanted is not self.step:
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
        if step is Step.SHOWING:
            async for event in self._show(said):
                yield event
            return
        if step is Step.BOOKING:
            async for event in self._offer_times(said):
                yield event
            return
        if step is Step.PRICING:
            async for event in self._price(said):
                yield event
            return
        if step is Step.WRAP:
            async for event in self._wrap(said):
                yield event
            return

        # Discovery and proposing are pure conversation: retarget the model and let it talk.
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

    # ── the steps that do something ─────────────────────────────────────
    async def _open(self) -> AsyncIterator[AgendaEvent]:
        """A greeting that proves she read their site. The disclosure already happened."""
        if not self.contact.researchable:
            async for event in self._speak(
                "(Greet them, and ask which company they are with — you could not look it up.)"
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
        who = self.contact.first_name or "them"
        async for event in self._speak(
            f"(Greet {who} by name, mention this specific thing you found — {hook or 'their site'}"
            " — and ask what prompted them to look at this.)"
        ):
            yield event

    async def _show(self, said: str) -> AsyncIterator[AgendaEvent]:
        """Open one of their pages on the stage and narrate it.

        One page per visit. A slideshow of six tabs is a screen share nobody follows.
        """
        page = next((p for p in self.pages if p["url"] not in self.shown), None)
        if page is None:
            async for event in self._speak(said or "(continue)"):
                yield event
            return

        self.shown.add(page["url"])
        yield Panel("browser", {"state": "opening", "url": page["url"], "label": page["label"]})

        # SCROLL TO THE THING SHE IS ABOUT TO TALK ABOUT. `browse` has taken a `scroll_to` since
        # it was written and nothing ever passed one, so every page opened at the top and she
        # narrated a viewport the prospect had to scroll past to check. A screen share that does
        # not go to the point is a screen share of a homepage.
        target = self._scroll_target(page["label"])
        try:
            looked = await self.tools.call(
                "research.browse",
                {"url": page["url"], "screenshot": True, "scroll_to": target},
            )
        except Exception as exc:  # noqa: BLE001
            log.info("could not open %s: %s", page["url"], exc)
            async for event in self._speak(said or "(continue)"):
                yield event
            return

        yield Panel(
            "browser",
            {
                "state": "open",
                "url": looked.get("url", page["url"]),
                "title": looked.get("title", ""),
                "label": page["label"],
                "frame": looked.get("frame_jpeg_base64", ""),
                "scrolled_to": looked.get("scrolled_to", ""),
            },
        )
        # The model is handed what is ON SCREEN, so the narration and the picture agree. Given
        # only the URL it would describe a page it has not seen.
        excerpt = (looked.get("text") or "")[:900]
        company = self.session.prospect.company or self.contact.company_guess or "their company"
        # WHOSE PAGE IT IS HAS TO BE SPELLED OUT. Given "you are showing them their own pricing
        # page", the model said "you might have stumbled upon OUR pricing page" — claiming
        # Stripe's page as Rainmaker's, to someone who works at Stripe. Naming the company and
        # saying it twice is cheap; being corrected on whose website you are looking at is not.
        async for event in self._speak(
            f"(You are screen-sharing {company}'s OWN {page['label']} page — it belongs to the "
            f"person you are speaking to, not to Rainmaker. Never call it 'our' page. Say what "
            f"you notice on {company}'s page and what an AI agent would do with it on a real "
            f"inbound call. The page says: {excerpt})"
        ):
            yield event

    def _scroll_target(self, label: str) -> str:
        """A phrase on this page worth putting in the middle of the screen.

        Taken from what research already found rather than guessed at, so the phrase is one the
        page demonstrably contains. `browse` treats a phrase it cannot find as no scroll at all,
        so a miss costs nothing and a hit puts the evidence where she is pointing.
        """
        if label == "careers":
            roles = [f for f in self.session.prospect.facts if f.startswith("Currently hiring")]
            if roles:
                # "Currently hiring (2 open roles): Data Engineer, ..." -> "Data Engineer"
                return roles[0].split(":", 1)[-1].split(",")[0].strip()
        if label == "pricing":
            return "pricing"
        tech = [f for f in self.session.prospect.facts if f.startswith("Technology on their site")]
        if tech:
            return tech[0].split(":", 1)[-1].split(",")[0].strip()
        return ""

    async def _offer_times(self, said: str) -> AsyncIterator[AgendaEvent]:
        """Offer real slots, in words, from the calendar.

        THE TIMES ARE NOT WRITTEN BY THE MODEL. They come from the tool already phrased for
        speech, and are read out verbatim. A model that invents "how about Thursday?" has
        promised a slot nobody holds.
        """
        try:
            found = await self.tools.call(
                "calendar.list_availability", {"limit": 3, "duration_minutes": 30}
            )
        except Exception as exc:  # noqa: BLE001
            log.info("no availability: %s", exc)
            async for event in self._say(
                "My diary isn't loading — let me have someone send you times instead."
            ):
                yield event
            return

        self.offered = found.get("slots", [])
        if not self.offered:
            async for event in self._say(
                "I don't have anything free this week — someone will send you times."
            ):
                yield event
            return

        yield Panel("slots", {"slots": self.offered})
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
                    "notes": " ".join(self.transcript_lines[-6:])[:500],
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.info("booking failed: %s", exc)
            async for event in self._say(
                "That didn't go through — I'll have someone confirm it with you."
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
            f"Done — {result['spoken']}. I've sent it to {self.contact.email}."
        ):
            yield event

    async def _price(self, said: str) -> AsyncIterator[AgendaEvent]:
        """Show the tiers THIS agent was configured with.

        THE FIGURES ARE NOT SPOKEN AND NOT INVENTED. The panel shows the tenant's own published
        tiers; the model is told to say it depends and that a person quotes it. A number said
        out loud on a sales call is a commitment, and this one would be a 1.5B model's guess —
        which is why `Guardrails.speak_prices` is a setting a tenant is not allowed to turn on.
        """
        size = ((self.enrichment.get("size") or {}).get("value") or "").replace("_", " ")
        spec = self.session.spec
        tiers = (
            [{"name": t.name, "per_seat": t.price, "for": t.detail} for t in spec.pricing]
            if spec
            else []
        )
        if not tiers:
            # An agent whose owner has not entered prices has nothing to show, and inventing a
            # placeholder tier would put a number on a customer's screen that nobody agreed to.
            async for event in self._say(
                "I don't have pricing in front of me — let me have someone send it across."
            ):
                yield event
            return

        yield Panel(
            "pricing",
            {
                "company": self.session.prospect.company or self.contact.company_guess,
                "size": size,
                "tiers": tiers,
                "note": spec.pricing_note if spec else "",
            },
        )
        async for event in self._speak(
            said or "(They asked about price. It is on screen. Say how it works, not a figure.)"
        ):
            yield event

    async def _wrap(self, said: str) -> AsyncIterator[AgendaEvent]:
        async for event in self._speak(said or "(Close the call.)"):
            yield event
        async for event in self._close_out(
            "meeting_booked" if self.booking else "no_decision"
        ):
            yield event

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

    def _summary(self, outcome: str) -> str:
        company = self.session.prospect.company or self.contact.company_guess or "the prospect"
        if outcome == "meeting_booked" and self.booking:
            return f"{company}: demo booked for {self.booking['spoken']}."
        if outcome == "handed_off":
            return f"{company}: asked for a person mid-call."
        return f"{company}: spoke to Liv, no meeting booked."
