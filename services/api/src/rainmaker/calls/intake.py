"""Turning "who are you?" into something worth researching.

THREE FIELDS AT THE FRONT DOOR: name, work email, company. It used to be one — an address — and
one field is a nicer form, but it is a worse call. The name is how she greets you, the company is
what she talks about, and the address is what she reads before she says a word. Asking a buyer
for all three takes eight seconds and is exactly what a human rep opens with.

THE ADDRESS HAS TO BE A WORK ONE. It is the entire cold start — the domain is where the research
agent points a browser, and gmail.com is Google's marketing site — and it is also the qualifying
question. This product is sold to businesses; somebody who will not give a business address is
not a buyer this agent should spend a call on, and a rep would have reached the same conclusion
a minute later. The refusal is a sentence explaining why, so the fix is obvious rather than
mysterious.

FAILURES ARE SPOKEN, NOT FLAGGED. Every rejection here happens while a person is watching a form
with an agent's face next to it, so each one carries a line she can say and the field to put it
under. A red border is what the rest of the internet does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Deliberately permissive. This is not validating an address for delivery — it is deciding
#: whether there is a domain worth opening a browser at. A stricter pattern rejects real
#: addresses (plus-tags, long TLDs, unicode locals) and the cost of being wrong is refusing a
#: prospect at the front door.
_EMAIL = re.compile(r"^\s*(?P<local>[^@\s]+)@(?P<domain>[^@\s]+\.[a-z]{2,})\s*$", re.IGNORECASE)

#: A domain hiding inside whatever they typed for their company: "corvusdata.io",
#: "https://www.corvusdata.io/about", "Corvus Data (corvusdata.io)".
_DOMAIN_IN_TEXT = re.compile(
    r"(?:https?://)?(?:www\.)?(?P<domain>[a-z0-9][a-z0-9-]{1,62}(?:\.[a-z0-9-]{2,63})+)",
    re.IGNORECASE,
)

#: Suffixes that make something a URL rather than a company called "Acme.Inc".
_REAL_TLDS = frozenset(
    """com co uk io net org ai app dev ca de fr es it nl se no dk fi pl br mx
    in jp cn ru ch at be ie nz za sg hk kr tw us biz info me tv cc xyz tech health law
    finance agency studio design cloud software systems group works company""".split()
)

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

#: Local parts that are a department rather than a person.
_ROLE_LOCALS = frozenset({"info", "hello", "sales", "team", "admin", "contact", "hi", "support"})


@dataclass(slots=True)
class Contact:
    """Who is on the call, as far as the front door can tell."""

    email: str
    domain: str
    #: Their first name. Taken from what they typed when they typed one, and only guessed from
    #: the local part otherwise — "Hi Dana" that gets it wrong is recoverable, but not free.
    first_name: str = ""
    full_name: str = ""
    #: What they typed in the company field, tidied. Never guessed.
    company: str = ""
    #: True when the ADDRESS says nothing about an employer, whatever `domain` ended up as.
    personal: bool = False
    disposable: bool = False
    #: Set when `domain` came from the company field rather than the address, so the agent can
    #: say "I had a look at corvusdata.io" instead of implying it knew.
    domain_from_company: bool = False

    @property
    def researchable(self) -> bool:
        return bool(self.domain) and not self.disposable and not self.personal

    @property
    def company_guess(self) -> str:
        """A display name for the company.

        What they typed, when they typed something. Otherwise from the domain, where
        "corvusdata.io" becomes "Corvusdata" — wrong often enough that it is only ever a
        placeholder until research returns the real name.
        """
        if self.company:
            return self.company
        if not self.researchable:
            # "someone@gmail.com" would otherwise produce a prospect called Gmail.
            return ""
        return self.domain.split(".")[0].replace("-", " ").title()


class IntakeError(ValueError):
    """A field could not be used. Carries a line to say and the field to say it under."""

    def __init__(self, detail: str, spoken: str, field: str = "email"):
        super().__init__(detail)
        self.spoken = spoken
        self.field = field


#: The old name, kept because the wire and the tests both know it and neither is worth churning.
InvalidEmail = IntakeError


def parse_intake(
    name: str,
    email: str,
    organisation: str = "",
    *,
    ask_company: bool = True,
    require_work_email: bool = True,
) -> Contact:
    """The front door, asking what this particular agent asks for.

    THE RULES ARE THE AGENT'S, NOT THIS MODULE'S. A B2B agent needs a work address because the
    domain is the cold start and the qualifying question. A dental practice does not, and
    demanding one from a patient with toothache is the version of this that looks like a
    software company wrote a form for itself. See `agents.spec.Intake`.

    Raises `IntakeError` with something sayable. The order matters: name first, because being
    told your email is wrong before anyone has said hello is the worst version of this form.
    """
    person = " ".join((name or "").split())
    if len(person) < 2:
        raise IntakeError("no name given", "And who am I speaking to?", field="name")

    contact = parse_contact(email)
    contact.full_name = person
    contact.first_name = person.split()[0].capitalize()

    company, stated_domain = _company_and_domain(organisation)
    if ask_company and not company:
        raise IntakeError("no company given", "Which company are you with?", field="company")
    contact.company = company

    if contact.disposable:
        raise IntakeError(
            f"disposable domain: {contact.domain}",
            "That address won't reach you — could I get the one you actually use?",
            field="email",
        )

    if contact.personal and require_work_email:
        raise IntakeError(
            f"personal address: {contact.domain}",
            "I'll need your work email — I read up on your company before we talk, and a "
            "personal address doesn't tell me where you are.",
            field="email",
        )

    # A WORK ADDRESS THEY TYPED A WEBSITE ALONGSIDE. Rare but real: people at holding companies,
    # agencies, and anyone whose mail domain is not their marketing domain. What they typed wins,
    # because they know which site describes their business and the mail server does not.
    if stated_domain and stated_domain != contact.domain:
        contact.domain = stated_domain
        contact.domain_from_company = True

    return contact


def parse_contact(raw: str) -> Contact:
    """Parse an address on its own, for paths that have nothing else.

    Used by the plain conversation and by anything holding an address without a form around it.
    Sets `personal` and `disposable`; deciding what to do about them is `parse_intake`'s job.
    """
    match = _EMAIL.match(raw or "")
    if not match:
        raise IntakeError(
            f"unparseable address: {raw!r}",
            "That doesn't look like an email address — could you check it?",
            field="email",
        )

    domain = match.group("domain").lower().removeprefix("www.")
    local = match.group("local")
    disposable = any(hint in domain for hint in _DISPOSABLE_HINTS)

    return Contact(
        email=f"{local}@{domain}",
        domain=domain,
        first_name=_first_name(local),
        personal=disposable or domain in FREE_PROVIDERS,
        disposable=disposable,
    )


def _company_and_domain(raw: str) -> tuple[str, str]:
    """Their company as a name, and a domain if they gave one.

    People answer "company" with a name, a website, or both, and all three are useful. A bare
    domain becomes a name too — "corvusdata.io" reads as "Corvusdata" in a sentence, and the
    alternative is an agent saying "how are things at corvusdata.io".
    """
    text = " ".join((raw or "").split())
    if not text:
        return "", ""

    found = _DOMAIN_IN_TEXT.search(text)
    domain = ""
    if found:
        candidate = found.group("domain").lower().removeprefix("www.")
        tail = candidate.split(".", 1)[1]
        if tail in _REAL_TLDS or tail.rsplit(".", 1)[-1] in _REAL_TLDS:
            domain = candidate

    # Strip the URL out of the display name, so "Corvus Data (corvusdata.io)" is a company
    # called Corvus Data rather than one with a URL in its name.
    name = text
    if domain:
        # The path goes with the domain. Otherwise "https://corvusdata.io/about" leaves "about"
        # behind and the agent spends the call addressing a company called About.
        without_url = re.sub(rf"\S*{re.escape(domain)}\S*", " ", text, flags=re.IGNORECASE)
        name = " ".join(without_url.split()).strip(" ()-,|/")
        if len(name) < 2:
            name = domain.split(".")[0].replace("-", " ").title()

    return name[:80], domain


def _first_name(local: str) -> str:
    """A plausible first name from the local part, or nothing.

    `dana.whitfield` and `dana_w` and `dwhitfield` are all common; only the first is worth
    guessing from. Anything that does not look like a name returns empty, and the agent asks
    instead — which is a better first impression than "Hi, Info".
    """
    head = re.split(r"[._\-+0-9]", local)[0]
    if len(head) < 3 or head.lower() in _ROLE_LOCALS:
        return ""
    return head.capitalize()
