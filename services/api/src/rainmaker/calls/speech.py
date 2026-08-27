"""Written text in, spoken text out. The last thing that happens before synthesis.

WHY THIS FILE EXISTS. Nothing sat between the model and the synthesiser. The system prompt asks
for no markdown, no lists and no emoji, and a 1.5B model mostly complies — "mostly" is the whole
problem. One stray `**` is read aloud as "asterisk asterisk". One "e.g." comes out as "ee gee".
One "24/7" becomes "twenty-four slash seven". A listener does not think "the model slipped";
they think the voice is broken, and every one of those is a thing people are pointing at when
they say a synthesised voice sounds like a machine.

A prompt is a request. This is a guarantee, and the two are not substitutes. Everything a
synthesiser mispronounces is fixed here, once, on the way past — so it covers the local voice,
the browser fallback, fixed lines and anything a tenant types, without any of them having to
remember.

THE WRITTEN FORM SURVIVES. A clip carries both: `text` is what goes on screen and `spoken` is
what goes to the voice, because "stripe.com" is right in a caption and "stripe dot com" is right
in an ear. This repository already splits a price exactly that way — see `agents/quoting.py`,
where `total_display` keeps the "$" and `spoken` never has one. Two senses, two representations,
and converging them breaks one of them.

WHAT THIS DELIBERATELY DOES NOT DO. It does not paraphrase, summarise or reorder. Every rule
rewrites a form that cannot be pronounced into one that can, and the meaning is required to
survive unchanged.
"""

from __future__ import annotations

import re

# ── markdown, which is written and never spoken ──────────────────────────────
#: `[the rate card](https://...)` -> `the rate card`. Before the URL rule, which would otherwise
#: consume the target and leave the brackets stranded around it.
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

#: Fenced and inline code. Backticks are punctuation to a reader and noise to a listener.
_FENCE = re.compile(r"```[A-Za-z0-9_+-]*\n?")
_CODE = re.compile(r"`([^`]+)`")

#: Emphasis, in each of the spellings it has. The inner text is kept; only the markers go.
_EMPHASIS = (
    re.compile(r"\*\*\*([^*]+)\*\*\*"),
    re.compile(r"\*\*([^*]+)\*\*"),
    re.compile(r"(?<![A-Za-z0-9*])\*([^*\n]+)\*(?![A-Za-z0-9*])"),
    re.compile(r"~~([^~]+)~~"),
    re.compile(r"(?<![A-Za-z0-9_])__([^_\n]+)__(?![A-Za-z0-9_])"),
    re.compile(r"(?<![A-Za-z0-9_])_([^_\n]+)_(?![A-Za-z0-9_])"),
)

#: A heading marker or a bullet at the start of a line. A numbered item keeps its number —
#: "1. Reserved" is a thing somebody says out loud; "hash hash Pricing" is not.
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BULLET = re.compile(r"^\s{0,4}[-*+•]\s+", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)

#: Anything from the emoji blocks, with its variation selector if it has one. An emoji is
#: either silent or, worse, read aloud as its name.
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff☀-➿\U0001f1e6-\U0001f1ff⬀-⯿]️?"
)

# ── addresses ────────────────────────────────────────────────────────────────
#: An email, said the way a person says one. Before the URL rule, which would otherwise take the
#: host half and leave the name attached by an "@" that nobody pronounces.
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")

#: The suffixes that make a dotted string an address rather than a version number.
_TLD = (
    "com|org|net|io|dev|ai|co|app|cloud|sh|gg|xyz|info|biz|me|tv"
    "|uk|us|de|fr|nl|ca|au|jp|in|eu"
    "|gov|edu|mil|int"
)

#: A URL with nothing worth hearing in it. `https://acme.dev/pricing` read character by character
#: is eleven seconds of a customer wondering what happened.
#:
#: ANCHORED ON A SCHEME OR A REAL SUFFIX, never on "a word with dots in it". Written the loose
#: way it matched `e.g` and produced "e dot g", and matched the `2.5` in `Qwen2.5-1.5B-Instruct`
#: and produced "Qwen2 dot 5" — both worse than the problem being solved.
_URL = re.compile(
    rf"\b(?:https?://|www\.)(?:[A-Za-z0-9-]+\.)*[A-Za-z0-9-]+(?:\.(?:{_TLD}))?(?:/\S*)?"
    rf"|\b(?:[A-Za-z0-9-]+\.)+(?:{_TLD})\b(?:/\S*)?",
    re.IGNORECASE,
)

# ── things a synthesiser says wrong ──────────────────────────────────────────
#: Read literally, each of these comes out as letters or as punctuation.
_SPELLED_OUT: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\be\.\s?g\.,?", re.IGNORECASE), "for example,"),
    # "in other words" rather than "that is": the contraction pass runs after this one and
    # turned "that is," straight back into "that's,". Two rules that are each correct and
    # produce something nobody wrote when run in sequence. The fix is an expansion the second
    # rule has no opinion on, rather than an ordering constraint to remember.
    (re.compile(r"\bi\.\s?e\.,?", re.IGNORECASE), "in other words,"),
    (re.compile(r"\betc\.?(?=\s|$)", re.IGNORECASE), "and so on"),
    (re.compile(r"\bvs\.?(?=\s|$)", re.IGNORECASE), "versus"),
    (re.compile(r"\bapprox\.?(?=\s|$)", re.IGNORECASE), "roughly"),
    (re.compile(r"\bw/\s"), "with "),
    (re.compile(r"\b24/7\b"), "twenty-four seven"),
    (re.compile(r"\s&\s"), " and "),
    (re.compile(r"(\d)\s?%"), r"\1 percent"),
    (re.compile(r"(?<=\d)\s?x\b"), " times"),
    (re.compile(r"\s\+\s"), " plus "),
    (re.compile(r"\s=\s"), " equals "),
    (re.compile(r"\s(?:->|→)\s"), " to "),
)

#: 4,800 -> 4800, so it is read as one number rather than two with a pause between them. Only
#: between digits, so it cannot touch the comma in "forty seats, and".
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")

# ── spoken register ──────────────────────────────────────────────────────────
#: WHY CONTRACT AT ALL. "I am not going to pretend" and "I'm not going to pretend" mean the same
#: thing and belong to different registers: nobody says the first one out loud. Written English
#: is what a language model defaults to, and hearing it is one of the reliable tells.
#:
#: Kept to pairs that are unambiguous. No "it's/its" hazard, nothing that shifts emphasis, and
#: nothing whose uncontracted form is the natural one at the end of a clause.
_CONTRACTIONS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"\b{written}\b", re.IGNORECASE), spoken)
    for written, spoken in (
        ("I am", "I'm"),
        ("you are", "you're"),
        ("we are", "we're"),
        ("they are", "they're"),
        ("it is", "it's"),
        ("that is", "that's"),
        ("there is", "there's"),
        ("what is", "what's"),
        ("do not", "don't"),
        ("does not", "doesn't"),
        ("did not", "didn't"),
        ("cannot", "can't"),
        ("can not", "can't"),
        ("will not", "won't"),
        ("would not", "wouldn't"),
        ("should not", "shouldn't"),
        ("is not", "isn't"),
        ("are not", "aren't"),
        ("have not", "haven't"),
        ("I will", "I'll"),
        ("you will", "you'll"),
        ("we will", "we'll"),
        ("I have", "I've"),
        ("you have", "you've"),
        ("we have", "we've"),
    )
)

# ── pauses ───────────────────────────────────────────────────────────────────
#: Three dots is a written gesture. A comma is the pause it stands in for, and it is one the
#: synthesiser knows how to perform.
_ELLIPSIS = re.compile(r"\s*(?:\.{3,}|…)\s*")

#: "!!" and "?!" read as emphasis on a page and as nothing through a synthesiser, which takes
#: the first mark and drops the rest. Reduced so one unambiguous character picks the contour.
_PILEUP = re.compile(r"([.!?])[.!?]+")

#: An em dash with no space around it gets glued to its neighbours by a tokeniser. Given room it
#: becomes the beat it was meant to be.
_DASH = re.compile(r"\s*[—–]\s*")

_SPACE = re.compile(r"[ \t ]+")
_ORPHAN_PUNCT = re.compile(r"\s+([,.;:!?])")


def _dotted(host: str) -> str:
    return host.replace(".", " dot ")


def _readable_url(match: re.Match[str]) -> str:
    """A domain a person can hear, and none of the path.

    `stripe.com/pricing` becomes "stripe dot com": the host identifies somebody, and the path is
    where a URL stops being pronounceable.
    """
    whole = match.group(0)
    whole = re.sub(r"^https?://", "", whole, flags=re.IGNORECASE)
    whole = re.sub(r"^www\.", "", whole, flags=re.IGNORECASE)
    return _dotted(whole.split("/", 1)[0].rstrip(".,;:!?"))


def _readable_email(match: re.Match[str]) -> str:
    """`dana@stripe.com` -> `dana at stripe dot com`."""
    return f"{_dotted(match.group(1))} at {_dotted(match.group(2))}"


def _cased(replacement: str):
    """Keep a contraction's capitalisation, so a sentence-initial "Do not" is not lowercased."""

    def sub(match: re.Match[str]) -> str:
        return replacement[:1].upper() + replacement[1:] if match.group(0)[:1].isupper() else replacement

    return sub


def say(text: str) -> str:
    """The spoken form of `text`. Idempotent, and safe on text that is already plain.

    Order is not arbitrary. Links resolve before URLs, or a link target is eaten and its brackets
    left behind. Markdown clears before the pronunciation rules, so `**e.g.**` is reached at all.
    Addresses resolve before abbreviations, because both are interested in full stops. Thousands
    separators go before punctuation is tidied, because the separator is a comma and the tidy-up
    is about commas.
    """
    if not text or not text.strip():
        return ""

    out = _LINK.sub(r"\1", text)
    out = _FENCE.sub(" ", out)
    out = _CODE.sub(r"\1", out)
    for pattern in _EMPHASIS:
        out = pattern.sub(r"\1", out)
    out = _HEADING.sub("", out)
    out = _BULLET.sub("", out)
    out = _BLOCKQUOTE.sub("", out)

    out = _EMOJI.sub(" ", out)
    out = _EMAIL.sub(_readable_email, out)
    out = _URL.sub(_readable_url, out)

    for pattern, replacement in _SPELLED_OUT:
        out = pattern.sub(replacement, out)
    out = _THOUSANDS.sub("", out)

    for pattern, replacement in _CONTRACTIONS:
        out = pattern.sub(_cased(replacement), out)

    out = _ELLIPSIS.sub(", ", out)
    out = _PILEUP.sub(r"\1", out)
    out = _DASH.sub(" — ", out)

    out = out.replace("\n", " ")
    out = _SPACE.sub(" ", out)
    out = _ORPHAN_PUNCT.sub(r"\1", out)
    return out.strip()
