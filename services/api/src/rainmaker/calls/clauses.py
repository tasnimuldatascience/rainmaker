"""Where to cut a reply so synthesis can start before the sentence is finished.

THIS IS THE SINGLE MOST IMPORTANT NUMBER IN THE VOICE PATH, and it is not obvious why. The
listener waits for the first chunk to be SYNTHESISED, not for it to be spoken, so the size of
that chunk is the length of the silence after they stop talking. Measured on the same 175-
character reply: synthesised in one call it is 3007ms before any sound, cut into clauses it is
407ms. That is the whole trick, and it is worth 2.6 seconds a turn.

WHAT THE COST CURVE ACTUALLY LOOKS LIKE, measured on this machine (Kokoro-82M, sixteen cores,
onnxruntime given eight of them) rather than assumed:

     3 chars ->  386ms      42 chars ->  840ms
    10 chars ->  452ms      92 chars -> 1592ms
    13 chars ->  444ms      fit: ~346ms fixed + ~13.5ms per character

THE FIXED COST DOMINATES AT THIS SIZE, which is the finding that sets `FIRST_CHUNK_CHARS`. A
three-character opening is not meaningfully faster than a thirteen-character one — both pay the
same ~350ms of setup — so there is nothing to gain by cutting tighter than a short phrase, and
plenty of prosody to lose. Twelve characters lands at ~440ms, which is under the half-second
that reads as a natural beat, and "Your engineering" or "Of course," are things people say.

(An earlier version of this comment, inherited from a sibling project, said 25ms per character
plus 150ms of overhead. That was measured on different hardware and is wrong here by more than
a factor of two on the fixed term — which matters, because the fixed term is what decides the
chunk size.)

After the opening, longer is strictly better: fewer calls, less fixed overhead, and noticeably
better intonation because the model sees a whole clause of context. Those chunks are produced
while the previous one is still playing, so their generation time is free.

Both the streaming synthesiser (`providers.KokoroTextToSpeech`) and the turn loop
(`pipeline.CallPipeline`) cut text here. One implementation, because two would drift and the
symptom would be audio that stutters in one code path and not the other.
"""

from __future__ import annotations

import re

#: The opening chunk, in characters. See the module docstring — this is a latency number
#: derived from a measured synthesis curve, not a formatting preference.
FIRST_CHUNK_CHARS = 12

#: Every chunk after the opening. Free to be long: it is generated during playback of the last.
CHUNK_CHARS = 90

#: Below this, a fragment is not worth its own synthesis call. The fixed overhead dominates and
#: a two-word chunk has no prosodic context, so it comes out flat and clipped.
MIN_CLAUSE_CHARS = 16

_CLAUSE = re.compile(r"(?<=[,;:.!?])\s+")
_WORD_BREAK = re.compile(r"\s+")


def first_cut(text: str, target: int = FIRST_CHUNK_CHARS) -> int:
    """Index at which the opening chunk ends, or 0 if it should not be split.

    A clause boundary is preferred because it carries its own intonation — but only when it is
    RIGHT THERE. Allowing one fourteen characters past the target let "For a routine check-up,"
    become the opening chunk, which is 1.5 seconds of audio and gives back the entire saving.
    """
    clause = _CLAUSE.search(text)
    if clause and clause.end() <= target + 4:
        return clause.end()
    word = _WORD_BREAK.search(text, target)
    return word.start() if word else 0


def split_clauses(
    text: str, *, first_chars: int = FIRST_CHUNK_CHARS, chunk_chars: int = CHUNK_CHARS
) -> list[str]:
    """Break a reply into synthesis units, smallest first."""
    text = " ".join(text.split())
    if not text:
        return []

    out: list[str] = []

    # Only worth splitting the opening when enough remains to be worth streaming; a short reply
    # is produced fast enough whole.
    if len(text) > first_chars + MIN_CLAUSE_CHARS:
        cut = first_cut(text, first_chars)
        if cut:
            out.append(text[:cut].strip())
            text = text[cut:].strip()

    buffer = ""
    for part in (p.strip() for p in _CLAUSE.split(text) if p.strip()):
        if not buffer:
            buffer = part
        elif len(buffer) + len(part) + 1 <= chunk_chars:
            buffer = f"{buffer} {part}"
        else:
            out.append(buffer)
            buffer = part
    if buffer:
        # Never leave a scrap on its own at the end: it would be its own synthesis call for two
        # words, and it would sound like it.
        if out and len(buffer) < MIN_CLAUSE_CHARS:
            out[-1] = f"{out[-1]} {buffer}"
        else:
            out.append(buffer)

    return out


def take_speakable(buffer: str, *, opened: bool) -> tuple[str, str]:
    """Split a growing token buffer into (ready to synthesise, keep buffering).

    Called on every token as the model streams. Before the first chunk has been emitted the bar
    is deliberately low — the listener is waiting on silence, so anything past `FIRST_CHUNK_CHARS`
    goes immediately. After that the bar rises to a full clause, because quality now costs
    nothing.

    Returns ("", buffer) when nothing is ready yet.
    """
    text = buffer.lstrip()
    if not opened:
        # AS SOON AS THERE IS A WORD BOUNDARY PAST THE TARGET, and not a character later. This
        # used to wait for `FIRST_CHUNK_CHARS + MIN_CLAUSE_CHARS` — 28 characters — before
        # cutting an opening chunk of 13, so sixteen characters of the model's output were
        # buffered purely to be thrown back into the next chunk. Measured on a live turn that
        # was ~180ms of silence bought for nothing: the listener waits on the FIRST chunk, and
        # every token spent deciding is a token they spend hearing nothing.
        #
        # `first_cut` returning a non-zero index is exactly the condition "a word ends past the
        # target", so there is no separate length check to keep in step with it.
        cut = first_cut(text)
        return (text[:cut].strip(), text[cut:]) if cut else ("", buffer)

    # Past the opening: emit only on a clause boundary, or when the buffer has grown past a
    # comfortable chunk and waiting longer would stall playback.
    boundary = None
    for match in _CLAUSE.finditer(text):
        if match.start() >= MIN_CLAUSE_CHARS:
            boundary = match.end()
            if boundary >= CHUNK_CHARS:
                break
    if boundary is None:
        if len(text) < CHUNK_CHARS * 2:
            return "", buffer
        word = _WORD_BREAK.search(text, CHUNK_CHARS)
        boundary = word.start() if word else len(text)
    return text[:boundary].strip(), text[boundary:]
