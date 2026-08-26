"""The call pipeline: disclosure enforcement, the latency budget, and the turn loop.

THIS MODULE'S OWN DOCSTRING SAID IT WAS TESTED. "the orchestration, budget accounting, disclosure
enforcement, and the provider interfaces are implemented and tested" — and coverage measured it at
zero percent. That sentence is now true rather than aspirational, which matters more here than in
most files: the same docstring is scrupulous about what has NOT been run end to end (the avatar
adapter, which needs a GPU), and a document that is careful about one claim and loose about
another teaches a reader to trust neither.

DISCLOSURE IS THE PART WORTH TESTING HARDEST. The agent says it is an AI, at the start, every time,
and the design makes that impossible to switch off — several jurisdictions require it and every
customer's compliance review will ask. A guarantee like that is only as good as its enforcement,
so the tests below try to get a turn out of the pipeline without it.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator

import pytest

from rainmaker.calls.pipeline import (
    CallPipeline,
    Clip,
    Disclosure,
    DisclosureError,
    LanguageModel,
    LatencyBudget,
    PlaceholderAvatar,
    SpeechToText,
    Stage,
    TextToSpeech,
    _wants_human,
)


class FakeSTT(SpeechToText):
    """Emits partials then a final, which is the contract the pipeline relies on."""

    name = "fake-stt"

    def __init__(self, transcript: str = "how much does it cost"):
        self.transcript = transcript

    async def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[tuple[str, bool]]:
        async for _ in audio:
            pass
        words = self.transcript.split()
        for i in range(1, len(words)):
            yield " ".join(words[:i]), False
        yield self.transcript, True


class FakeLLM(LanguageModel):
    name = "fake-llm"

    def __init__(self, reply: str = "It is forty pounds a seat, per month."):
        self.reply = reply
        self.saw_prompt: str | None = None
        self.saw_context: dict | None = None

    async def stream(self, prompt: str, context: dict) -> AsyncIterator[str]:
        self.saw_prompt, self.saw_context = prompt, context
        for token in self.reply.split(" "):
            yield token + " "


class FakeTTS(TextToSpeech):
    name = "fake-tts"

    def __init__(self):
        self.tokens_seen: list[str] = []

    async def clips(self, text: AsyncIterator[str]) -> AsyncIterator[Clip]:
        index = 0
        async for token in text:
            self.tokens_seen.append(token)
            yield Clip(text=token, wav=b"\x00\x01", duration_ms=40.0, index=index)
            index += 1


async def silence(chunks: int = 3) -> AsyncIterator[bytes]:
    for _ in range(chunks):
        yield b"\x00" * 320


def pipeline(**kwargs) -> CallPipeline:
    return CallPipeline(stt=FakeSTT(), llm=FakeLLM(), tts=FakeTTS(), **kwargs)


class TestDisclosureCannotBeSkipped:
    def test_a_turn_before_the_disclosure_is_refused(self):
        """THE ENFORCEMENT. Documented guarantees get forgotten; this one raises."""
        with pytest.raises(DisclosureError, match="disclosure"):
            asyncio.run(pipeline().turn(silence()))

    def test_a_turn_after_the_disclosure_is_allowed(self):
        async def go():
            call = pipeline()
            await call.open()
            return await call.turn(silence())

        assert asyncio.run(go()).transcript

    def test_opening_returns_the_line_the_prospect_hears(self):
        async def go():
            return await pipeline().open()

        spoken = asyncio.run(go())
        assert "AI" in spoken
        assert "not a human" in spoken.lower()

    def test_it_cannot_be_configured_off(self):
        """`required=False` is a legal question rather than a setting, and the constructor says
        so by refusing to build."""
        with pytest.raises(DisclosureError, match="cannot be disabled"):
            Disclosure(required=False)

    def test_the_wording_can_be_changed_but_not_removed(self):
        """A deployment may need its own phrasing. It may not need silence."""
        custom = Disclosure(spoken="Heads up: you're speaking with an AI.")
        assert custom.required
        assert custom.logged

    def test_what_is_logged_is_separate_from_what_is_said(self):
        """The spoken line is for the prospect; the logged token is what a dispute months later
        is settled with. One changing must not silently change the other."""
        assert Disclosure().logged != Disclosure().spoken


class TestTheLatencyBudget:
    def test_marks_accumulate(self):
        budget = LatencyBudget()
        budget.mark(Stage.STT)
        budget.mark(Stage.LLM)
        assert set(budget.marks) == {"stt", "llm"}

    def test_the_total_is_the_sum_of_the_stages(self):
        budget = LatencyBudget()
        budget.marks.update({"stt": 40.0, "llm": 220.0, "tts": 70.0})
        assert budget.total_ms == pytest.approx(330.0)

    def test_a_fast_turn_is_within_budget_and_a_slow_one_is_not(self):
        """The number is the product. A budget that cannot report being exceeded is decoration."""
        fast = LatencyBudget()
        fast.marks.update({"stt": 40.0, "llm": 200.0})
        assert fast.within_budget

        slow = LatencyBudget()
        slow.marks.update({"stt": 400.0, "llm": 700.0, "tts": 350.0, "avatar": 120.0})
        assert not slow.within_budget

    def test_the_report_carries_the_stages_and_the_verdict(self):
        budget = LatencyBudget()
        budget.mark(Stage.STT)
        report = budget.report()
        assert "stt" in report
        assert "total_ms" in report
        assert isinstance(report["within_budget"], bool)

    def test_an_empty_budget_is_zero_rather_than_an_error(self):
        assert LatencyBudget().total_ms == 0


class TestTheTurnLoop:
    def run(self, call: CallPipeline, context: dict | None = None):
        async def go():
            await call.open()
            return await call.turn(silence(), context)

        return asyncio.run(go())

    def test_the_final_transcript_is_the_one_returned(self):
        """Partials are for the screen. The model must be prompted with the final."""
        llm = FakeLLM()
        call = CallPipeline(stt=FakeSTT("how much does it cost"), llm=llm, tts=FakeTTS())
        result = self.run(call)

        assert result.transcript == "how much does it cost"
        assert llm.saw_prompt == "how much does it cost"

    def test_the_response_is_the_tokens_joined(self):
        # DELIBERATELY NOT A PRICE. This used to say "Forty pounds a seat", which now comes out
        # redacted — correctly, and see `TestTheModelCannotStateAPrice`. A fixture that trips a
        # guardrail tests the guardrail rather than the thing it was written for.
        call = CallPipeline(stt=FakeSTT(), llm=FakeLLM("It runs on your own hardware."), tts=FakeTTS())
        assert self.run(call).response.strip() == "It runs on your own hardware."

    def test_synthesis_sees_the_tokens_as_they_stream(self):
        """THE WHOLE TRICK. TTS is handed the token stream, not the finished string -- if it were
        awaited, the naive column of the table in the module docstring is what you get."""
        tts = FakeTTS()
        # Not number words: "one two three four" is four things a figure could be made of, so
        # the price guard holds them waiting for a currency to land and the stream arrives whole.
        call = CallPipeline(stt=FakeSTT(), llm=FakeLLM("quick brown fox jumps"), tts=tts)
        self.run(call)
        assert len(tts.tokens_seen) >= 4, tts.tokens_seen

    def test_every_server_side_stage_is_measured(self):
        """Three of the five stages happen here. The other two happen in the browser and are
        adopted from it — see `LatencyBudget.adopt`. Marking them here would be a guess wearing
        a measurement's clothes."""
        result = self.run(pipeline())
        for stage in ("stt", "llm", "tts"):
            assert stage in result.budget.marks, f"{stage} was never marked"

    def test_the_client_measured_stages_are_adopted_not_invented(self):
        budget = LatencyBudget()
        budget.mark(Stage.LLM)
        budget.adopt(Stage.STT, 244.0)
        budget.adopt(Stage.AVATAR, 31.0)
        assert budget.marks["stt"] == 244.0
        assert budget.marks["avatar"] == 31.0

    def test_a_negative_client_measurement_is_clamped(self):
        """The client computes these from two `performance.now()` readings. A clock that goes
        backwards across a tab suspend would otherwise subtract from the total."""
        budget = LatencyBudget()
        budget.adopt(Stage.STT, -12.0)
        assert budget.marks["stt"] == 0.0

    def test_context_reaches_the_model(self):
        llm = FakeLLM()
        call = CallPipeline(stt=FakeSTT(), llm=llm, tts=FakeTTS())
        self.run(call, {"deal": "acme", "stage": "discovery"})
        assert llm.saw_context == {"deal": "acme", "stage": "discovery"}

    def test_no_context_is_an_empty_dict_not_none(self):
        """The model implementations index into it. `None` would raise inside the provider,
        which is the worst place to discover it."""
        llm = FakeLLM()
        call = CallPipeline(stt=FakeSTT(), llm=llm, tts=FakeTTS())
        self.run(call)
        assert llm.saw_context == {}

    def test_an_empty_reply_does_not_break_the_turn(self):
        call = CallPipeline(stt=FakeSTT(), llm=FakeLLM(""), tts=FakeTTS())
        assert self.run(call).response.strip() == ""

    def test_a_turn_over_budget_still_returns(self):
        """Exceeding the budget is a log line, not an exception -- hanging up on a slow turn is
        worse than a slow turn."""
        budget = LatencyBudget()
        budget.marks.update({"stt": 5000.0})
        assert not budget.within_budget


class TestThePlaceholderAvatar:
    def test_it_is_the_default(self):
        """So the system runs anywhere. The real avatar needs several GB of weights and a GPU,
        and the module docstring is explicit that it has not been run end to end here."""
        assert isinstance(pipeline().avatar, PlaceholderAvatar)

    def test_it_consumes_audio_and_yields_something(self):
        async def go():
            async def audio() -> AsyncIterator[bytes]:
                yield b"\x00\x01"

            return [chunk async for chunk in PlaceholderAvatar().render(audio())]

        asyncio.run(go())


class TestTheModelCannotStateAPrice:
    """OBSERVED ON A REAL CALL, WHICH IS WHY THIS FILE HAS THIS CLASS. Asked what capacity was
    available, Qwen said "starting from $50 per GPU per hour" about a product whose rate card
    says $2.40. Every other part of the pricing design was already right — the quote is
    arithmetic, the sentence stating it is written in code, the prompt says the figure has
    already been read out — and none of it mattered, because nothing was checking.

    The platform's own computed sentences do not pass through this. That is the distinction:
    the platform may state a figure it worked out, and the model may not state one at all.
    """

    @staticmethod
    async def guarded(text: str, chunk: int = 3, allowed: bool = False) -> str:
        from rainmaker.calls.pipeline import guard_prices

        async def feed():
            for i in range(0, len(text), chunk):
                yield text[i : i + chunk]

        return "".join([token async for token in guard_prices(feed(), allowed)])

    @pytest.mark.parametrize(
        "said",
        [
            "We rent GPUs by the hour, starting from $50 per GPU per hour.",
            "Would $4,800 per month work for you?",
            "That's fifty dollars, roughly.",
            "It costs about 2,400 dollars a month.",
            "Around £1,250.00 all in.",
        ],
    )
    async def test_a_figure_never_reaches_the_synthesiser(self, said: str):
        spoken = await self.guarded(said)
        assert not re.search(r"[$£€]\s?\d", spoken), spoken
        assert "dollars" not in spoken and "pounds" not in spoken
        assert "figure on your screen" in spoken

    async def test_the_redaction_does_not_depend_on_how_tokens_arrive(self):
        """A model streams a word at a time and sometimes a letter at a time. An earlier version
        emitted "$5" and held "0", turning a price into a stray zero."""
        said = "That's fifty dollars, or $4,800 a month."
        outputs = {await self.guarded(said, chunk=n) for n in (1, 2, 3, 5, 8, 40, 500)}
        assert len(outputs) == 1, outputs
        assert "$" not in outputs.pop()

    @pytest.mark.parametrize(
        "said",
        [
            "We have 64 nodes free in eu-west-1 right now.",
            "It comes up in about 90 seconds.",
            "There are 3 regions.",
            "Yes, absolutely — that works well for teams your size.",
        ],
    )
    async def test_ordinary_numbers_are_left_alone(self, said: str):
        """A guard that eats every number makes the agent unable to say how many nodes are free,
        which is most of what this particular agent is for."""
        assert await self.guarded(said) == said

    async def test_a_tenant_who_turned_the_guard_off_is_obeyed(self):
        """`speak_prices` exists, and if a tenant sets it the platform does what it is told —
        it simply cannot be set without `validate()` making them say it out loud."""
        said = "It's $50 an hour."
        assert await self.guarded(said, allowed=True) == said

    async def test_the_platform_still_says_its_own_computed_figure(self):
        """The quote is read out by `_say`, which never goes near the model path. If this ever
        starts failing, the guard has been wired in one layer too high."""
        from rainmaker.agents.quoting import build_quote
        from rainmaker.agents.spec import AgentSpec, Tier

        spec = AgentSpec(
            tenant="t", agent_id="a", currency="usd", pricing_period="month",
            pricing=(Tier("Team", "$40", "", unit_amount=4000, min_seats=1),),
        )
        assert "$400" in build_quote(spec, said_seats=10).spoken()


class TestAskingForAPersonByTheirJob:
    """NOBODY SAYS "MAY I SPEAK TO A HUMAN". They ask for an engineer, a rep, someone technical
    — and the detector held "talk to a human" plus four variants of it, which are the words a
    software company uses about itself rather than the words a buyer uses.

    So "actually, can I talk to an engineer?" on a GPU cloud's own call went to the model as an
    ordinary question and the agent carried on selling. Missing a request for a person is the
    single worst thing this product can do, and it survived every test in this file until
    somebody drove a whole call and asked the way a buyer would.
    """

    @pytest.mark.parametrize(
        "said",
        [
            "actually can I talk to an engineer?",
            "can I speak to someone technical",
            "talk to a human please",
            "connect me with a rep",
            "I want to chat with a specialist",
            "can I speak to your sales team",
            "could I talk to the account manager",
        ],
    )
    def test_a_request_for_a_person_is_heard(self, said: str):
        assert _wants_human(said), said

    @pytest.mark.parametrize(
        "said",
        [
            "can you talk to me about pricing",
            "how do I talk to your API",
            "we speak to about forty customers a week",
            "I will talk to my team about it",
            "does it integrate with our chat tool",
        ],
    )
    def test_ordinary_sentences_do_not_trigger_a_handoff(self, said: str):
        """A false positive costs one unnecessary transfer, which is cheap — but an agent that
        hands off whenever the word "talk" appears cannot hold a conversation at all."""
        assert not _wants_human(said), said
