"""Turning "what's your work email?" into something worth researching.

THE EMAIL IS THE WHOLE COLD START. Everything Liv knows about a prospect before she says a word
comes from the domain in their address, so this is the one input the product genuinely depends
on — and the one place a typo costs the entire personalised half of the call.

FREE PROVIDERS ARE NOT A REJECTION. A demo that refuses gmail.com is a demo that turns away half
the people who try it, including every founder using a personal address. They are detected and
handled: the call runs, the research step is skipped, and Liv asks which company they are with
instead of pretending she looked them up. Silently researching `gmail.com` would have her open
Google's marketing site and describe it back to them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Deliberately permissive. This is not validating an address for delivery — it is deciding
#: whether there is a domain worth opening a browser at. A stricter pattern rejects real
#: addresses (plus-tags, long TLDs, unicode locals) and the cost of being wrong is refusing a
#: prospect at the front door.
_EMAIL = re.compile(r"^\s*(?P<local>[^@\s]+)@(?P<domain>[^@\s]+\.[a-z]{2,})\s*$", re.IGNORECASE)

#: Addresses that say nothing about where someone works.
FREE_PROVIDERS = frozenset(
    {
        "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com", "hotmail.co.uk",
        "outlook.com", "live.com", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com",
        "proton.me", "protonmail.com", "gmx.com", "gmx.de", "mail.com", "yandex.com",
        "zoho.com", "fastmail.com", "hey.com", "tutanota.com", "pm.me", "duck.com",
    }
)

#: Somebody testing the form. Researching these wastes a browser launch and produces nothing.
_DISPOSABLE_HINTS = ("mailinator", "guerrillamail", "10minutemail", "tempmail", "trashmail")


@dataclass(slots=True)
class Contact:
    """Who is on the call, as far as the front door can tell."""

    email: str
    domain: str
    #: A guess at their first name from the local part. Used only to greet, never asserted —
    #: "Hi Dana" that gets it wrong is recoverable, "I see you're Dana Whitfield" is not.
    first_name: str = ""
    #: True when the domain says nothing about an employer.
    personal: bool = False
    disposable: bool = False

    @property
    def researchable(self) -> bool:
        return bool(self.domain) and not self.personal and not self.disposable

    @property
    def company_guess(self) -> str:
        """A display name for the company, from the domain.

        "corvusdata.io" becomes "Corvusdata", which is wrong often enough that it is only ever
        used as a placeholder until research returns the real name.
        """
        if not self.researchable:
            return ""
        return self.domain.split(".")[0].replace("-", " ").title()


class InvalidEmail(ValueError):
    """The address could not be used. Carries a line to say, not a validation error code."""

    def __init__(self, detail: str, spoken: str):
        super().__init__(detail)
        self.spoken = spoken


def parse_contact(raw: str) -> Contact:
    """Parse an address into everything the call needs from it.

    Raises `InvalidEmail` with something sayable, because this runs while a person is watching
    a form and the failure has to be a sentence rather than a red border.
    """
    match = _EMAIL.match(raw or "")
    if not match:
        raise InvalidEmail(
            f"unparseable address: {raw!r}",
            "That doesn't look like an email address — could you check it?",
        )

    domain = match.group("domain").lower().removeprefix("www.")
    local = match.group("local")

    if any(hint in domain for hint in _DISPOSABLE_HINTS):
        return Contact(
            email=f"{local}@{domain}", domain=domain, first_name=_first_name(local),
            personal=True, disposable=True,
        )

    return Contact(
        email=f"{local}@{domain}",
        domain=domain,
        first_name=_first_name(local),
        personal=domain in FREE_PROVIDERS,
    )


def _first_name(local: str) -> str:
    """A plausible first name from the local part, or nothing.

    `dana.whitfield` and `dana_w` and `dwhitfield` are all common; only the first is worth
    guessing from. Anything that does not look like a name returns empty, and the agent asks
    instead — which is a better first impression than "Hi, Info".
    """
    head = re.split(r"[._\-+0-9]", local)[0]
    if len(head) < 3 or head.lower() in {"info", "hello", "sales", "team", "admin", "contact", "hi"}:
        return ""
    return head.capitalize()
