"""The live call pipeline: hear, think, speak, animate.

THE LATENCY BUDGET IS THE PRODUCT. Human conversation tolerates roughly 300ms of silence
before a pause becomes noticeable and roughly 800ms before it reads as "something is wrong
with this person". Every architectural decision below is downstream of that number.

MEASURED, NOT PROJECTED. This table used to hold the numbers the design was aiming at, which
is a reasonable thing to write before the engines exist and a dishonest thing to leave once
they do. These are fifteen typed turns against Qwen2.5-1.5B on a laptop RTX 5070 and Kokoro-82M
on sixteen CPU cores, reported by `LatencyBudget` on the wire:

    stage                    median      range       who measures it
    ───────────────────────────────────────────────────────────────────────────
    transcription              n/a         n/a       the browser, when spoken
    LLM, to first token       139ms    101-354ms     here
    TTS, to first audio       724ms    561-1022ms    here
    first frame                n/a         n/a       the browser
    ───────────────────────────────────────────────────────────────────────────
    turn, to first sound      850ms    692-1175ms    typed input, no transcription

WHAT THE STREAMING IS WORTH, on one 175-character reply, same text both ways:

    synthesised whole, then played       3007ms of silence first
    synthesised first clause first        407ms of silence first

So the clause-streaming in `clauses.py` is worth about 2.6 seconds a turn, and it is the reason
this is a conversation rather than a form submission.

WHERE THE REMAINING TIME GOES, stated plainly because the budget is missed: synthesis. Kokoro
costs roughly 340ms of fixed setup per call on this CPU regardless of how short the text is
(see `providers.KokoroTextToSpeech`), so no amount of chunking gets the first sound under about
380ms, and the measured 724ms is that floor plus the wait for enough tokens to cut a chunk from.
Moving synthesis to the GPU is the obvious next move and is NOT done here — onnxruntime-gpu is
another dependency and another install step, and this repository's rule is that a clone runs.

WHAT IS VERIFIED AND WHAT IS NOT — stated here rather than discovered by a reader: the
orchestration, budget accounting, disclosure enforcement and both local engines are implemented,
tested, and run end to end; the numbers above came out of that path. The face too: a photoreal
synthetic portrait whose mouth is generated from the audio being played, by Wav2Lip on the local
GPU — see `calls/lipsync.py`, and `calls/avatar.py` for what it degrades to without the
checkpoint and why MuseTalk was tried and rejected.

The face is deliberately OUTSIDE the budget above. Frames follow their audio rather than
delaying it, so generating them cannot make a turn slower; the worst it can do is have her mouth
join a beat into the first clause.
"""

from __future__ import annotations

import asyncio
import logging
import re
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

    def skip(self) -> None:
        """Advance the clock past work this process did not do, recording nothing.

        Without this the choice is between reporting a stage that did not happen and silently
        attributing its elapsed time to the NEXT stage, which is worse: the number would still
        be wrong and would no longer be labelled.
        """
        self._last = time.perf_counter()

    def adopt(self, stage: Stage | str, ms: float) -> None:
        """Record a stage this process did not perform.

        TWO OF THE FOUR STAGES HAPPEN IN THE BROWSER and the server cannot see either.
        Transcription — including the endpointing inside it, which the browser's recogniser does
        not report separately — belongs to the client, and so does the avatar's first frame.
        Estimating them here would make the strip a drawing of a budget rather than a
        measurement of one, so the client measures them and sends the numbers with its next
        message.
        """
        self.marks[str(stage)] = round(max(0.0, float(ms)), 2)

    def report(self) -> dict[str, float | bool]:
        return {**self.marks, "total_ms": self.total_ms, "within_budget": self.within_budget}


# ───────────────────────────────────────────────────────────── what synthesis hands back
@dataclass(slots=True)
class Clip:
    """One synthesised chunk of a reply: the audio, and what it is audio *of*.

    RAW BYTES WERE NOT ENOUGH, and the face is what proved it. Synthesis used to yield
    `bytes`, so nothing downstream knew what was being said or for how long — which left the
    console animating the mouth from a hardcoded 52ms-per-character timer, guessing at audio it
    was holding in its hand. A clip carries its own text and duration, so the viseme sequence
    and the moment the mouth stops are both derived from the thing actually being played.
    """

    #: What was said, as it was written. This is the caption, and it keeps its "$", its
    #: "stripe.com" and its capital letters.
    text: str
    wav: bytes
    sample_rate: int = 24_000
    duration_ms: float = 0.0
    generate_ms: float = 0.0
    index: int = 0
    #: True when there is no local voice and the client must speak this text itself.
    browser_voice: bool = False
    #: The same words, spelled for a voice rather than for an eye — see `calls/speech.say`.
    #: Empty means "no different from `text`"; read it through `to_say` rather than directly.
    spoken: str = ""

    @property
    def to_say(self) -> str:
        """What a synthesiser should be given. Never the caption, when the two differ."""
        return self.spoken or self.text


# ───────────────────────────────────────────────────────────── provider interfaces
class SpeechToText(ABC):
    """Streaming transcription. Yields partials, then a final on endpoint."""

    #: Whether timing this provider measures anything real. False for an adapter that receives
    #: text already transcribed elsewhere: the queue read takes microseconds, and reporting it
    #: as the transcription stage puts a confident "stt 0ms" on the strip for work that either
    #: happened in the browser or, for typed input, never happened at all.
    measured: bool = True

    @abstractmethod
    def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[tuple[str, bool]]: ...


class LanguageModel(ABC):
    """Token-streaming reasoning. MUST stream: waiting for a complete response is the single
    largest avoidable cost in the budget."""

    @abstractmethod
    def stream(self, prompt: str, context: dict) -> AsyncIterator[str]: ...


class TextToSpeech(ABC):
    """Streaming synthesis. Started on the first CLAUSE, not the first sentence.

    One abstract method, deliberately: `clips` is strictly more than a byte stream and
    everything that only wants bytes — the avatar — gets them from the concrete `stream`
    below. Two abstract methods would let an implementation satisfy one and not the other.
    """

    @abstractmethod
    def clips(self, text: AsyncIterator[str]) -> AsyncIterator[Clip]: ...

    async def stream(self, text: AsyncIterator[str]) -> AsyncIterator[bytes]:
        """Audio only, for consumers that have no use for the words — chiefly `Avatar`."""
        async for clip in self.clips(text):
            yield clip.wav


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


#: How many sentences the agent is allowed per turn.
#:
#: THE PROMPT ASKS FOR THIS AND THE MODEL DOES NOT COMPLY. Measured over five real turns with
#: "One or two sentences. Never more." at the top of the system prompt, Qwen2.5-1.5B produced
#: replies of 308, 365, 416, 429 and 462 characters — four to six sentences every time. At 110
#: max tokens the last of them was cut off mid-word, so the agent ended turns by trailing into
#: silence mid-sentence, which is worse than either a long reply or a short one.
#:
#: So the limit is enforced rather than requested, for the same reason the handoff is: a rule
#: that matters is not left to a 1.5B model's judgement. Two sentences is also what makes the
#: latency work — the reply finishes while the listener is still hearing the first clause.
MAX_SENTENCES = 2

#: A sentence ends at punctuation followed by space or end of text. The lookahead is what keeps
#: "40.5" and "rainmaker.io" from ending a turn.
_SENTENCE_END = re.compile(r"[.!?]+(?=\s|$)")


async def cap_sentences(
    stream: AsyncIterator[str], limit: int = MAX_SENTENCES
) -> AsyncIterator[str]:
    """Pass tokens through until `limit` sentences are complete, then stop the model.

    The generator is closed rather than merely abandoned, so generation actually stops and the
    GPU is free for the next turn. Dropping the reference instead would leave the model writing
    three more sentences nobody will hear, on the one device the next turn is waiting for.
    """
    if limit <= 0:
        return

    emitted = ""
    tokens = stream.__aiter__()
    try:
        async for token in tokens:
            emitted += token
            ends = list(_SENTENCE_END.finditer(emitted))
            if len(ends) < limit:
                yield token
                continue

            # Yield only the part of this token that belongs to the final sentence. Emitting
            # the whole token would let a stray fragment of sentence three reach synthesis.
            cut = ends[limit - 1].end()
            keep = len(token) - (len(emitted) - cut)
            if keep > 0:
                yield token[:keep]
            return
    finally:
        await tokens.aclose()



#: A figure with money attached, in the shapes a model actually writes it.
_NUMBER_WORDS = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|"
    "forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million"
)
_MONEY = re.compile(
    # "$50", "£1,250.00", "$4.8k"
    r"[$£€]\s?\d[\d,]*(?:\.\d+)?(?:\s?[km]\b)?"
    # "2,400 dollars", "45 pence"
    r"|\b\d[\d,]*(?:\.\d+)?\s*(?:dollars|pounds|euros|cents|pence|usd|gbp|eur)\b"
    # "fifty dollars", "two thousand pounds" — what a voice agent actually says out loud
    rf"|\b(?:{_NUMBER_WORDS})(?:[\s-]+(?:{_NUMBER_WORDS}))*\s+"
    r"(?:dollars|pounds|euros|cents|pence)\b",
    re.IGNORECASE,
)

#: What replaces one. Always true, and grammatical where a figure usually sits: "it's $50"
#: becomes "it's the figure on your screen", and "starting from $50" keeps its preposition.
PRICE_STANDIN = "the figure on your screen"

#: Anything that could be the start of a figure. Holding back only once one of these has
#: appeared is what keeps this off the latency path: ordinary speech has no digits in it, so a
#: turn without numbers is not delayed by a single token.
_MONEY_MAYBE = re.compile(rf"[$£€\d]|\b(?:{_NUMBER_WORDS})\b", re.IGNORECASE)

#: The words a money phrase is built from, besides the numbers: "fifty DOLLARS".
_CURRENCY_NOUNS = ("dollars", "pounds", "euros", "cents", "pence", "usd", "gbp", "eur")

#: Every prefix of every word that could still become part of a figure. "f" might be "fifty";
#: "doll" might be "dollars"; "GPU" is never going to be either.
_WORD_PREFIXES = frozenset(
    word[:n]
    for word in (*_NUMBER_WORDS.split("|"), *_CURRENCY_NOUNS)
    for n in range(1, len(word) + 1)
)

#: A complete word that a figure can be made of.
_MONEY_ATOM = re.compile(
    rf"^(?:[$£€]?\d[\d,.]*|(?:{_NUMBER_WORDS})|{'|'.join(_CURRENCY_NOUNS)})$",
    re.IGNORECASE,
)


def _could_still_be_money(text: str) -> bool:
    """Whether `text` might yet turn into a figure if more of it arrives.

    THE ALTERNATIVE WAS A FIXED WINDOW, and it was worse: holding everything for forty-six
    characters after any digit delays an agent whose job includes saying "sixty-four nodes are
    free right now". This releases as soon as a word arrives that no figure could contain —
    usually the very next one.
    """
    body = text.strip()
    if not body:
        return True
    parts = [part for part in re.split(r"[\s,-]+", body) if part]
    *complete, last = parts
    if any(not _MONEY_ATOM.match(word) for word in complete):
        return False
    # A LONE CURRENCY SYMBOL IS THE MOST IMPORTANT PARTIAL THERE IS. Token by token, "$50"
    # arrives as "$" and then "5": releasing the "$" on its own put the symbol beyond recall and
    # the price was spoken with only its digits redacted.
    if last in ("$", "£", "€"):
        return True
    return bool(_MONEY_ATOM.match(last)) or last.lower() in _WORD_PREFIXES


def _figure_start(text: str) -> int | None:
    """Where a figure that is still arriving begins, if one is.

    Everything before it can be spoken; everything from it has to wait, because a price is not
    a price until the currency word lands.
    """
    for match in _MONEY_MAYBE.finditer(text):
        if _could_still_be_money(text[match.start() :]):
            return match.start()
    # A half-arrived word that could become a number: "That's f" holds, "That's GPU" does not.
    cut = text.rfind(" ") + 1
    if cut < len(text) and text[cut:].lower() in _WORD_PREFIXES:
        return cut
    return None


async def guard_prices(stream: AsyncIterator[str], allowed: bool = False) -> AsyncIterator[str]:
    """Stop a figure the MODEL invented from being said out loud.

    THIS IS THE GUARD THE WHOLE PRICING DESIGN RESTED ON AND DID NOT HAVE. Everything else was
    in place — the quote is arithmetic, the sentence stating it is written in code, the prompt
    tells the model the number has already been said — and then a 1.5B model, asked what was
    available, said "starting from $50 per GPU per hour" about a product that charges $2.40.
    Nothing stopped it, because nothing was looking. A rule the model is asked to follow is a
    request. This is the enforcement.

    ONLY THE MODEL'S STREAM PASSES THROUGH HERE. Lines the platform writes — the quote, the
    checkout, the times offered — never reach it, which is exactly the distinction being drawn:
    the platform may state a figure it computed, and the model may not state one at all.

    Streaming, and cheap where it counts. Tokens are held back only while they could still be
    becoming a figure, so a turn with no numbers in it is not delayed at all and a turn that
    mentions sixty-four nodes is delayed by one word.
    """
    if allowed:
        async for token in stream:
            yield token
        return

    pending = ""
    async for token in stream:
        pending += token
        start = _figure_start(pending)
        safe, pending = (pending[:start], pending[start:]) if start is not None else (pending, "")
        if safe:
            yield _MONEY.sub(PRICE_STANDIN, safe)
    if pending:
        yield _MONEY.sub(PRICE_STANDIN, pending)


# ───────────────────────────────────────────────────────────── the turn loop
@dataclass
class TurnResult:
    transcript: str
    response: str
    budget: LatencyBudget
    handoff_requested: bool = False


# ───────────────────────────────────────────────────────────── what a turn emits
@dataclass(slots=True)
class Heard:
    """A transcript, partial or final."""

    text: str
    final: bool


@dataclass(slots=True)
class Thought:
    """One token from the model, as it was produced."""

    token: str


@dataclass(slots=True)
class Spoke:
    """One synthesised chunk, ready to play."""

    clip: Clip


@dataclass(slots=True)
class Finished:
    """The turn is over. Carries the budget and whether a human was asked for."""

    result: TurnResult


TurnEvent = Heard | Thought | Spoke | Finished


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
        max_sentences: int = MAX_SENTENCES,
        may_speak_prices: bool = False,
    ):
        self.stt = stt
        self.llm = llm
        self.tts = tts
        self.max_sentences = max_sentences
        #: Whether the MODEL may state a figure. False everywhere by default; the platform's own
        #: computed sentences do not go through the model path and are unaffected.
        self.may_speak_prices = may_speak_prices
        self.avatar = avatar or PlaceholderAvatar()
        self.disclosure = disclosure or Disclosure()
        self._disclosed = False

    async def open(self) -> str:
        """Start the call. Returns the disclosure line, which MUST be spoken first."""
        self._disclosed = True
        log.info("call opened; %s", self.disclosure.logged)
        return self.disclosure.spoken

    async def stream_turn(
        self,
        audio: AsyncIterator[bytes],
        context: dict | None = None,
        *,
        announce: bool = True,
    ) -> AsyncIterator[TurnEvent]:
        """One turn, emitting each piece the instant it exists.

        THIS IS THE METHOD A LIVE CALL USES. `turn()` below drains it and hands back the
        finished result, which is the right shape for a test and the wrong shape for a socket:
        a caller that waits for the return value cannot start playing the first clause, and
        starting on the first clause is the entire latency argument of this project.

        Tokens and clips are interleaved through one queue rather than returned as two streams.
        The alternative — the caption stream and the audio stream as separate iterators — reads
        cleaner and lets them drift, so the words on screen would stop matching the words in the
        ear at exactly the moment someone is watching closely.
        """
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
            # `announce=False` for a turn the AGENDA started rather than the prospect. The
            # steering text is a stage direction — "(You are screen-sharing their pricing
            # page...)" — and emitting it as `Heard` put it on screen as the caption and into
            # the transcript as something the prospect had said. The instructions to the actor
            # were being read out to the audience.
            if announce:
                yield Heard(text, final)
            if final:
                break
        if getattr(self.stt, "measured", True):
            budget.mark(Stage.STT)
        else:
            budget.skip()

        response_parts: list[str] = []
        first_token_seen = False
        events: asyncio.Queue[TurnEvent | None] = asyncio.Queue()

        async def token_stream() -> AsyncIterator[str]:
            nonlocal first_token_seen
            capped = cap_sentences(self.llm.stream(transcript, context), self.max_sentences)
            async for token in guard_prices(capped, self.may_speak_prices):
                if not first_token_seen:
                    first_token_seen = True
                    budget.mark(Stage.LLM)
                response_parts.append(token)
                events.put_nowait(Thought(token))
                yield token

        async def synthesise() -> None:
            first_clip = True
            try:
                async for clip in self.tts.clips(token_stream()):
                    if first_clip:
                        first_clip = False
                        budget.mark(Stage.TTS)
                    events.put_nowait(Spoke(clip))
            except Exception:  # noqa: BLE001
                # A synthesis failure must end the turn, not wedge it. The prospect gets a
                # short reply and a log line; a hung socket would look like the agent ignoring
                # them, which is worse than a bad answer.
                log.exception("synthesis failed mid-turn")
            finally:
                events.put_nowait(None)

        worker = asyncio.create_task(synthesise())
        try:
            while True:
                event = await events.get()
                if event is None:
                    break
                yield event
        finally:
            # Barge-in: the consumer stops iterating and the generator is closed. Without this
            # the model keeps generating into a queue nobody reads, and the next turn queues
            # behind a GPU still busy with an answer the prospect interrupted.
            if not worker.done():
                worker.cancel()

        response = "".join(response_parts)
        if not budget.within_budget:
            log.warning(
                "turn exceeded budget: %.0fms > %.0fms  %s",
                budget.total_ms, TURN_BUDGET_MS, budget.marks,
            )
        yield Finished(
            TurnResult(
                transcript=transcript,
                response=response,
                budget=budget,
                handoff_requested=_wants_human(transcript),
            )
        )

    async def turn(
        self, audio: AsyncIterator[bytes], context: dict | None = None
    ) -> TurnResult:
        """The whole turn, awaited. Built on `stream_turn` so there is one turn loop, not two."""

        result: TurnResult | None = None
        async for event in self.stream_turn(audio, context):
            if isinstance(event, Finished):
                result = event.result
        if result is None:  # pragma: no cover — structurally impossible, cheap to assert
            raise RuntimeError("stream_turn ended without a Finished event")
        return result


_HUMAN_REQUEST = (
    "real person", "actual person", "human being", "transfer me", "get me someone",
    "is this a bot", "are you a robot", "are you real", "are you human",
)

#: WHAT PEOPLE CALL A PERSON, WHICH IS USUALLY THEIR JOB. Nobody says "may I speak to a human"
#: — they ask for an engineer, a rep, someone technical. The phrase list held "talk to a human"
#: and four variants of it, so "can I talk to an engineer?" went to the model as an ordinary
#: question and the agent carried on selling. That is the single worst thing this product can
#: do, and it survived until somebody drove a whole call and asked the way a buyer would.
_HUMAN_ROLE = (
    r"human|person|someone|somebody|rep|engineer|specialist|expert|advisor|adviser|"
    r"consultant|manager|agent|sales(?:person)?|account manager|technical"
)
_ASK_FOR_A_PERSON = re.compile(
    r"\b(?:talk|speak|chat|connect me)\b[^.?!]{0,16}?\b(?:to|with)\b\s+"
    r"(?:an?\s+|the\s+|your\s+)?(?:" + _HUMAN_ROLE + r")\b",
    re.IGNORECASE,
)


def _wants_human(transcript: str) -> bool:
    """Detect a handoff request.

    Conservative on purpose: a false positive costs one unnecessary transfer, a false negative
    means the agent talked over someone explicitly asking for a person — which is the single
    worst thing this product can do.
    """
    low = transcript.lower()
    if any(phrase in low for phrase in _HUMAN_REQUEST):
        return True
    return bool(_ASK_FOR_A_PERSON.search(transcript))
