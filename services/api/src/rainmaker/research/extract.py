"""Turning fetched pages into typed facts.

EVERYTHING HERE IS DETERMINISTIC. No model calls, no randomness, no network. Given the same
pages it produces the same enrichment, which is what makes the pipeline testable and what
makes a wrong answer debuggable rather than mysterious.

That is a deliberate boundary, not a limitation. A language model belongs in this system —
reading a positioning statement, judging whether a job post implies our category — but it
belongs *behind* the deterministic layer, adding INFERRED fields on top of an OBSERVED base.
When the model is unavailable, the enrichment degrades to fewer fields rather than to fiction.
`agent.py` owns that composition; this module is the floor it stands on.

Every extractor returns `Sourced` values carrying the URL and the matched text, because a
sales rep will ask "where did you get that" and "the model said so" is not an answer.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime

from .fetch import Page
from .schema import (
    BuyingSignal,
    CompanySize,
    HiringSignal,
    PricingModel,
    Provenance,
    Sourced,
    TechSignal,
)

# ── pricing ──────────────────────────────────────────────────────────────────
# Ordered: the first pattern that matches wins, so put the least ambiguous first.
_PRICE = re.compile(
    r"(?:^|[\s(])\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\s*"
    r"(?:/|per\s+)?\s*(?:mo|month|user|seat|yr|year)?",
    re.IGNORECASE,
)
_CONTACT_SALES = re.compile(
    r"\b(contact (?:us|sales)|talk to (?:us|sales)|request a (?:demo|quote)|"
    r"get in touch|book a (?:demo|call)|custom pricing|let'?s talk)\b",
    re.IGNORECASE,
)
_FREE_TIER = re.compile(
    r"\b(free (?:tier|plan|forever)|start(?:s)? free|no credit card)\b", re.IGNORECASE
)
_SELF_SERVE = re.compile(
    r"\b(start (?:now|building)|sign up|get started|subscribe|buy now|"
    r"credit card|checkout)\b",
    re.IGNORECASE,
)


def extract_pricing(page: Page) -> tuple[Sourced[PricingModel], Sourced[float] | None]:
    """Classify the pricing motion and pull the entry price if one is published.

    The classification matters more than the number: it decides whether this account is
    self-serve (no AE) or sales-assisted (route to a human), which is the single most
    consequential routing decision the platform makes.
    """
    text = page.markdown
    prices = [_to_float(m.group(1)) for m in _PRICE.finditer(text)]
    prices = [p for p in prices if p is not None and 0 < p < 1_000_000]

    has_contact = bool(_CONTACT_SALES.search(text))
    has_free = bool(_FREE_TIER.search(text))
    has_checkout = bool(_SELF_SERVE.search(text))

    if prices and has_contact:
        # Published entry tiers plus a "contact us" ceiling: the standard hybrid motion.
        model, conf = PricingModel.SALES_ASSISTED, 0.85
    elif prices and has_checkout:
        model, conf = PricingModel.SELF_SERVE, 0.9
    elif prices:
        model, conf = PricingModel.SELF_SERVE, 0.6
    elif has_contact:
        model, conf = PricingModel.ENTERPRISE_ONLY, 0.75
    elif has_free:
        model, conf = PricingModel.FREE, 0.5
    else:
        model, conf = PricingModel.UNKNOWN, 0.0

    sourced_model = Sourced[PricingModel](
        value=model,
        provenance=Provenance.DERIVED,
        source_url=page.url,
        excerpt=_window(text, _CONTACT_SALES.search(text) or _PRICE.search(text)),
        confidence=conf,
    )
    entry: Sourced[float] | None = None
    if prices:
        low = min(prices)
        entry = Sourced[float](
            value=low,
            provenance=Provenance.OBSERVED,
            source_url=page.url,
            excerpt=_window(text, _PRICE.search(text)),
            confidence=0.8,
        )
    return sourced_model, entry


# ── tech stack ───────────────────────────────────────────────────────────────
# Category matters more than the specific product for qualification, so the table is keyed
# that way. Kept small and explicit rather than sourced from a large fingerprint database:
# a wrong tech detection erodes trust faster than a missing one.
_TECH: dict[str, tuple[str, ...]] = {
    "language": ("python", "typescript", "golang", "rust", "java", "ruby", "elixir"),
    "frontend": ("react", "next.js", "vue", "svelte", "angular"),
    "backend": ("django", "fastapi", "rails", "express", "spring boot", "laravel"),
    "database": ("postgres", "postgresql", "mysql", "mongodb", "dynamodb", "clickhouse",
                 "snowflake", "bigquery", "duckdb"),
    "vector_db": ("pinecone", "weaviate", "qdrant", "milvus", "pgvector", "chroma"),
    "cloud": ("aws", "gcp", "google cloud", "azure", "cloudflare", "vercel", "fly.io"),
    "orchestration": ("kubernetes", "airflow", "prefect", "dagster", "temporal", "argo"),
    "observability": ("datadog", "grafana", "sentry", "opentelemetry", "prometheus"),
    "ai": ("openai", "anthropic", "claude", "llama", "hugging face", "langchain",
           "vllm", "pytorch"),
    "payments": ("stripe", "adyen", "braintree", "paddle"),
}


def extract_tech(pages: list[Page]) -> list[TechSignal]:
    """Detect stack mentions across every fetched page.

    Word-boundary matched. Substring matching produces embarrassing false positives — "java"
    inside "javascript", "aws" inside "laws" — and a sales agent that opens with a wrong
    technical claim has lost the call.
    """
    found: dict[str, TechSignal] = {}
    for page in pages:
        lowered = page.markdown.lower()
        for category, names in _TECH.items():
            for name in names:
                pattern = re.compile(rf"(?<![\w.-]){re.escape(name)}(?![\w-])")
                match = pattern.search(lowered)
                if not match or name in found:
                    continue
                found[name] = TechSignal(
                    name=name,
                    category=category,
                    signal=Sourced[str](
                        value=name,
                        provenance=Provenance.OBSERVED,
                        source_url=page.url,
                        excerpt=_window(page.markdown, match),
                        confidence=0.7,
                    ),
                )
    return sorted(found.values(), key=lambda t: (t.category, t.name))


# ── hiring ───────────────────────────────────────────────────────────────────
_ROLE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\[)?\s*"
    r"((?:senior|staff|principal|lead|junior|head of)?\s*"
    r"(?:software|backend|frontend|full[- ]stack|data|machine learning|ml|ai|platform|"
    r"infrastructure|devops|security|product|sales|marketing|design|research)"
    r"[\w /&-]{0,40})",
    re.IGNORECASE | re.MULTILINE,
)
_DEPT = {
    "engineering": ("software", "backend", "frontend", "full", "platform",
                    "infrastructure", "devops", "security"),
    "data": ("data", "machine learning", " ml", "ai ", "research"),
    "product": ("product", "design"),
    "gtm": ("sales", "marketing"),
}


#: The nouns a job title ends on.
#:
#: WHY THE PATTERN ABOVE IS NOT ENOUGH. `_ROLE` finds a department word at the start of a line
#: and then takes up to forty more characters of it — which on a real careers page is usually
#: the rest of a marketing sentence. Run against stripe.com it produced, as open roles:
#:
#:     "AI is replatforming the global economy"
#:     "Products and pricing Pricing Atlas Authorizatio"
#:
#: Neither contains sentence punctuation, so the existing "does it look like a sentence" check
#: passed both. That check asks what a title is NOT. This asks what one IS: a noun phrase that
#: lands on a role, which is how a person recognises one at a glance.
#:
#: This matters more than it reads. The opening line of a call now speaks the strongest research
#: finding verbatim — see `Need.narrow` and `Agenda._open` — so a sentence that got this far
#: would be read out loud to the person whose careers page it was taken from.
_ROLE_NOUNS = (
    "engineer", "developer", "architect", "scientist", "analyst", "researcher",
    "manager", "director", "lead", "head", "officer", "president", "chief",
    "designer", "specialist", "associate", "consultant", "coordinator",
    "administrator", "technician", "strategist", "marketer", "advocate",
    "representative", "recruiter", "counsel", "partner", "writer", "producer",
    "intern", "apprentice", "generalist", "operator", "accountant", "controller",
)

#: "Head of Sales" and "VP of Engineering" end on a department rather than on a role, and are
#: unambiguously job titles anyway.
_ROLE_PREFIXES = ("head of", "vp of", "vice president of", "director of", "chief")


def _is_job_title(title: str) -> bool:
    """Whether a matched line is plausibly a job title rather than a sentence about one."""
    words = title.lower().split()
    if not (1 < len(words) <= 7):
        return False
    if any(title.lower().startswith(prefix) for prefix in _ROLE_PREFIXES):
        return True
    # The last word carries it — "Research and Development Engineer" is a title and
    # "Research and Development is how we grow" is not, and only the ending separates them.
    return words[-1].rstrip("s") in _ROLE_NOUNS or words[-1] in _ROLE_NOUNS


def extract_hiring(pages: list[Page]) -> list[HiringSignal]:
    roles: dict[str, HiringSignal] = {}
    for page in pages:
        if not _looks_like_careers(page):
            continue
        for match in _ROLE.finditer(page.markdown):
            title = " ".join(match.group(1).split())[:80]
            if len(title) < 6 or title.lower() in roles:
                continue
            if not _is_job_title(title):
                continue
            roles[title.lower()] = HiringSignal(
                title=title,
                department=_department(title),
                url=page.url,
            )
    return sorted(roles.values(), key=lambda r: r.title)[:60]


def _department(title: str) -> str | None:
    low = f" {title.lower()} "
    for dept, needles in _DEPT.items():
        if any(n in low for n in needles):
            return dept
    return None


def _looks_like_careers(page: Page) -> bool:
    url = page.url.lower()
    if any(seg in url for seg in ("/careers", "/jobs", "/join", "/hiring", "/work-with-us")):
        return True
    head = page.markdown[:2000].lower()
    return ("open positions" in head or "open roles" in head or "we're hiring" in head)


# ── company size ─────────────────────────────────────────────────────────────
_HEADCOUNT = re.compile(
    r"\b(\d{1,3}(?:,\d{3})*|\d+)\+?\s*(?:employees|people|team members|"
    r"engineers|of us)\b",
    re.IGNORECASE,
)

# Open-role count is a proxy, not a measurement. The bands are deliberately wide because the
# relationship is noisy: a 30-person company in a hiring sprint can out-post a 300-person one
# in a freeze. Used only when no explicit headcount is stated anywhere.
_ROLE_COUNT_BANDS = ((1, CompanySize.SMALL), (8, CompanySize.MID),
                     (25, CompanySize.LARGE), (75, CompanySize.ENTERPRISE))


def extract_size(pages: list[Page], hiring: list[HiringSignal]) -> Sourced[CompanySize]:
    for page in pages:
        match = _HEADCOUNT.search(page.markdown)
        if match:
            n = _to_int(match.group(1))
            if n:
                return Sourced[CompanySize](
                    value=_band(n),
                    provenance=Provenance.OBSERVED,
                    source_url=page.url,
                    excerpt=_window(page.markdown, match),
                    confidence=0.85,
                )

    if hiring:
        band = CompanySize.MICRO
        for threshold, candidate in _ROLE_COUNT_BANDS:
            if len(hiring) >= threshold:
                band = candidate
        return Sourced[CompanySize](
            value=band,
            provenance=Provenance.DERIVED,
            source_url=hiring[0].url,
            excerpt=f"derived from {len(hiring)} open roles",
            # Low on purpose. This is the weakest inference the pipeline makes and the UI
            # should render it as such rather than as a fact.
            confidence=0.35,
        )
    return Sourced[CompanySize](
        value=CompanySize.UNKNOWN, provenance=Provenance.DERIVED, confidence=0.0
    )


def _band(n: int) -> CompanySize:
    if n <= 1:
        return CompanySize.SOLO
    if n <= 10:
        return CompanySize.MICRO
    if n <= 50:
        return CompanySize.SMALL
    if n <= 250:
        return CompanySize.MID
    if n <= 1000:
        return CompanySize.LARGE
    return CompanySize.ENTERPRISE


# ── buying signals ───────────────────────────────────────────────────────────
_FUNDING = re.compile(
    r"\b(raised|closed|announcing)\s+(?:our\s+)?\$?\s?\d+(?:\.\d+)?\s*[mb]?\s*"
    r"(?:million|billion)?\s*(?:series\s+[a-e]|seed|pre-seed|round)\b",
    re.IGNORECASE,
)
_LAUNCH = re.compile(r"\b(introducing|announcing|now available|general availability|"
                     r"we(?:'re| are) launching)\b", re.IGNORECASE)


def extract_signals(
    pages: list[Page], hiring: list[HiringSignal], our_category: tuple[str, ...] = ()
) -> list[BuyingSignal]:
    """Evidence that this account is in-market now.

    `our_category` is the caller's own keywords. A job post asking for skills adjacent to what
    we sell is the strongest public signal available and it is entirely account-specific, so
    it is a parameter rather than a constant.
    """
    signals: list[BuyingSignal] = []

    for page in pages:
        if m := _FUNDING.search(page.markdown):
            signals.append(
                BuyingSignal(
                    kind="new_funding",
                    detail=_sourced(page, m, 0.9),
                    weight=0.8,
                )
            )
            break

    for page in pages:
        if _is_changelog(page) and (m := _LAUNCH.search(page.markdown)):
            signals.append(
                BuyingSignal(kind="product_launch", detail=_sourced(page, m, 0.6), weight=0.3)
            )
            break

    eng = [r for r in hiring if r.department in ("engineering", "data")]
    if len(eng) >= 5:
        signals.append(
            BuyingSignal(
                kind="hiring_surge",
                detail=Sourced[str](
                    value=f"{len(eng)} open engineering/data roles",
                    provenance=Provenance.DERIVED,
                    source_url=eng[0].url,
                    excerpt=", ".join(r.title for r in eng[:5]),
                    confidence=0.7,
                ),
                weight=0.5,
            )
        )

    if our_category:
        pattern = re.compile("|".join(re.escape(k) for k in our_category), re.IGNORECASE)
        for page in pages:
            if not _looks_like_careers(page):
                continue
            if m := pattern.search(page.markdown):
                signals.append(
                    BuyingSignal(
                        kind="job_mentions_our_category",
                        detail=_sourced(page, m, 0.85),
                        weight=0.9,
                    )
                )
                break
    return signals


def _is_changelog(page: Page) -> bool:
    url = page.url.lower()
    return any(s in url for s in ("/changelog", "/releases", "/whats-new", "/blog"))


# ── description ──────────────────────────────────────────────────────────────
def extract_description(page: Page) -> Sourced[str] | None:
    """First substantial prose paragraph. The company's own positioning, in their words."""
    for block in page.markdown.split("\n\n"):
        clean = " ".join(block.split())
        if clean.startswith(("#", "-", "*", "|", "```", ">")):
            continue
        if 60 <= len(clean) <= 400 and clean.count(" ") >= 8:
            return Sourced[str](
                value=clean,
                provenance=Provenance.OBSERVED,
                source_url=page.url,
                excerpt=clean[:300],
                confidence=0.6,
            )
    return None


def extract_name(page: Page) -> Sourced[str] | None:
    if not page.title:
        return None
    # Page titles are near-universally "Name | Tagline" or "Name - Tagline".
    name = re.split(r"\s+[|\-–—]\s+", page.title)[0].strip()
    if not name or len(name) > 60:
        return None
    return Sourced[str](
        value=name,
        provenance=Provenance.OBSERVED,
        source_url=page.url,
        excerpt=page.title,
        confidence=0.75,
    )


# ── helpers ──────────────────────────────────────────────────────────────────
def _sourced(page: Page, match: re.Match[str], confidence: float) -> Sourced[str]:
    return Sourced[str](
        value=" ".join(match.group(0).split()),
        provenance=Provenance.OBSERVED,
        source_url=page.url,
        excerpt=_window(page.markdown, match),
        confidence=confidence,
    )


def _window(text: str, match: re.Match[str] | None, radius: int = 140) -> str | None:
    if match is None:
        return None
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    return " ".join(text[start:end].split())[:600]


def _to_float(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _to_int(s: str) -> int | None:
    try:
        return int(s.replace(",", ""))
    except ValueError:
        return None


def dominant_industry(tech: list[TechSignal]) -> str | None:
    """Crude sector guess from the stack. Returns None rather than a low-confidence label.

    Kept honest: this is a heuristic over a small keyword table, so it declines to answer far
    more often than it guesses. A confidently wrong industry on a call is worse than silence.
    """
    if not tech:
        return None
    counts = Counter(t.category for t in tech)
    if counts.get("ai", 0) >= 2:
        return "AI / ML"
    if counts.get("vector_db", 0) >= 1 and counts.get("ai", 0) >= 1:
        return "AI infrastructure"
    if counts.get("payments", 0) >= 1:
        return "Fintech"
    if counts.get("observability", 0) >= 2:
        return "Developer tooling"
    return None


def parse_date(value: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%B %d, %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None
