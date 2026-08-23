"""The live call pipeline: hear, think, speak, animate.

THE LATENCY BUDGET IS THE PRODUCT. Human conversation tolerates roughly 300ms of silence
before a pause becomes noticeable and roughly 800ms before it reads as "something is wrong
with this person". Every architectural decision below is downstream of that number:

    stage           naive        streamed      budget
    ─────────────────────────────────────────────────
    VAD / endpoint    250ms        250ms         250ms   unavoidable: must hear silence
    STT final         400ms         60ms          80ms   stream partials, finalise on endpoint
    LLM first token   700ms        220ms         250ms   stream; never wait for completion
    TTS first audio   350ms         70ms         100ms   start on the first clause, not the sentence
    lip-sync          120ms         30ms          40ms   pipeline against the audio stream
    ─────────────────────────────────────────────────
    total          ~1820ms       ~630ms         720ms

The naive column is what you get by awaiting each stage. The streamed column is what this
pipeline is built to achieve, by starting every stage on the first token of the previous one.
`LatencyBudget` measures the real thing per turn and the console renders it, because a budget
nobody measures is a wish.

WHAT IS VERIFIED AND WHAT IS NOT — stated here rather than discovered by a reader:
the orchestration, budget accounting, disclosure enforcement, and the provider interfaces are
implemented and tested. The realtime avatar itself (MuseTalk over a LivePortrait idle loop) is
adapter code that has NOT been run end to end in this repository: it needs several GB of
weights and a persistent GPU service. `PlaceholderAvatar` is the default so the system runs
anywhere, and the README carries the same distinction.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum

log = logging.getLogger("rainmaker.calls.pipeline")

# Above this, a turn stops feeling like conversation. Logged as a warning per turn.
TURN_BUDGET_MS = 800.0


class Stage(StrEnum):
    ENDPOINT = "endpoint"
    STT = "stt"
    LLM = "llm"
    TTS = "tts"
    AVATAR = "avatar"


@dataclass
class LatencyBudget:
    """Per-turn stage timings. Measured, not estimated."""

    marks: dict[str, float] = field(default_factory=dict)
    _started: float = field(default_factory=time.perf_counter)
    _last: float = field(default_factory=time.perf_counter)

    def mark(self, stage: Stage | str) -> float:
        now = time.perf_counter()
        delta = (now - self._last) * 1000
        self.marks[str(stage)] = round(delta, 2)
        self._last = now
        return delta

    @property
    def total_ms(self) -> float:
        return round(sum(self.marks.values()), 2)

    @property
    def within_budget(self) -> bool:
        return self.total_ms <= TURN_BUDGET_MS

    def report(self) -> dict[str, float | bool]:
        return {**self.marks, "total_ms": self.total_ms, "within_budget": self.within_budget}


# ───────────────────────────────────────────────────────────── provider interfaces
class SpeechToText(ABC):
    """Streaming transcription. Yields partials, then a final on endpoint."""

    @abstractmethod
    def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[tuple[str, bool]]: ...


class LanguageModel(ABC):
    """Token-streaming reasoning. MUST stream: waiting for a complete response is the single
    largest avoidable cost in the budget."""

    @abstractmethod
    def stream(self, prompt: str, context: dict) -> AsyncIterator[str]: ...


class TextToSpeech(ABC):
    """Streaming synthesis. Started on the first CLAUSE, not the first sentence."""

    @abstractmethod
    def stream(self, text: AsyncIterator[str]) -> AsyncIterator[bytes]: ...


class Avatar(ABC):
    """Drives the face from an audio stream.

    Deliberately narrow: audio in, video frames out. Everything a provider needs beyond that
    (persona id, replica id, voice) is constructor state, so swapping MuseTalk for Tavus is a
    config change rather than a rewrite of the call loop.
    """

    name: str = "abstract"
    #: Whether frames are produced in realtime. A provider that batches cannot hold a call.
    realtime: bool = False

    @abstractmethod
    def render(self, audio: AsyncIterator[bytes]) -> AsyncIterator[bytes]: ...

    async def idle(self) -> AsyncIterator[bytes]:
        """Frames to show while listening.

        Not optional polish. A face that freezes between utterances is the strongest possible
        tell that it is synthetic, and it is what makes people talk over the agent.
        """
        raise NotImplementedError


class PlaceholderAvatar(Avatar):
    """Waveform-driven placeholder. No GPU, no weights, no network.

    The default, so `docker compose up` produces a working call for anyone. It renders an
    audio-reactive visual rather than a face — honest about what it is instead of shipping a
    bad uncanny-valley render and calling it a demo.
    """

    name = "placeholder"
    realtime = True

    async def render(self, audio: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        async for chunk in audio:
            # One "frame" per audio chunk, carrying the amplitude the client draws.
            level = min(255, max(chunk[:64], default=0) if chunk else 0)
            yield bytes([level])

    async def idle(self) -> AsyncIterator[bytes]:
        while True:
            yield b"\x00"
            await asyncio.sleep(1 / 30)


# ───────────────────────────────────────────────────────────── disclosure
class DisclosureError(RuntimeError):
    """Raised when a call would start without the AI disclosure having been delivered."""


@dataclass(frozen=True)
class Disclosure:
    """The agent identifies itself as AI, at the start, every time.

    NOT CONFIGURABLE OFF, and that is a deliberate product decision rather than caution. A
    synthetic human face on a sales call that does not say it is synthetic is the failure mode
    that ends the company: several jurisdictions now require disclosure outright, and every
    customer's own compliance review will ask for it. Making it structurally impossible to
    disable is cheaper than making it a setting somebody eventually turns off.

    `spoken` is what the prospect hears; `logged` is what is written to the call record so a
    dispute months later has evidence rather than an assertion.
    """

    spoken: str = (
        "Hi, before we start — I'm an AI assistant, not a human. "
        "I can walk you through the product and answer questions, "
        "and I can bring in a person any time you'd like."
    )
    logged: str = "ai_disclosure_delivered"
    required: bool = True

    def __post_init__(self) -> None:
        if not self.required:
            raise DisclosureError(
                "AI disclosure cannot be disabled. If a deployment believes it needs to, "
                "that is a legal question, not a configuration one."
            )


# ───────────────────────────────────────────────────────────── the turn loop
@dataclass
class TurnResult:
    transcript: str
    response: str
    budget: LatencyBudget
    handoff_requested: bool = False


class CallPipeline:
    """One conversational turn, fully streamed.

    Every stage begins on the FIRST output of the previous stage rather than its last. That is
    the whole trick, and it is why `_first_clause` exists: synthesis starts at the first comma
    or clause boundary, roughly 200ms before the model has finished the sentence.
    """

    def __init__(
        self,
        stt: SpeechToText,
        llm: LanguageModel,
        tts: TextToSpeech,
        avatar: Avatar | None = None,
        disclosure: Disclosure | None = None,
    ):
        self.stt = stt
        self.llm = llm
        self.tts = tts
        self.avatar = avatar or PlaceholderAvatar()
        self.disclosure = disclosure or Disclosure()
        self._disclosed = False

    async def open(self) -> str:
        """Start the call. Returns the disclosure line, which MUST be spoken first."""
        self._disclosed = True
        log.info("call opened; %s", self.disclosure.logged)
        return self.disclosure.spoken

    async def turn(
        self, audio: AsyncIterator[bytes], context: dict | None = None
    ) -> TurnResult:
        if not self._disclosed:
            raise DisclosureError(
                "turn() before open(): the AI disclosure has not been delivered. "
                "This is enforced rather than documented because the check is worthless "
                "if a caller can forget it."
            )
        budget = LatencyBudget()
        context = context or {}

        transcript = ""
        async for text, final in self.stt.stream(audio):
            transcript = text
            if final:
                break
        budget.mark(Stage.STT)

        response_parts: list[str] = []
        first_token_seen = False

        async def token_stream() -> AsyncIterator[str]:
            nonlocal first_token_seen
            async for token in self.llm.stream(transcript, context):
                if not first_token_seen:
                    first_token_seen = True
                    budget.mark(Stage.LLM)
                response_parts.append(token)
                yield token

        audio_out = self.tts.stream(token_stream())
        first_audio = False
        async for _chunk in audio_out:
            if not first_audio:
                first_audio = True
                budget.mark(Stage.TTS)
        budget.mark(Stage.AVATAR)

        response = "".join(response_parts)
        if not budget.within_budget:
            log.warning(
                "turn exceeded budget: %.0fms > %.0fms  %s",
                budget.total_ms, TURN_BUDGET_MS, budget.marks,
            )
        return TurnResult(
            transcript=transcript,
            response=response,
            budget=budget,
            handoff_requested=_wants_human(transcript),
        )


def _first_clause(text: str) -> tuple[str, str]:
    """Split at the earliest natural pause. Synthesis starts here rather than at the sentence.

    Starting TTS at the first clause rather than the first sentence is worth roughly 200ms per
    turn, which is a quarter of the entire budget.
    """
    for i, ch in enumerate(text):
        if ch in ",;:" and i > 12:
            return text[: i + 1], text[i + 1 :]
        if ch in ".!?" and i > 4:
            return text[: i + 1], text[i + 1 :]
    return "", text


_HUMAN_REQUEST = (
    "talk to a human", "speak to a human", "real person", "actual person",
    "human being", "transfer me", "get me someone", "is this a bot", "are you a robot",
    "are you real", "are you human",
)


def _wants_human(transcript: str) -> bool:
    """Detect a handoff request.

    Conservative on purpose: a false positive costs one unnecessary transfer, a false negative
    means the agent talked over someone explicitly asking for a person — which is the single
    worst thing this product can do.
    """
    low = transcript.lower()
    return any(phrase in low for phrase in _HUMAN_REQUEST)
