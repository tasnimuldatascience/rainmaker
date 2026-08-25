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
from collections.abc import AsyncIterator

import pytest

from rainmaker.calls.pipeline import (
    CallPipeline,
    Disclosure,
    DisclosureError,
    LanguageModel,
    LatencyBudget,
    PlaceholderAvatar,
    SpeechToText,
    Stage,
    TextToSpeech,
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
        self.chunks_before_first_audio = 0

    async def stream(self, text: AsyncIterator[str]) -> AsyncIterator[bytes]:
        async for token in text:
            self.tokens_seen.append(token)
            yield b"\x00\x01"


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
        call = CallPipeline(stt=FakeSTT(), llm=FakeLLM("Forty pounds a seat."), tts=FakeTTS())
        assert self.run(call).response.strip() == "Forty pounds a seat."

    def test_synthesis_sees_the_tokens_as_they_stream(self):
        """THE WHOLE TRICK. TTS is handed the token stream, not the finished string -- if it were
        awaited, the naive column of the table in the module docstring is what you get."""
        tts = FakeTTS()
        call = CallPipeline(stt=FakeSTT(), llm=FakeLLM("one two three four"), tts=tts)
        self.run(call)
        assert len(tts.tokens_seen) >= 4, tts.tokens_seen

    def test_every_stage_is_measured(self):
        result = self.run(pipeline())
        for stage in ("stt", "llm", "tts", "avatar"):
            assert stage in result.budget.marks, f"{stage} was never marked"

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
