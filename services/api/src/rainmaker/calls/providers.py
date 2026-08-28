"""Concrete engines behind the four provider interfaces in `pipeline.py`.

WHY THIS FILE EXISTS. For a long time `pipeline.py` declared `SpeechToText`, `LanguageModel`,
`TextToSpeech` and `Avatar` as abstract base classes and nothing in the repository implemented
them — the only subclasses were fakes in the test suite. The orchestration was real and tested,
and the agent could not say a word. This is the other half.

EVERYTHING HERE RUNS ON THE MACHINE IT IS INSTALLED ON. No API key, no account, no per-call
cost, nothing leaving the box. That is not frugality: a sales agent handles pipeline data and
recorded conversations, so "the audio never leaves" is a procurement answer, and it is also the
only way this repository stays runnable by someone who has just cloned it.

    hearing   the browser's SpeechRecognition, transcribed client-side  (`ClientSpeechToText`)
    thinking  Qwen2.5-1.5B-Instruct, streamed token by token            (`LocalLanguageModel`)
    speaking  Kokoro-82M, synthesised clause by clause                  (`KokoroTextToSpeech`)

THE FALLBACKS ARE NOT DECORATION. A clone with no model weights must still hold a conversation,
or the demo is a screenshot. `ScriptedLanguageModel` answers from a small grounded script and
`SilentTextToSpeech` hands synthesis to the browser's own voice. Both are worse, both say so in
`engines()`, and the console shows which is running rather than quietly sounding bad.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import threading
import time
import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agents.spec import DEFAULT_VOICE, VOICE_CATALOGUE
from .clauses import split_clauses, take_speakable
from .speech import say
from .pipeline import Clip, LanguageModel, SpeechToText, TextToSpeech

log = logging.getLogger("rainmaker.calls.providers")

MODEL_DIR = Path(
    os.environ.get("RAINMAKER_MODELS", Path(__file__).resolve().parents[3] / "models")
)
KOKORO_MODEL = MODEL_DIR / "kokoro-v1.0.onnx"
KOKORO_VOICES = MODEL_DIR / "voices-v1.0.bin"

#: Small enough to load in ~4s and stream at a useful rate on a laptop GPU, large enough to
#: follow a system prompt and stay on topic for the length of a sales turn. The 0.5B variant is
#: barely faster: at this size the GPU is latency-bound, not throughput-bound.
DEFAULT_LLM = os.environ.get("RAINMAKER_LLM", "Qwen/Qwen2.5-1.5B-Instruct")

#: A spoken reply that runs long is unusable however good the prose is — the prospect cannot
#: skim it, and every extra token delays the next turn.
MAX_NEW_TOKENS = 110


# ───────────────────────────────────────────────────────────── audio
def to_wav(samples: Any, rate: int) -> bytes:
    """Float samples to a 16-bit PCM WAV.

    A container rather than raw PCM because the browser needs to know the sample rate, and
    getting that wrong produces audio at the wrong pitch — which sounds like a broken model
    rather than a broken header, and gets debugged accordingly.
    """
    import numpy as np

    clipped = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    pcm = (clipped * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())
    return buffer.getvalue()


# ───────────────────────────────────────────────────────────── hearing
class ClientSpeechToText(SpeechToText):
    """Transcription that already happened, in the browser.

    THE TRANSCRIBER IS THE BROWSER'S OWN `SpeechRecognition`, and that is a deliberate choice
    rather than a shortcut. It is free, needs no download, streams interim results, and — the
    part that matters on a laptop — leaves the CPU entirely free for the two models that are
    actually latency-critical. Running faster-whisper server-side would compete with Kokoro for
    the same cores and make the reply slower to arrive than the transcription was to produce.

    The honest cost, stated because the rest of this project is local-first: Chrome's
    implementation sends audio to Google. Typing is the path that does not, and the console
    says so.

    This adapter exists so the pipeline's contract does not bend around that. Text arrives over
    the WebSocket as `(text, final)` and is replayed here as the stream the pipeline expects.
    """

    name = "browser"
    #: Nothing is transcribed here, so nothing here is worth timing. The client sends its own
    #: `stt_ms` when it spoke, and a typed turn has no transcription stage at all.
    measured = False

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, bool]] = asyncio.Queue()

    def offer(self, text: str, *, final: bool) -> None:
        """Called by the WebSocket handler when the client sends a partial or a final."""
        self._queue.put_nowait((text, final))

    async def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[tuple[str, bool]]:
        # `audio` is unused: the bytes never reach the server in this configuration. The
        # parameter stays because a server-side engine slots in here unchanged.
        while True:
            text, final = await self._queue.get()
            yield text, final
            if final:
                return


# ───────────────────────────────────────────────────────────── thinking
class LocalLanguageModel(LanguageModel):
    """Qwen on the local GPU, streamed token by token.

    STREAMING IS NOT AN OPTIMISATION HERE, it is the design. Everything in `pipeline.py` starts
    the next stage on the FIRST token of this one; a wrapper that returned a finished string
    would cost roughly 400ms per turn and make the project's entire latency claim false. So the
    only generation method is an async iterator and there is deliberately no `generate()`.
    """

    name = "qwen"

    def __init__(self, model_name: str = DEFAULT_LLM, device: str | None = None) -> None:
        self.model_name = model_name
        self.device = device
        self._tokenizer: Any = None
        self._model: Any = None
        self._lock = threading.Lock()
        #: One generation at a time. The GPU has a single queue anyway, and two concurrent calls
        #: on a laptop would make both slow rather than one fast — a live call would much rather
        #: be fast for the person currently speaking.
        self._gpu = asyncio.Semaphore(1)
        self.load_seconds = 0.0

    @property
    def available(self) -> bool:
        from importlib.util import find_spec

        return find_spec("torch") is not None and find_spec("transformers") is not None

    @property
    def ready(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        """Load the weights at startup, never on the first turn.

        A lazily-loaded model makes the first prospect of the day wait four seconds for a
        greeting. That is the worst available place to spend it, and it does not show up in any
        average.
        """
        with self._lock:
            if self._model is not None:
                return
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer

            started = time.perf_counter()
            device = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
            self._tokenizer = AutoTokenizer.from_pretrained(self.model_name)
            self._model = AutoModelForCausalLM.from_pretrained(
                self.model_name,
                dtype=torch.float16 if device == "cuda" else torch.float32,
                device_map=device,
            )
            self._model.eval()
            self.device = device
            self.load_seconds = time.perf_counter() - started
            log.info("loaded %s on %s in %.1fs", self.model_name, device, self.load_seconds)
            self._warm()

    def _warm(self) -> None:
        """One throwaway generation, on a prompt the shape a real turn has.

        A single-token warm-up is not enough, and measuring is what showed it: the first real
        reply took ~1200ms to its first token while every reply after it took ~110ms. One token
        exercises prefill and leaves the decode loop, the sampler and the attention kernels for
        the prospect to pay for. The prompt LENGTH matters as much as the token count — a real
        turn carries the call rules, the persona, the research facts and the history, close to a
        thousand tokens, and kernels are compiled per shape.
        """
        try:
            filler = (
                "Pricing: Team is 40 dollars per seat per month, Business is 75, Enterprise is "
                "quoted. Deployment: cloud, or self-hosted in the customer's own VPC. "
                "Security: SOC 2 Type II, data residency in the EU on request.\n"
            ) * 4
            enc = self._encode(
                [
                    {"role": "system", "content": filler},
                    {
                        "role": "user",
                        "content": "what does it cost at our size, and can we self-host?",
                    },
                ]
            )
            self._model.generate(
                **enc,
                max_new_tokens=24,
                do_sample=False,
                pad_token_id=self._tokenizer.eos_token_id,
            )
            log.info("warmed on a %d-token prompt", enc["input_ids"].shape[-1])
        except Exception:  # noqa: BLE001 — warming is best effort
            log.debug("warm-up failed; the first turn will be slower", exc_info=True)

    def _encode(self, messages: list[dict[str, str]]) -> Any:
        text = self._tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=False
        )
        return self._tokenizer(text, return_tensors="pt").to(self._model.device)

    async def stream(self, prompt: str, context: dict) -> AsyncIterator[str]:
        """Yield text as the model produces it.

        `TextIteratorStreamer` on a worker thread, because `generate` blocks and holding the
        event loop for a whole reply would stall the WebSocket that is waiting to forward the
        first clause.
        """
        if self._model is None:
            self.load()

        from transformers import TextIteratorStreamer

        messages = [{"role": "system", "content": context.get("system", "")}]
        messages += list(context.get("history", []))
        messages.append({"role": "user", "content": prompt})

        async with self._gpu:
            enc = self._encode(messages)
            streamer = TextIteratorStreamer(
                self._tokenizer, skip_prompt=True, skip_special_tokens=True
            )
            temperature = float(context.get("temperature", 0.4))
            kwargs: dict[str, Any] = {
                **enc,
                "max_new_tokens": int(context.get("max_new_tokens", MAX_NEW_TOKENS)),
                "streamer": streamer,
                "pad_token_id": self._tokenizer.eos_token_id,
                # `do_sample=True` with a near-zero temperature is a numerically unstable way of
                # asking for greedy decoding.
                "do_sample": temperature > 0.05,
            }
            if temperature > 0.05:
                kwargs["temperature"] = temperature
                kwargs["top_p"] = 0.9

            threading.Thread(target=self._model.generate, kwargs=kwargs, daemon=True).start()

            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[str | None] = asyncio.Queue()

            def pump() -> None:
                # The streamer is a blocking iterator; bridge it onto the loop rather than
                # polling, so a slow model does not spin a core.
                for piece in streamer:
                    loop.call_soon_threadsafe(queue.put_nowait, piece)
                loop.call_soon_threadsafe(queue.put_nowait, None)

            threading.Thread(target=pump, daemon=True).start()

            while True:
                piece = await queue.get()
                if piece is None:
                    return
                yield piece


_INTENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\b(cost|price|pricing|budget|expensive|how much)\b", re.I),
        "Pricing depends on seats and where it runs, and I would rather have a person quote "
        "you than guess at it. Can I have someone send exact numbers today?",
    ),
    (
        re.compile(r"\b(postgres|already (use|have|run)|why (would|do) we need)\b", re.I),
        "That is a fair question — most teams we speak to already have something in place. "
        "We tend to sit alongside it rather than replace it.",
    ),
    (
        re.compile(r"\b(secur\w*|soc ?2|gdpr|compliance|residency|privacy)\b", re.I),
        "Everything can run inside your own environment, so the data never leaves it. I can "
        "have our security documentation sent over if that would help.",
    ),
    (
        re.compile(r"\b(integrat\w*|api|connect|webhook|salesforce|hubspot)\b", re.I),
        "There is an API for everything the interface does, and the common CRMs are supported. "
        "Which one are you on?",
    ),
    (
        re.compile(r"\b(offline|no internet|connection|sync)\b", re.I),
        "That is the part I would actually show you — the console keeps working with the "
        "network off, and syncs when it comes back.",
    ),
    (
        re.compile(r"\b(hi|hello|hey|morning|afternoon)\b", re.I),
        "Good to meet you. What made you take the call today?",
    ),
)

_FALLBACK_REPLY = (
    "I want to answer that properly rather than guess. Tell me a little about how your team "
    "handles it today?"
)


@dataclass
class ScriptedLanguageModel(LanguageModel):
    """A model-shaped object with no model behind it.

    NOT A MOCK — this is what a clone with no weights actually talks to, so it has to hold a
    plausible sales conversation rather than return a fixed string. It matches the prospect's
    question against a handful of intents, which keeps every layer above it exercised on a
    machine with no GPU.

    It streams word by word at roughly the rate a 1.5B model manages, so the latency arithmetic
    downstream still means something instead of completing instantly and hiding a missing await.
    """

    name: str = "scripted"
    ms_per_word: float = 45.0
    calls: list[str] = field(default_factory=list)

    async def stream(self, prompt: str, context: dict) -> AsyncIterator[str]:
        self.calls.append(prompt)
        reply = next((r for pattern, r in _INTENTS if pattern.search(prompt)), _FALLBACK_REPLY)
        for word in reply.split():
            await asyncio.sleep(self.ms_per_word / 1000)
            yield word + " "


# ───────────────────────────────────────────────────────────── speaking
class KokoroTextToSpeech(TextToSpeech):
    """Kokoro-82M, loaded once and shared by every call.

    WHY NOT THE BROWSER'S OWN VOICE. `speechSynthesis` is free and instant and sounds like a
    train station announcement — on Windows it falls back to SAPI voices from about 2005. For a
    product whose entire pitch is that talking to it feels like a conversation, the voice is the
    first thing anyone judges. Kokoro is Apache-2.0, 82M parameters, ~330MB, and runs on CPU.

    It synthesises at 1.7x realtime on a short phrase and 2.9x on a long one, which sounds like
    a problem and is not: audio is produced clause by clause and playback starts on the first
    one while the rest is still being generated. Every chunk after the first is produced faster
    than the previous one is spoken.
    """

    name = "kokoro"

    #: The voices a tenant may pick. Defined in `agents/spec.py` and imported rather than
    #: restated, because a copy of this table lived here and the two stopped agreeing — see the
    #: comment on `VOICE_CATALOGUE`. The engine and the validator now cannot disagree.
    VOICES = VOICE_CATALOGUE

    #: Natural pace. This was 1.05 on the theory that synthesised speech reads as slower than it
    #: measures — true of a monotone reader, and the wrong correction here: sped up, Kokoro
    #: clips its own phrase endings, and a voice that never quite finishes a word is one of the
    #: things people mean when they say a voice sounds synthetic.
    DEFAULT_SPEED = float(os.environ.get("RAINMAKER_VOICE_SPEED", "1.0"))

    def __init__(
        self,
        model: Path = KOKORO_MODEL,
        voices: Path = KOKORO_VOICES,
        *,
        voice: str = DEFAULT_VOICE,
        speed: float | None = None,
    ) -> None:
        self.model_path = model
        self.voices_path = voices
        self.voice = voice
        self.speed = speed if speed is not None else self.DEFAULT_SPEED
        self._kokoro: Any = None
        self._lock = threading.Lock()
        #: One synthesis at a time: the session is not thread-safe, and two concurrent calls
        #: would make both slow rather than one fast.
        self._gate = asyncio.Semaphore(1)
        self.load_seconds = 0.0

    @property
    def available(self) -> bool:
        from importlib.util import find_spec

        return (
            self.model_path.exists()
            and self.voices_path.exists()
            and find_spec("kokoro_onnx") is not None
        )

    @property
    def ready(self) -> bool:
        return self._kokoro is not None

    def load(self) -> None:
        if self._kokoro is not None or not self.available:
            return
        with self._lock:
            if self._kokoro is not None:
                return
            import onnxruntime as ort
            from kokoro_onnx import Kokoro

            started = time.perf_counter()

            # HALF THE CORES, AND THE NUMBER IS MEASURED. By default onnxruntime spawns a
            # worker per core and saturates them — and it shares this box with a language model
            # that needs CPU-side work of its own to pump tokens out of the streamer. But
            # starving it is far worse than sharing: on a ten-character opening chunk, two
            # threads cost 945ms, eight cost 452ms and all sixteen cost 382ms. The curve is
            # flat past half the cores, so half is where this sits — nearly all of the speed,
            # and the model still gets somewhere to run.
            options = ort.SessionOptions()
            options.intra_op_num_threads = max(2, (os.cpu_count() or 8) // 2)
            options.inter_op_num_threads = 1
            session = ort.InferenceSession(
                str(self.model_path), options, providers=["CPUExecutionProvider"]
            )
            self._kokoro = Kokoro.from_session(session, str(self.voices_path))

            # A cold ONNX session pays graph optimisation and arena setup on its first call.
            # A live opening line is the worst available place for it.
            try:
                self._kokoro.create("Ready.", voice="af_heart", speed=1.0, lang="en-us")
            except Exception:  # noqa: BLE001 — warming is best effort
                log.debug("voice warm-up failed; the first clause will be slower", exc_info=True)

            self.load_seconds = time.perf_counter() - started
            log.info("loaded Kokoro in %.1fs", self.load_seconds)

    #: Leave a little headroom below full scale. Not taste — the browser mixes these clips and
    #: anything at 1.0 has nowhere to go.
    PEAK_CEILING = 0.89

    def _synth(self, text: str) -> Clip:
        # THE VOICE READS `spoken`, THE SCREEN READS `text`. Kokoro is handed a string with the
        # markdown taken out, the abbreviations expanded and the URLs made pronounceable; the
        # caption keeps the version a person would rather look at. See `calls/speech`.
        spoken = say(text)
        kokoro_voice, lang = self.VOICES.get(self.voice, self.VOICES[DEFAULT_VOICE])
        started = time.perf_counter()
        samples, rate = self._kokoro.create(
            spoken, voice=kokoro_voice, speed=self.speed, lang=lang
        )
        return Clip(
            text=text,
            spoken=spoken,
            wav=to_wav(self._level(samples), rate),
            sample_rate=rate,
            duration_ms=len(samples) / rate * 1000,
            generate_ms=(time.perf_counter() - started) * 1000,
        )

    def _level(self, samples: Any) -> Any:
        """Bring a clip under full scale, and only ever downwards.

        MEASURED, NOT PRECAUTIONARY. Reading this product's own quote sentence, `bf_isabella`
        peaked at 1.14 — above full scale — and the served WAV came back with samples pinned at
        the ceiling. That is clipping: a hard, buzzy distortion on exactly the loudest syllables,
        which is one of the things people are hearing when they say a synthesised voice sounds
        cheap. Voices vary by more than 2x in level, so this cannot be fixed by picking a good
        one and hoping.

        Downwards only, deliberately. Normalising quiet clips UP would make the level jump
        between one sentence and the next, which is worse than a quiet voice and much harder to
        diagnose.

        WHY THERE IS NO CLAUSE-TO-CLAUSE LOUDNESS MATCHING HERE, having gone looking for it. A
        reply is cut into clauses for latency and each is synthesised without knowledge of its
        neighbours, so the obvious worry is that their levels step mid-sentence. Measured over
        this agent's own script, per-clause RMS:

            af_heart   0.0730 - 0.0884   median 0.0773   spread 1.21x
            af_bella   0.0770 - 0.0906   median 0.0841   spread 1.18x
            bf_emma    0.0933 - 0.1145   median 0.1010   spread 1.23x

        About 1.7dB inside a voice, which is at the edge of audible — and the medians differ by
        30% BETWEEN voices, so any fixed target would change a voice's character rather than
        even out its clauses. A self-calibrating target would fix that, and cannot live here:
        `state.tts` is one process-wide instance shared by every concurrent call, so running
        level state would mix clauses from different conversations and different voices.

        The measurement did find something real — the short opening chunk is consistently the
        loudest clause of a turn on all three voices — but 1.7dB of it, and the fix for it is
        stateful. Left alone on purpose; the note is here so the next person measures before
        adding a compressor rather than after.
        """
        import numpy as np

        audio = np.asarray(samples, dtype=np.float32)
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > self.PEAK_CEILING:
            audio = audio * (self.PEAK_CEILING / peak)
        return audio

    async def clips(self, text: AsyncIterator[str]) -> AsyncIterator[Clip]:
        """Synthesise a streaming reply, yielding each chunk the moment it is ready.

        The buffer is cut by `clauses.take_speakable`, which is deliberately impatient about the
        first chunk and patient about every one after it.
        """
        if self._kokoro is None:
            self.load()

        index = 0
        buffer = ""
        async for token in text:
            buffer += token
            ready, buffer = take_speakable(buffer, opened=index > 0)
            if ready:
                yield await self._one(ready, index)
                index += 1
        for tail in split_clauses(buffer, opened=index > 0):
            yield await self._one(tail, index)
            index += 1

    async def _one(self, text: str, index: int) -> Clip:
        async with self._gate:
            # Off the event loop: synthesis is CPU-bound and holding the loop would stall the
            # socket that is waiting to forward this very clip.
            clip = await asyncio.to_thread(self._synth, text)
        clip.index = index
        return clip

class SilentTextToSpeech(TextToSpeech):
    """No local voice — the browser speaks the text instead.

    THE AGENT ALWAYS TALKS. That is a product rule, not a nice-to-have: an agent that answers in
    silence on a machine without the weights is a broken demo, and a reviewer who has just
    cloned this repository will not download 330MB before forming an opinion. So when Kokoro is
    missing, the clips carry text and no audio, and the console hands them to `speechSynthesis`.

    It sounds markedly worse, which is why `engines()` reports it and the console labels it.
    Durations are estimated from a speaking rate so the latency strip and the mouth animation
    stay honest rather than snapping to zero.
    """

    name = "browser"

    #: Characters per second of speech. Measured against Kokoro's output at the same speed, so
    #: the estimate and the real thing animate the face at the same pace.
    CHARS_PER_SECOND = 15.5

    async def clips(self, text: AsyncIterator[str]) -> AsyncIterator[Clip]:
        index = 0
        buffer = ""
        async for token in text:
            buffer += token
            ready, buffer = take_speakable(buffer, opened=index > 0)
            if ready:
                yield self._estimate(ready, index)
                index += 1
        for tail in split_clauses(buffer, opened=index > 0):
            yield self._estimate(tail, index)
            index += 1

    def _estimate(self, text: str, index: int) -> Clip:
        # The browser is the synthesiser on this path, so it gets the spoken spelling too —
        # `speechSynthesis` reads "asterisk asterisk" as readily as Kokoro does. The estimate is
        # measured on the spoken form because that is the string that will be read aloud.
        spoken = say(text)
        return Clip(
            text=text,
            spoken=spoken,
            wav=b"",
            duration_ms=len(spoken) / self.CHARS_PER_SECOND * 1000,
            index=index,
            browser_voice=True,
        )

# ───────────────────────────────────────────────────────────── selection
def build_language_model(prefer: str = "auto") -> LanguageModel:
    """The local model when it can run, the script when it cannot.

    `prefer="scripted"` forces the fallback, which is what CI uses: downloading 3GB of weights
    on every push to assert that a WebSocket sends JSON is not a trade worth making.
    """
    if prefer == "scripted":
        return ScriptedLanguageModel()
    model = LocalLanguageModel()
    if prefer == "local" or model.available:
        return model
    log.warning("transformers/torch not installed — the agent will answer from the script")
    return ScriptedLanguageModel()


def build_voice(prefer: str = "auto") -> TextToSpeech:
    if prefer == "browser":
        return SilentTextToSpeech()
    voice = KokoroTextToSpeech()
    if prefer == "kokoro" or voice.available:
        return voice
    log.warning(
        "Kokoro weights not found in %s — the browser will speak instead. "
        "Run `python scripts/fetch-models.py` for the real voice.",
        MODEL_DIR,
    )
    return SilentTextToSpeech()


def engines(llm: LanguageModel, tts: TextToSpeech) -> dict[str, Any]:
    """What is actually loaded, for /api/calls/health and the console's engine badge.

    Reported rather than assumed. A demo that silently degrades to the fallback voice and says
    nothing about it is how a reviewer concludes the whole thing sounds like that.
    """
    return {
        "llm": {
            "name": getattr(llm, "name", type(llm).__name__),
            "model": getattr(llm, "model_name", None),
            "device": getattr(llm, "device", None),
            "ready": getattr(llm, "ready", True),
            "local": isinstance(llm, LocalLanguageModel),
        },
        "tts": {
            "name": getattr(tts, "name", type(tts).__name__),
            "ready": getattr(tts, "ready", True),
            "local": isinstance(tts, KokoroTextToSpeech),
            "voice": getattr(tts, "voice", None),
        },
        "stt": {"name": "browser", "local": False, "note": "Chrome sends audio to Google"},
    }
