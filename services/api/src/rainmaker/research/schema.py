"""What the research agent is contractually required to produce.

DESIGN RULE — the agent returns STRUCTURED DATA WITH PROVENANCE, never prose. A sales agent
that says "they seem to be mid-market" is useless: nobody can act on it, nobody can correct it,
and nobody can tell whether it was read off the page or invented. Every field here carries the
URL it came from and how it was determined, so a rep can click through and a reviewer can audit.

The `confidence` and `source` fields are not decoration. They are the difference between an
enrichment pipeline and a hallucination pipeline:

    OBSERVED    the value appears verbatim on a page we fetched. Citable.
    DERIVED     computed from observed values by a documented rule (headcount band from job
                post count). Reproducible without a model.
    INFERRED    a language model's reading of the page. Useful, and the only tier that can be
                wrong in an interesting way -- so it is labelled, and the UI renders it
                differently.

Nothing in this module is allowed to be `INFERRED` without also carrying the passage the
inference was drawn from. An unsourced inference is indistinguishable from a guess.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Literal, TypeVar

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


class Provenance(StrEnum):
    OBSERVED = "observed"
    DERIVED = "derived"
    INFERRED = "inferred"


T = TypeVar("T")


class Sourced[T](BaseModel):
    """A value plus where it came from. The only way a fact enters the system."""

    value: T
    provenance: Provenance
    source_url: HttpUrl | None = None
    # The literal text the value was read from. Required for INFERRED, so a reviewer can
    # check the model's reading against the page rather than trusting it.
    excerpt: str | None = Field(default=None, max_length=600)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _inference_needs_evidence(self) -> Sourced[T]:
        """An INFERRED value must carry the text it was inferred from.

        A MODEL validator, not a field validator. `@field_validator("excerpt")` does not run
        when the field is simply omitted — Pydantic skips validation of defaulted fields
        unless `validate_default` is set — so the rule silently did not apply in exactly the
        case it exists to catch: a model producing a claim and attaching no evidence at all.
        """
        if self.provenance is Provenance.INFERRED and not self.excerpt:
            raise ValueError(
                "an INFERRED value must carry the excerpt it was inferred from; "
                "an unsourced inference is a guess wearing a schema"
            )
        return self


class CompanySize(StrEnum):
    """Bands, not point estimates.

    A scraped headcount is wrong more often than it is right (stale About pages, LinkedIn
    inflation, contractors). A band is defensible from weak signals and is what actually
    drives routing: SMB self-serve vs. mid-market AE vs. enterprise.
    """

    SOLO = "solo"              # 1
    MICRO = "micro"            # 2-10
    SMALL = "small"            # 11-50
    MID = "mid"                # 51-250
    LARGE = "large"            # 251-1000
    ENTERPRISE = "enterprise"  # 1000+
    UNKNOWN = "unknown"


class PricingModel(StrEnum):
    FREE = "free"
    SELF_SERVE = "self_serve"        # published price, credit card checkout
    SALES_ASSISTED = "sales_assisted"  # "contact us" above some tier
    ENTERPRISE_ONLY = "enterprise_only"
    UNKNOWN = "unknown"


class TechSignal(BaseModel):
    """One piece of stack evidence.

    `category` matters more than `name` for qualification: knowing they run *a* vector
    database is a stronger buying signal than knowing which one.
    """

    name: str
    category: str
    signal: Sourced[str]


class HiringSignal(BaseModel):
    """Open roles. The single most reliable public growth indicator."""

    title: str
    department: str | None = None
    location: str | None = None
    posted: date | None = None
    url: HttpUrl | None = None


class BuyingSignal(BaseModel):
    """Something that suggests this account is in-market NOW.

    Deliberately typed rather than free text so the closer agent can branch on it and the
    pipeline can rank on it.
    """

    kind: Literal[
        "hiring_surge",
        "new_funding",
        "product_launch",
        "pricing_change",
        "competitor_mention",
        "docs_activity",
        "job_mentions_our_category",
    ]
    detail: Sourced[str]
    weight: float = Field(ge=0.0, le=1.0)


class Enrichment(BaseModel):
    """The complete research output for one account."""

    domain: str
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    name: Sourced[str] | None = None
    description: Sourced[str] | None = None
    industry: Sourced[str] | None = None
    size: Sourced[CompanySize] = Field(
        default_factory=lambda: Sourced[CompanySize](
            value=CompanySize.UNKNOWN, provenance=Provenance.DERIVED, confidence=0.0
        )
    )
    pricing_model: Sourced[PricingModel] = Field(
        default_factory=lambda: Sourced[PricingModel](
            value=PricingModel.UNKNOWN, provenance=Provenance.DERIVED, confidence=0.0
        )
    )
    published_price_usd: Sourced[float] | None = None

    tech: list[TechSignal] = Field(default_factory=list)
    hiring: list[HiringSignal] = Field(default_factory=list)
    signals: list[BuyingSignal] = Field(default_factory=list)

    # Auditability. Every page the agent touched, whether or not it produced a field.
    pages_fetched: list[HttpUrl] = Field(default_factory=list)
    pages_skipped: list[tuple[str, str]] = Field(default_factory=list)  # (url, reason)
    duration_ms: float = 0.0
    cache_hit: bool = False

    @property
    def score(self) -> float:
        """Aggregate buying-intent score in [0, 1].

        Saturating rather than linear: five weak signals should not outrank one strong one,
        and an unbounded sum makes the number meaningless across accounts with different
        amounts of public surface area.
        """
        if not self.signals:
            return 0.0
        total = sum(s.weight for s in self.signals)
        return round(1.0 - pow(2.718281828, -total), 4)

    def field_count(self) -> dict[str, int]:
        """How much was actually learned, by provenance tier. Printed after every run.

        A run that produces mostly INFERRED fields is a run where the crawl failed and the
        model filled the gap -- which looks like success and is not.
        """
        counts = {p.value: 0 for p in Provenance}
        for value in (self.name, self.description, self.industry, self.size,
                      self.pricing_model, self.published_price_usd):
            if value is not None:
                counts[value.provenance.value] += 1
        for t in self.tech:
            counts[t.signal.provenance.value] += 1
        for s in self.signals:
            counts[s.detail.provenance.value] += 1
        return counts


class ResearchRequest(BaseModel):
    domain: Annotated[str, Field(min_length=3, max_length=253)]
    max_pages: int = Field(default=12, ge=1, le=40)
    force_refresh: bool = False

    @field_validator("domain")
    @classmethod
    def _normalise(cls, v: str) -> str:
        v = v.strip().lower()
        for prefix in ("https://", "http://"):
            if v.startswith(prefix):
                v = v[len(prefix):]
        v = v.split("/")[0]
        if v.startswith("www."):
            v = v[4:]
        if "." not in v:
            raise ValueError(f"{v!r} is not a domain")
        return v
