"""Getting a company's name out of whatever its <title> tag happened to say.

WHY THIS IS ITS OWN FILE. The research agent reads the name from the page title, so what arrives
is "Home \\ Anthropic", "Plans & Pricing | Claude by Anthropic", "Acme — the fastest way to ship".
The agent says that name out loud in the first sentence of the call, and "Hi, I had a look at
Home backslash Anthropic" has lost the room in four words. It is a small function guarding the
first impression, which is a reasonable thing to be able to find and test on its own.

THE HEURISTIC IS "SHORTEST MEANINGFUL SEGMENT", and it took a wrong one to get there. Preferring
the LONGEST segment seems right — more specific, more brand-like — and turns
"Corvus Data — Analytics for logistics" into "Analytics for logistics", which is a tagline, not
a company. Names are short and taglines are long, so the shortest surviving segment wins.
"""

from __future__ import annotations

import re

#: Words a site puts around its name in a title. Dropped before choosing, so "About us | Acme"
#: does not have to compete on length.
TITLE_NOISE = frozenset(
    {
        "home", "homepage", "welcome", "index", "official site", "official website",
        "pricing", "plans", "plans & pricing", "pricing & plans", "about", "about us",
        "careers", "jobs", "docs", "documentation", "blog", "contact", "contact us",
        "products", "product", "platform", "overview", "login", "sign in", "get started",
    }
)

#: Every separator a title might use, written as escapes rather than literal glyphs — an en dash,
#: an em dash and a hyphen are indistinguishable in most editors, and which ones are in this
#: class is the difference between "Anthropic" and "Home \\ Anthropic".
_SPLIT = re.compile(
    "\\s*(?:[|\u2013\u2014\u00b7:\u2022]|\\\\|/|\\s-\\s)\\s*"
)

#: Beyond this a "name" is a sentence. Better to fall back to the domain than to say it.
_MAX_NAME_CHARS = 60


def clean_company_name(raw: str, *, fallback: str = "") -> str:
    """The company's name, or `fallback` when the title yields nothing usable.

    >>> clean_company_name("Home \\\\ Anthropic")
    'Anthropic'
    >>> clean_company_name("Plans & Pricing | Claude by Anthropic")
    'Claude by Anthropic'
    >>> clean_company_name("Corvus Data \u2014 Analytics for logistics")
    'Corvus Data'
    >>> clean_company_name("Welcome", fallback="Acme")
    'Acme'
    """
    segments = [part.strip(" \t\u00a0") for part in _SPLIT.split(raw or "")]
    candidates = [
        part
        for part in segments
        if part and part.lower().strip(" .") not in TITLE_NOISE and len(part) <= _MAX_NAME_CHARS
    ]
    if not candidates:
        return fallback

    # Shortest by word count; the original order breaks ties, because a company that appears
    # twice in a title appears first.
    return min(candidates, key=lambda part: (len(part.split()), segments.index(part)))
