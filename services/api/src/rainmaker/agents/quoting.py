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

    def spoken(self) -> str:
        """The quote as the agent says it out loud.

        Written here rather than by the model for the same reason the calendar writes its own
        times: this sentence is a commitment, and the one place a number turns into one is not
        where you want a 1.5B model paraphrasing.
        """
        each = self.money(self.unit_amount)
        total = self.money(self.total)
        opening = "For about " if self.assumed else "For "
        line = (
            f"{opening}{self.units} on {self.tier}, "
            f"that's {each} per {self.unit_name} per {self.period}"
        )
        if self.discount:
            line += f", less {self.money(self.discount)} for paying annually"
        return f"{line} — {total} per {self.period}."

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "seats": self.seats,
            "units": self.units,
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
            "spoken": self.spoken(),
        }


def seats_from_conversation(text: str, units: tuple[str, ...] = ()) -> int | None:
    """A quantity the buyer stated, or nothing.

    Deliberately narrow. It fires on "we have forty reps" and "about 25 people on the team", and
    stays quiet on "we closed forty deals last quarter" — a number near the wrong noun is worse
    than no number, because it silently becomes the quote.

    `units` carries the tenant's own words for what they sell, because the nouns below are the
    words a software company uses about headcount and a buyer of GPU-hours says "we need about
    two thousand hours a month". A tenant whose unit is not in this list gets a guess when the
    buyer told them the answer.
    """
    import re

    words = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
        "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
        "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "eighty": 80,
        "hundred": 100,
    }
    tenant_words = "|".join(
        re.escape(word) + "s?" for word in dict.fromkeys(u.lower() for u in units if u)
    )
    nouns = r"(?:seats?|licen[cs]es?|users?|reps?|people|staff|salespeople|agents?|of us"
    nouns += (f"|{tenant_words})" if tenant_words else ")")
    number = r"(?P<n>\d{1,3}(?:,\d{3})+|\d{1,7}|" + "|".join(words) + r")"
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
    match = next((found for found in (p.search(text) for p in patterns) if found), None)
    if not match:
        return None
    raw = match.group("n").lower()
    seats = words.get(raw) if raw in words else int(raw.replace(",", ""))
    if seats is None or not 1 <= seats <= MAX_SEATS:
        return None
    return seats


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
    found: list[str] = []
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
    )


def _seats(said: int | None, size_band: str) -> tuple[int, str]:
    if said is not None and 1 <= said <= MAX_SEATS:
        return said, "said"
    band = (size_band or "").strip().lower().replace("companysize.", "")
    if band in SIZE_SEATS:
        return SIZE_SEATS[band], "research"
    return 10, "assumed"
