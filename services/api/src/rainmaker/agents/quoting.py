"""Turning what the agent learned into a number it is allowed to say.

THE MODEL NEVER PRODUCES A FIGURE. Not the seats, not the rate, not the discount, not the total.
Every number on a quote is arithmetic over the tenant's published pricing and a seat count the
conversation established, computed here and handed to the model already formed. This is the same
rule prices have always had in this codebase, tightened, because the next step after a quote is
a payment: an agent that invents a booking wastes a slot, and an agent that invents an amount
takes somebody's money.

WHERE THE SEATS COME FROM, in order of how much they should be trusted:

    said      the buyer told us. "We've got forty reps" is not small talk, it is the seat count.
    research  the enrichment's company-size band, which is a guess with a source.
    default   the tier's minimum. Used when nothing else is known, and flagged as an assumption
              so the agent says "for a team of ten" rather than asserting it.

A quote built on a guess is fine as long as the agent SAYS it is a guess. A quote built on a
guess and presented as a fact is how somebody agrees to the wrong number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .spec import AgentSpec, Tier

#: Company-size bands the research agent emits, and a seat count to assume for each. Deliberately
#: conservative: quoting low and being corrected upward is a conversation, quoting high and being
#: corrected downward is an objection.
SIZE_SEATS: dict[str, int] = {
    "solo": 1,
    "micro": 3,
    "small": 8,
    "mid": 25,
    "large": 80,
    "enterprise": 200,
}

#: Ceiling on what a conversation can claim. Somebody saying "we have nine billion reps" is
#: joking or testing, and a quote with nine billion on it is not a quote.
#:
#: RAISED WHEN THE UNIT STOPPED BEING A SEAT. Five thousand is a sane bound on headcount and an
#: absurd one on GPU-hours or kilos, and a cap that silently discards a real quantity is worse
#: than no cap: the quote falls back to a guess without saying it did. Genuinely large numbers
#: are handled further down the line, where the payment ceiling refuses to charge them and
#: offers a person instead.
MAX_SEATS = 1_000_000


#: Numbers as words, because the voice reads what it is given.
#:
#: THE SYNTHESISER SAYS "DOLLAR FOUR THOUSAND EIGHT HUNDRED". Handed "$4,800", espeak
#: phonemises the symbol as the word "dollar" and puts it FIRST, then reads the digits — and
#: "$2.40" comes out as "dollar two. forty", with a full stop in the middle of the price. That
#: is the single most obviously synthetic thing on the call, and it happens at the one moment
#: the whole product exists for.
#:
#: The call rules have always told the MODEL to write numbers the way they are spoken. The
#: platform's own computed sentence was breaking its own rule, which is the sort of thing that
#: only shows up when you phonemise what you are actually sending.
_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
    "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")

#: What a unit of each currency is called, singular and plural.
_CURRENCY_WORDS = {
    "usd": ("dollar", "dollars", "cent", "cents"),
    "gbp": ("pound", "pounds", "penny", "pence"),
    "eur": ("euro", "euros", "cent", "cents"),
}


def _hundreds(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f" {_ONES[ones]}" if ones else "")
    hundreds, rest = divmod(n, 100)
    said = f"{_ONES[hundreds]} hundred"
    return f"{said} and {_hundreds(rest)}" if rest else said


def number_words(n: int) -> str:
    """`4800` -> "four thousand eight hundred". Spoken form, not written form."""
    if n < 0:
        return f"minus {number_words(-n)}"
    if n < 1000:
        return _hundreds(n)
    for size, name in ((1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand")):
        if n >= size:
            count, rest = divmod(n, size)
            said = f"{number_words(count)} {name}"
            return f"{said} {number_words(rest)}" if rest else said
    return str(n)


def money_words(amount: int, currency: str) -> str:
    """Minor units to something a voice can read: `480000, "usd"` -> "four thousand eight
    hundred dollars"."""
    major_word, major_plural, minor_word, minor_plural = _CURRENCY_WORDS.get(
        currency.lower(), ("unit", "units", "part", "parts")
    )
    major, minor = divmod(abs(amount), 100)

    said = f"{number_words(major)} {major_word if major == 1 else major_plural}" if major else ""
    if minor:
        # "two dollars forty", the way a person says a price — not "two dollars and forty cents",
        # which is how a form reads it back to you.
        small = (
            f"{number_words(minor)}"
            if said
            else f"{number_words(minor)} {minor_word if minor == 1 else minor_plural}"
        )
        said = f"{said} {small}" if said else small
    return said or f"zero {major_plural}"


@dataclass(frozen=True, slots=True)
class Quote:
    """A number with somebody's name on it. Every field is computed, none is generated."""

    tier: str
    seats: int
    #: What one of them is called, in the tenant's words: "seat", "GPU-hour", "charger".
    unit_name: str
    unit_plural: str
    unit_amount: int
    currency: str
    period: str
    term: str
    subtotal: int
    discount: int
    total: int
    #: Where the seat count came from: "said", "research" or "assumed".
    seats_from: str
    company: str = ""
    #: For a RATE unit, what the buyer actually said: how many things, for how long.
    #:
    #: A buyer of GPU-hours does not ask for 23,360 of them, they ask for "32 H100s for a
    #: month". The arithmetic needs the product; the sentence needs both halves, or she answers
    #: a question nobody asked. Empty for a unit that is not a rate, where the count IS the ask.
    quantity: int = 0
    duration_label: str = ""
    #: The buyer's word for the thing, not the price list's: "GPUs", not "GPU-hours".
    quantity_noun: str = ""

    @property
    def assumed(self) -> bool:
        return self.seats_from == "assumed"

    def money(self, amount: int) -> str:
        """Minor units to something a person reads. Never rounded — a rounded quote is a
        different quote."""
        symbol = {"usd": "$", "gbp": "£", "eur": "€"}.get(self.currency.lower(), "")
        whole, minor = divmod(amount, 100)
        body = f"{whole:,}" if minor == 0 else f"{whole:,}.{minor:02d}"
        return f"{symbol}{body}" if symbol else f"{body} {self.currency.upper()}"

    @property
    def units(self) -> str:
        """The quantity in the tenant's words: "40 GPU-hours", "one seat"."""
        plural = self.unit_plural or f"{self.unit_name}s"
        return f"one {self.unit_name}" if self.seats == 1 else f"{self.seats:,} {plural}"

    @property
    def asked(self) -> str:
        """The quantity in the BUYER's words: "64 GPUs for two weeks", "40 seats".

        `units` is the same figure converted into the price list's unit, which is the right
        thing to charge in and the wrong thing to read back. "Sized from the 21,504 GPU-hours
        you mentioned" was said to somebody who mentioned sixty-four GPUs, and a buyer cannot
        check a number they never said. The spoken sentence already did this; the panel did not.
        """
        if self.quantity and self.duration_label:
            noun = f" {self.quantity_noun}" if self.quantity_noun else ""
            return f"{self.quantity:,}{noun} for {self.duration_label}"
        return self.units

    def spoken(self) -> str:
        """The quote as the agent says it out loud.

        Written here rather than by the model for the same reason the calendar writes its own
        times: this sentence is a commitment, and the one place a number turns into one is not
        where you want a 1.5B model paraphrasing.
        """
        # WRITTEN FOR THE EAR, NOT FOR THE SCREEN. The screen version of this sentence had an
        # em dash in the middle and said "per month" twice; read aloud that is a stumble and a
        # repetition. A person says the rate, then what it comes to.
        each = money_words(self.unit_amount, self.currency)
        total = money_words(self.total, self.currency)
        opening = "For about " if self.assumed else "For "
        # SAY IT BACK THE WAY THEY ASKED IT. "For 23,360 GPU-hours" is arithmetically the same
        # question and a different sentence: the buyer asked for 32 cards for a month and has
        # no way to check a number they never said. Both halves, then the total.
        if self.quantity and self.duration_label:
            noun = f" {self.quantity_noun}" if self.quantity_noun else ""
            asked = f"{self.quantity:,}{noun} for {self.duration_label}"
            line = (
                f"{opening}{asked} — that's {self.units} on {self.tier}, "
                f"at {each} per {self.unit_name}"
            )
        else:
            line = (
                f"{opening}{self.units} on {self.tier}, "
                f"that's {each} per {self.unit_name}"
            )
        if self.discount:
            line += f", less {money_words(self.discount, self.currency)} for paying annually"
        return f"{line}, which comes to {total} a {self.period}."

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "seats": self.seats,
            "units": self.units,
            "asked": self.asked,
            "unit_name": self.unit_name,
            "unit_plural": self.unit_plural or f"{self.unit_name}s",
            "seats_from": self.seats_from,
            "assumed": self.assumed,
            "company": self.company,
            "currency": self.currency,
            "period": self.period,
            "term": self.term,
            "unit_amount": self.unit_amount,
            "unit_display": self.money(self.unit_amount),
            "subtotal": self.subtotal,
            "subtotal_display": self.money(self.subtotal),
            "discount": self.discount,
            "discount_display": self.money(self.discount) if self.discount else "",
            "total": self.total,
            "total_display": self.money(self.total),
            "quantity": self.quantity,
            "quantity_noun": self.quantity_noun,
            "duration_label": self.duration_label,
            "spoken": self.spoken(),
        }


#: How many of the rate unit are in a stretch of calendar time.
#:
#: A MONTH IS 730 HOURS, NOT THIRTY. This table exists because a unit like "GPU-hour" is a RATE:
#: the buyer states a quantity and a duration in the same breath — "32 H100s for a month" — and
#: the number that goes into the arithmetic is the product of the two. Without it the duration
#: was simply dropped, and "32 H100s for a month" was quoted as if it were a handful of hours.
RATE_UNIT_HOURS: dict[str, float] = {
    "hour": 1, "day": 24, "week": 168, "month": 730, "year": 8760,
}

#: "for a month", "for three months", "over 2 weeks", "for a couple of weeks".
_DURATION = re.compile(
    r"\b(?:for|over|across)?\s*(?:a|an|one|1|(?P<n>\d{1,4}|two|three|four|six|twelve)|couple\s+of)?"
    r"\s*(?P<period>hour|day|week|month|year)s?\b",
    re.IGNORECASE,
)

_DURATION_WORDS = {"two": 2, "three": 3, "four": 4, "six": 6, "twelve": 12}


def rate_period(unit_name: str) -> str:
    """The time word a rate unit is a rate over, or "" if it is not a rate.

    "GPU-hour" -> "hour". "seat" -> "". A seat is a thing you have; a GPU-hour is a thing you
    consume, and only the second one multiplies by a duration.
    """
    tail = unit_name.replace("-", " ").replace("/", " ").split()
    return tail[-1].lower() if tail and tail[-1].lower() in RATE_UNIT_HOURS else ""


def duration_from_conversation(text: str, period: str) -> tuple[float, str] | None:
    """How long they said they needed it for, as a multiple of `period`, plus a label.

    Returns `None` when no duration was stated — which is not the same as one, and must not be
    treated as one: quoting a month when the buyer never said "month" is inventing the larger
    half of the number.
    """
    if period not in RATE_UNIT_HOURS:
        return None
    for match in _DURATION.finditer(text):
        said = match.group("period").lower()
        if said == period and not match.group("n"):
            # "for an hour" when the unit is the hour: a duration of one, not a multiplier.
            continue
        raw = (match.group("n") or "").lower()
        count = _DURATION_WORDS.get(raw, int(raw) if raw.isdigit() else 1)
        hours = RATE_UNIT_HOURS[said] * count / RATE_UNIT_HOURS[period]
        if hours <= 1:
            continue
        label = f"{count} {said}s" if count > 1 else f"a {said}"
        return hours, label
    return None


# TENS AND UNITS SEPARATELY, BECAUSE "THIRTY TWO" IS TWO WORDS. A flat table of number words
# matched the longest single one it could find, so a buyer saying "we need about thirty two
# H100s" out loud was quoted for THIRTY — a 6% error nobody would catch by ear, on a figure of
# eighty-four thousand dollars. Digits never had this problem, which is why it survived: it only
# appears when somebody speaks the number instead of typing it.
#
# At module scope because two matchers need them: the one that reads the quantity, and the one
# that decides whether the buyer already stated it in the unit being charged for.
_HEARD_ONES = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9,
}
_HEARD_TEENS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_HEARD_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
}
_HEARD_WORDS = {**_HEARD_ONES, **_HEARD_TEENS, **_HEARD_TENS, "hundred": 100}


def _number_pattern() -> str:
    """Digits or spoken number words, longest alternative first."""
    compound = "|".join(f"{ten}[- ]{one}" for ten in _HEARD_TENS for one in _HEARD_ONES)
    singles = "|".join(sorted(_HEARD_WORDS, key=len, reverse=True))
    return r"(?:\d{1,3}(?:,\d{3})+|\d{1,7}|" + compound + "|" + singles + r")"


def quantity_already_in_rate_units(noun: str, period: str) -> bool:
    """True when the buyer's number is ALREADY the thing being charged for.

    THE NOUN NEXT TO THE NUMBER IS THE WHOLE SIGNAL. "32 H100s for a month" counts cards, and a
    month has to be multiplied in before it is a price. "2,000 GPU-hours a month" counts the
    priced unit itself, and multiplying the month in again quoted two point four million dollars
    for forty-eight hundred dollars of compute.

    Takes the noun the matcher actually used rather than re-reading the sentence, because
    "we run training 24 hours a day, we need 32 H100s for a month" contains both nouns and only
    the matcher knows which number won. Guessing from the raw text divides that quote by seven
    hundred, which is the worse of the two failures.
    """
    if not period or not noun:
        return False
    # "GPU-hours" and "hours" both end in the rate's period word; "H100s" and "GPUs" do not.
    tail = noun.lower().replace("-", " ").split()
    return bool(tail) and tail[-1].rstrip("s") == period.lower().rstrip("s")


def _is_time_noun(noun: str | None) -> bool:
    """Is this noun a unit of time rather than a thing being bought?"""
    if not noun:
        return False
    return noun.lower().rstrip("s") in RATE_UNIT_HOURS


def seats_from_conversation(text: str, units: tuple[str, ...] = ()) -> int | None:
    """The quantity alone, for callers that do not care what it was called."""
    found = stated_quantity(text, units)
    return found[0] if found else None


def stated_quantity(text: str, units: tuple[str, ...] = ()) -> tuple[int, str] | None:
    """A quantity the buyer stated and the noun they attached it to, or nothing.

    THE NOUN IS HALF THE ANSWER. "2,000 GPU-hours" and "2,000 GPUs" are the same number and a
    five-hundred-fold difference in price, and only the noun says which one was meant.

    Deliberately narrow. It fires on "we have forty reps" and "about 25 people on the team", and
    stays quiet on "we closed forty deals last quarter" — a number near the wrong noun is worse
    than no number, because it silently becomes the quote.

    `units` carries the tenant's own words for what they sell, because the nouns below are the
    words a software company uses about headcount and a buyer of GPU-hours says "we need about
    two thousand hours a month". A tenant whose unit is not in this list gets a guess when the
    buyer told them the answer.
    """
    import re

    ones, tens, words = _HEARD_ONES, _HEARD_TENS, _HEARD_WORDS
    # LONGEST FIRST, BECAUSE ALTERNATION TAKES THE FIRST MATCH THAT FITS. "GPU" and "GPU-hour"
    # are both the tenant's words, and a hyphen is a word boundary — so "2,000 GPU-hours"
    # matched the noun "GPU", which is a card rather than a rate, and the quote was multiplied
    # by a month it had already been given.
    tenant_words = "|".join(
        re.escape(word) + "s?"
        for word in sorted(
            dict.fromkeys(u.lower() for u in units if u), key=len, reverse=True
        )
    )
    # THE UNIT IS NOT THE NOUN. `units` is what the price list calls it — "GPU-hour" — and no
    # buyer says that; they say "32 H100s". A tenant whose product has a name of its own puts
    # those words in `AgentSpec.unit_nouns`, and they are matched here alongside the built-in
    # headcount words. Without this, "what does it cost for 32 H100s for a month" matched
    # nothing and the quote fell back to a guessed size band.
    nouns = r"(?P<noun>seats?|licen[cs]es?|users?|reps?|people|staff|salespeople|agents?|of us"
    nouns += (f"|{tenant_words})" if tenant_words else ")")
    # Longest first inside each group, and compounds before their own tens word, so "thirty two"
    # is never truncated to "thirty" by an earlier alternative winning.
    compound = "|".join(
        f"{ten}[- ]{one}" for ten in tens for one in ones
    )
    singles = "|".join(sorted(words, key=len, reverse=True))
    number = (
        r"(?P<n>\d{1,3}(?:,\d{3})+|\d{1,7}|" + compound + "|" + singles + r")"
    )
    # "for" earns its place: "how much for 40 people" is one of the two commonest ways the
    # question is asked, and without it the quote silently falls back to the research band's
    # guess on a sentence that stated the number out loud.
    lead = r"\b(?:we(?:'ve| have| are)?|there are|about|roughly|around|just|for)\b[^.?!]{0,24}?"

    # THREE SHAPES, BECAUSE PEOPLE SAY IT THREE WAYS. The first has the noun after the number;
    # the other two do not, and "our team of 25" is one of the commonest answers to "how big is
    # the team" — a pattern that only handled the first shape silently fell back to a guess on
    # a sentence that had told it the answer.
    patterns = (
        # "we have forty reps", "there are about 8 of us"
        re.compile(rf"{lead}{number}\s+(?:\w+\s+){{0,2}}{nouns}\b", re.IGNORECASE),
        # "our team of 25", "a team of forty"
        re.compile(rf"\bteam of\s+{number}\b", re.IGNORECASE),
        # "a 25-person team", "40 seat plan"
        re.compile(rf"\b{number}[- ](?:person|seat|user|head)\b", re.IGNORECASE),
    )
    # THE PRODUCT OUTRANKS THE CLOCK. "we run training 24 hours a day, we need 32 H100s for a
    # month" states two numbers, and the first one is not the order — it is context. Taking the
    # earliest match quoted twenty-four of something on a sentence that said thirty-two.
    match = None
    for pattern in patterns:
        found = list(pattern.finditer(text))
        if not found:
            continue
        match = next(
            (m for m in found if not _is_time_noun(m.groupdict().get("noun"))),
            found[0],
        )
        break
    if not match:
        return None
    noun = (match.groupdict().get("noun") or "").lower().strip()
    raw = match.group("n").lower().replace("-", " ").strip()
    if raw in words:
        seats: int | None = words[raw]
    elif " " in raw:
        parts = raw.split()
        seats = sum(words.get(part, 0) for part in parts) if all(p in words for p in parts) else None
    else:
        try:
            seats = int(raw.replace(",", ""))
        except ValueError:
            seats = None
    if seats is None or not 1 <= seats <= MAX_SEATS:
        return None
    return seats, noun


def pick_tier(spec: AgentSpec, seats: int) -> Tier | None:
    """The tier that fits this many seats.

    Falls to the last tier that the seat count reaches, so a large team lands on the enterprise
    row rather than being quoted the starter rate multiplied by two hundred.
    """
    priced = [tier for tier in spec.pricing if tier.unit_amount > 0]
    if not priced:
        return None
    fitting = [tier for tier in priced if seats >= tier.min_seats]
    return max(fitting, key=lambda tier: tier.min_seats) if fitting else priced[0]


def unit_words(spec: AgentSpec) -> tuple[str, ...]:
    """Every word this tenant uses for what they sell, for the seat detector to listen for."""
    # What the BUYER calls it comes first: "H100", "node", "card". The price list's own word is
    # often one nobody says out loud. See `AgentSpec.unit_nouns`.
    found: list[str] = [noun for noun in spec.unit_nouns if noun]
    for tier in spec.pricing:
        found.extend([tier.unit_name, tier.unit_plural or f"{tier.unit_name}s"])
        # "GPU-hour" is also said as "GPU hour" and as "hour". The last word on its own is the
        # one people actually say once the context is established.
        tail = tier.unit_name.replace("-", " ").split()
        if len(tail) > 1:
            found.extend([" ".join(tail), tail[-1]])
    return tuple(dict.fromkeys(word for word in found if word))


def build_quote(
    spec: AgentSpec,
    *,
    said_seats: int | None = None,
    size_band: str = "",
    company: str = "",
    annual: bool = False,
    said_duration: tuple[float, str] | None = None,
) -> Quote | None:
    """A quote, or nothing if this agent has no quotable pricing.

    Returning `None` rather than a placeholder is deliberate: an agent whose owner has entered
    tiers with no amounts can talk about price without putting a number on the screen, and a
    zero would be a number.
    """
    seats, seats_from = _seats(said_seats, size_band)
    tier = pick_tier(spec, seats)
    if tier is None:
        return None

    seats = max(seats, tier.min_seats)

    # A RATE UNIT MULTIPLIES BY TIME. "32 H100s for a month" is 32 x 730 GPU-hours, and dropping
    # the second half quoted thirty six dollars for eighty-four thousand dollars of compute.
    # Only applied when the buyer actually stated a duration: assuming one would be inventing
    # the larger half of the number.
    quantity, duration_label = 0, ""
    if said_duration and rate_period(tier.unit_name):
        multiplier, duration_label = said_duration
        quantity = seats
        seats = max(1, min(MAX_SEATS, round(seats * multiplier)))

    period = spec.pricing_period
    subtotal = tier.unit_amount * seats

    # Annual discount applies to the yearly figure, so it is expressed against the period the
    # buyer is being quoted in rather than silently changing what "per month" means.
    discount = (subtotal * spec.annual_discount_pct) // 100 if annual else 0

    return Quote(
        tier=tier.name,
        seats=seats,
        unit_name=tier.unit_name,
        unit_plural=tier.unit_plural,
        unit_amount=tier.unit_amount,
        currency=spec.currency,
        period=period,
        term="annual" if annual else period,
        subtotal=subtotal,
        discount=discount,
        total=subtotal - discount,
        seats_from=seats_from,
        company=company,
        quantity=quantity,
        duration_label=duration_label,
        quantity_noun=(
            f"{spec.unit_nouns[0]}s" if quantity and spec.unit_nouns else ""
        ),
    )


def _seats(said: int | None, size_band: str) -> tuple[int, str]:
    if said is not None and 1 <= said <= MAX_SEATS:
        return said, "said"
    band = (size_band or "").strip().lower().replace("companysize.", "")
    if band in SIZE_SEATS:
        return SIZE_SEATS[band], "research"
    return 10, "assumed"
