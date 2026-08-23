"""Research agent tests, against a synthetic site.

No network. `NullFetcher` serves a fixed page map, so every assertion here is about the
agent's LOGIC — what it decides to read, what it extracts, what it refuses to claim — rather
than about whether some real website happened to be up.

The tests that matter most are the negative ones. An enrichment pipeline's dangerous failure
is not missing a field, it is confidently producing a wrong one and handing it to a rep who
repeats it on a call.
"""

from __future__ import annotations

import pytest

from rainmaker.research import (
    CompanySize,
    NullFetcher,
    Page,
    PricingModel,
    Provenance,
    ResearchAgent,
    ResearchConfig,
    ResearchRequest,
)
from rainmaker.research.extract import (
    extract_hiring,
    extract_pricing,
    extract_signals,
    extract_size,
    extract_tech,
)
from rainmaker.research.schema import Sourced

BASE = "https://acme.dev"

SITE = {
    f"{BASE}/": """# Acme — Realtime Data Infrastructure

Acme is a managed streaming platform that lets engineering teams build realtime data
pipelines without operating Kafka themselves. Used by thousands of developers worldwide.

[Pricing](/pricing) · [Careers](/company/careers) · [Docs](/docs) · [Blog](/blog)
[Partner site](https://someone-else.example/partners)
""",
    f"{BASE}/pricing": """# Pricing

## Starter
$49 / month — up to 5 million events. Get started, no credit card required.

## Growth
$499 per month — includes 50 million events.

## Enterprise
Contact sales for custom pricing and volume discounts.
""",
    f"{BASE}/company/careers": """# Careers

We're hiring. Open positions:

- Senior Backend Engineer — Remote
- Staff Data Engineer — Berlin
- Machine Learning Engineer — Remote
- Platform Engineer — Remote
- Infrastructure Engineer — London
- Product Designer — Remote
- Head of Sales — New York

You'll work with Python, Go, Kubernetes and ClickHouse. Experience with
learning-to-rank or vector search is a plus.
""",
    f"{BASE}/about": """# About Acme

Founded in 2021. We are 140 employees across three offices.

Our stack runs on AWS with Postgres and ClickHouse.
""",
    f"{BASE}/docs": """# Documentation

Acme SDKs are available for Python and TypeScript. Deploy on Kubernetes,
monitor with Grafana and OpenTelemetry.
""",
    f"{BASE}/blog": """# Blog

## Announcing Acme 3.0
We are launching a new query engine today.

## We raised our $40 million Series B
Closed a $40 million Series B led by Example Ventures.
""",
}


def page(url: str) -> Page:
    return Page(url=url, markdown=SITE[url], title="Acme — Realtime Data Infrastructure")


# ────────────────────────────────────────────────────────────── pricing
class TestPricing:
    def test_detects_hybrid_self_serve_plus_enterprise(self):
        model, entry = extract_pricing(page(f"{BASE}/pricing"))
        assert model.value is PricingModel.SALES_ASSISTED
        assert entry is not None
        assert entry.value == 49.0
        assert entry.provenance is Provenance.OBSERVED

    def test_contact_only_page_is_enterprise(self):
        p = Page(url=f"{BASE}/p", markdown="# Pricing\n\nContact sales for a quote.")
        model, entry = extract_pricing(p)
        assert model.value is PricingModel.ENTERPRISE_ONLY
        assert entry is None

    def test_a_page_with_no_pricing_information_says_unknown(self):
        """The important negative case: silence, not a guess."""
        p = Page(url=f"{BASE}/p", markdown="# About us\n\nWe like building things.")
        model, entry = extract_pricing(p)
        assert model.value is PricingModel.UNKNOWN
        assert model.confidence == 0.0
        assert entry is None

    def test_every_pricing_claim_carries_its_source(self):
        model, entry = extract_pricing(page(f"{BASE}/pricing"))
        assert str(model.source_url).startswith(BASE)
        assert entry and entry.excerpt


# ────────────────────────────────────────────────────────────── tech
class TestTech:
    def test_detects_stack_across_pages(self):
        tech = extract_tech([page(f"{BASE}/about"), page(f"{BASE}/docs")])
        names = {t.name for t in tech}
        assert {"aws", "postgres", "clickhouse", "kubernetes", "grafana"} <= names

    def test_does_not_match_inside_a_longer_word(self):
        """'java' inside 'javascript' and 'aws' inside 'laws' are the classic false
        positives, and a wrong technical claim on a call is unrecoverable."""
        p = Page(url=f"{BASE}/x", markdown="We write javascript and follow local laws.")
        names = {t.name for t in extract_tech([p])}
        assert "java" not in names
        assert "aws" not in names

    def test_every_detection_is_observed_with_an_excerpt(self):
        for t in extract_tech([page(f"{BASE}/docs")]):
            assert t.signal.provenance is Provenance.OBSERVED
            assert t.signal.excerpt


# ────────────────────────────────────────────────────────────── hiring & size
class TestHiringAndSize:
    def test_extracts_roles_from_a_careers_page(self):
        roles = extract_hiring([page(f"{BASE}/company/careers")])
        titles = {r.title.lower() for r in roles}
        assert any("backend engineer" in t for t in titles)
        assert any("machine learning" in t for t in titles)

    def test_ignores_pages_that_are_not_careers_pages(self):
        assert extract_hiring([page(f"{BASE}/docs")]) == []

    def test_explicit_headcount_beats_the_role_count_heuristic(self):
        roles = extract_hiring([page(f"{BASE}/company/careers")])
        size = extract_size([page(f"{BASE}/about")], roles)
        assert size.value is CompanySize.MID          # "140 employees"
        assert size.provenance is Provenance.OBSERVED
        assert size.confidence >= 0.8

    def test_role_count_fallback_is_marked_low_confidence(self):
        """The weakest inference the pipeline makes; it must not look like a fact."""
        roles = extract_hiring([page(f"{BASE}/company/careers")])
        size = extract_size([page(f"{BASE}/docs")], roles)
        assert size.provenance is Provenance.DERIVED
        assert size.confidence < 0.5

    def test_no_evidence_at_all_yields_unknown(self):
        size = extract_size([page(f"{BASE}/docs")], [])
        assert size.value is CompanySize.UNKNOWN
        assert size.confidence == 0.0


# ────────────────────────────────────────────────────────────── signals
class TestSignals:
    def test_detects_funding(self):
        signals = extract_signals([page(f"{BASE}/blog")], [], ())
        assert any(s.kind == "new_funding" for s in signals)

    def test_detects_an_engineering_hiring_surge(self):
        roles = extract_hiring([page(f"{BASE}/company/careers")])
        signals = extract_signals([], roles, ())
        surge = [s for s in signals if s.kind == "hiring_surge"]
        assert surge and surge[0].detail.provenance is Provenance.DERIVED

    def test_job_post_mentioning_our_category_is_the_strongest_signal(self):
        roles = extract_hiring([page(f"{BASE}/company/careers")])
        signals = extract_signals(
            [page(f"{BASE}/company/careers")], roles, ("learning-to-rank", "vector search")
        )
        match = [s for s in signals if s.kind == "job_mentions_our_category"]
        assert match
        assert match[0].weight == max(s.weight for s in signals)

    def test_score_saturates_rather_than_summing_without_bound(self):
        from rainmaker.research.schema import BuyingSignal, Enrichment

        def sig(w: float) -> BuyingSignal:
            return BuyingSignal(
                kind="hiring_surge",
                detail=Sourced[str](value="x", provenance=Provenance.DERIVED),
                weight=w,
            )

        many_weak = Enrichment(domain="a.com", signals=[sig(0.1) for _ in range(5)])
        one_strong = Enrichment(domain="b.com", signals=[sig(0.9)])
        assert one_strong.score > many_weak.score
        assert Enrichment(domain="c.com", signals=[sig(1.0)] * 20).score <= 1.0


# ────────────────────────────────────────────────────────────── the agent
class TestAgent:
    @pytest.fixture
    def agent(self) -> ResearchAgent:
        return ResearchAgent(
            NullFetcher(SITE),
            ResearchConfig(
                min_interval=0.0,
                respect_robots=False,
                our_category=("learning-to-rank", "vector search"),
            ),
        )

    async def test_end_to_end_enrichment(self, agent: ResearchAgent):
        result = await agent.research(ResearchRequest(domain="acme.dev"))
        assert result.name and result.name.value == "Acme"
        assert result.description and "streaming" in result.description.value
        assert result.pricing_model.value is PricingModel.SALES_ASSISTED
        assert result.published_price_usd and result.published_price_usd.value == 49.0
        assert result.size.value is CompanySize.MID
        assert len(result.hiring) >= 5
        assert {t.name for t in result.tech} >= {"clickhouse", "kubernetes"}
        assert result.score > 0.5

    async def test_follows_the_discovered_careers_link_not_the_guess(
        self, agent: ResearchAgent
    ):
        """The careers page is at /company/careers, not /careers. Using the link from the
        landing page is the difference between finding it and burning budget on a 404."""
        result = await agent.research(ResearchRequest(domain="acme.dev"))
        assert f"{BASE}/company/careers" in [str(u) for u in result.pages_fetched]

    async def test_respects_the_page_budget(self):
        agent = ResearchAgent(
            NullFetcher(SITE), ResearchConfig(min_interval=0.0, respect_robots=False)
        )
        result = await agent.research(ResearchRequest(domain="acme.dev", max_pages=2))
        assert len(result.pages_fetched) <= 2
        assert any("budget" in reason for _, reason in result.pages_skipped)

    async def test_never_leaves_the_prospects_domain(self, agent: ResearchAgent):
        """The landing page links to a partner site. Following it would spend the budget on
        a company we are not researching."""
        result = await agent.research(ResearchRequest(domain="acme.dev"))
        for url in result.pages_fetched:
            assert "someone-else.example" not in str(url)

    async def test_records_every_page_it_could_not_read(self, agent: ResearchAgent):
        result = await agent.research(ResearchRequest(domain="acme.dev"))
        # /changelog does not exist on this site and must appear as a skip, not vanish.
        assert any("changelog" in url for url, _ in result.pages_skipped)

    async def test_a_domain_with_no_site_returns_empty_not_an_error(self):
        agent = ResearchAgent(
            NullFetcher({}), ResearchConfig(min_interval=0.0, respect_robots=False)
        )
        result = await agent.research(ResearchRequest(domain="nothing-here.test"))
        assert result.pages_fetched == []
        assert result.score == 0.0
        assert result.size.value is CompanySize.UNKNOWN

    async def test_provenance_breakdown_is_reported(self, agent: ResearchAgent):
        result = await agent.research(ResearchRequest(domain="acme.dev"))
        counts = result.field_count()
        # A healthy run is dominated by OBSERVED. Mostly-INFERRED means the crawl failed and
        # something filled the gap, which looks like success and is not.
        assert counts["observed"] > counts["inferred"]

    async def test_is_deterministic(self, agent: ResearchAgent):
        a = await agent.research(ResearchRequest(domain="acme.dev"))
        b = await agent.research(ResearchRequest(domain="acme.dev"))
        assert a.model_dump(exclude={"fetched_at", "duration_ms", "cache_hit"}) == \
               b.model_dump(exclude={"fetched_at", "duration_ms", "cache_hit"})


# ────────────────────────────────────────────────────────────── schema guarantees
class TestSchemaGuarantees:
    def test_an_inferred_value_without_evidence_is_rejected(self):
        """The rule that keeps the model honest: no unsourced inference enters the system."""
        with pytest.raises(ValueError, match="excerpt"):
            Sourced[str](value="probably fintech", provenance=Provenance.INFERRED)

    def test_an_inferred_value_with_evidence_is_accepted(self):
        s = Sourced[str](
            value="fintech",
            provenance=Provenance.INFERRED,
            excerpt="we process payments for marketplaces",
        )
        assert s.value == "fintech"

    def test_observed_values_do_not_require_an_excerpt(self):
        assert Sourced[int](value=5, provenance=Provenance.OBSERVED).value == 5

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("https://www.Example.com/", "example.com"),
            ("HTTP://Example.com/pricing", "example.com"),
            ("  example.com  ", "example.com"),
            ("sub.example.co.uk", "sub.example.co.uk"),
        ],
    )
    def test_domain_normalisation(self, raw: str, expected: str):
        assert ResearchRequest(domain=raw).domain == expected

    def test_a_bare_word_is_not_a_domain(self):
        with pytest.raises(ValueError):
            ResearchRequest(domain="localhost")
